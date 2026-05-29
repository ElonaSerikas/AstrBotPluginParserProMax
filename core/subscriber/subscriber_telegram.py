"""Telegram subscriber - polls channel posts via t.me/s/ preview page"""

import re
from typing import Optional

import aiohttp
from astrbot.api import logger
from bs4 import BeautifulSoup

from ..parsers.anti_ban import get_anti_ban
from .base import BaseSubscriber, SubUpdate, SubUserInfo


class TelegramSubscriber(BaseSubscriber):
    platform = "telegram"

    PREVIEW_URL = "https://t.me/s/{channel}"
    POST_URL = "https://t.me/{channel}/{post_id}"

    def __init__(self, session: aiohttp.ClientSession | None = None, proxy: str = ""):
        self._session = session
        self._proxy = proxy or None
        self._anti_ban = get_anti_ban()

    def _get_headers(self) -> dict:
        return {
            "User-Agent": self._anti_ban.random_ua(),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8",
        }

    @staticmethod
    def _parse_count(text: str) -> int:
        """Parse count like '10.6M', '1.2K', '500' into int"""
        if not text:
            return 0
        text = text.strip().replace(",", "").replace(" ", "")
        multipliers = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}
        for suffix, mult in multipliers.items():
            if text.upper().endswith(suffix):
                try:
                    return int(float(text[:-1]) * mult)
                except ValueError:
                    return 0
        try:
            return int(text)
        except ValueError:
            return 0

    @staticmethod
    def _extract_bg_url(style: str) -> str:
        """Extract URL from CSS background-image: url(...)"""
        if not style:
            return ""
        match = re.search(r"url\(['\"]?(.*?)['\"]?\)", style)
        return match.group(1) if match else ""

    async def fetch_updates(self, uid: str) -> list[SubUpdate]:
        """Fetch latest posts from a Telegram channel via t.me/s/ preview"""
        await self._anti_ban.wait_if_needed("t.me", min_interval=5.0)

        url = self.PREVIEW_URL.format(channel=uid)
        headers = self._get_headers()

        session = self._session or aiohttp.ClientSession()
        try:
            async with session.get(
                url,
                headers=headers,
                proxy=self._proxy,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"[Telegram订阅] 获取频道页面失败 channel={uid}, status={resp.status}")
                    self._anti_ban.record_failure("t.me", is_rate_limit=(resp.status == 429))
                    return []
                html = await resp.text()

            self._anti_ban.record_success("t.me")
            soup = BeautifulSoup(html, "html.parser")

            updates = []
            messages = soup.select(".tgme_widget_message_wrap")
            for msg_wrap in messages[:5]:
                msg = msg_wrap.select_one(".tgme_widget_message")
                if not msg:
                    continue

                # Post ID
                post_data = msg.get("data-post", "")
                post_id = post_data.split("/")[-1] if "/" in post_data else post_data
                if not post_id:
                    continue

                # Text
                text_el = msg.select_one(".tgme_widget_message_text")
                text = text_el.get_text(strip=True)[:500] if text_el else ""

                # Images
                image_urls = []
                photo_wraps = msg.select(".tgme_widget_message_photo_wrap")
                for pw in photo_wraps:
                    style = pw.get("style", "")
                    img_url = self._extract_bg_url(style)
                    if img_url:
                        image_urls.append(img_url)

                # Video
                video_url = ""
                video_el = msg.select_one("video.tgme_widget_message_video")
                if video_el and video_el.get("src"):
                    video_url = video_el["src"]

                # Video duration
                video_duration = ""
                duration_el = msg.select_one(".message_video_duration")
                if duration_el:
                    video_duration = duration_el.get_text(strip=True)

                # Cover (video thumb)
                cover_url = ""
                video_thumb = msg.select_one(".tgme_widget_message_video_thumb")
                if video_thumb:
                    style = video_thumb.get("style", "")
                    cover_url = self._extract_bg_url(style)

                # Views
                views = 0
                views_el = msg.select_one(".tgme_widget_message_views")
                if views_el:
                    views = self._parse_count(views_el.get_text(strip=True))

                # Timestamp
                timestamp = 0
                time_el = msg.select_one(".tgme_widget_message_date time")
                if time_el and time_el.get("datetime"):
                    from datetime import datetime
                    try:
                        dt = datetime.fromisoformat(time_el["datetime"].replace("Z", "+00:00"))
                        timestamp = int(dt.timestamp())
                    except Exception:
                        pass

                # Reactions
                reactions = []
                reaction_els = msg.select(".tgme_widget_message_reactions .tgme_reaction")
                for r in reaction_els:
                    count_el = r.select_one(".tgme_reaction_count")
                    if count_el:
                        reactions.append(count_el.get_text(strip=True))

                # Link preview
                link_preview = {}
                preview_el = msg.select_one(".tgme_widget_message_link_preview")
                if preview_el:
                    title_el = preview_el.select_one(".link_preview_title")
                    desc_el = preview_el.select_one(".link_preview_description")
                    link_preview = {
                        "title": title_el.get_text(strip=True) if title_el else "",
                        "description": desc_el.get_text(strip=True) if desc_el else "",
                    }

                # Forward (repost)
                forward_from = ""
                forward_el = msg.select_one(".tgme_widget_message_forward_author")
                if forward_el:
                    forward_from = forward_el.get_text(strip=True)

                update_type = "video" if video_url else ("image" if image_urls else "text")

                update = SubUpdate(
                    id=post_id,
                    platform="telegram",
                    uid=uid,
                    type=update_type,
                    title=link_preview.get("title", ""),
                    text=text,
                    image_urls=image_urls,
                    url=self.POST_URL.format(channel=uid, post_id=post_id),
                    timestamp=timestamp,
                    image_count=len(image_urls),
                    video_url=video_url,
                    video_duration=video_duration,
                    cover_url=cover_url,
                )
                updates.append(update)

            return updates

        except Exception as e:
            logger.warning(f"[Telegram订阅] 获取更新失败 channel={uid}: {e}")
            self._anti_ban.record_failure("t.me")
            return []
        finally:
            if self._session is None:
                await session.close()

    async def get_user_info(self, uid: str) -> Optional[SubUserInfo]:
        """Get channel info from Telegram"""
        await self._anti_ban.wait_if_needed("t.me", min_interval=5.0)

        url = self.PREVIEW_URL.format(channel=uid)
        headers = self._get_headers()

        session = self._session or aiohttp.ClientSession()
        try:
            async with session.get(
                url,
                headers=headers,
                proxy=self._proxy,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    return None
                html = await resp.text()

            soup = BeautifulSoup(html, "html.parser")

            # Channel info from sidebar
            name = ""
            avatar = ""
            subscriber_count = 0
            description = ""

            title_el = soup.select_one(".tgme_channel_info_header_title span[dir=auto]")
            if title_el:
                name = title_el.get_text(strip=True)

            avatar_el = soup.select_one(".tgme_page_photo_image img")
            if avatar_el and avatar_el.get("src"):
                avatar = avatar_el["src"]

            # Subscriber count
            counter_el = soup.select_one('.tgme_channel_info_counter[type="subscribers"] .counter_value')
            if counter_el:
                subscriber_count = self._parse_count(counter_el.get_text(strip=True))

            # Description
            desc_el = soup.select_one(".tgme_channel_info_description")
            if desc_el:
                description = desc_el.get_text(strip=True)

            return SubUserInfo(
                platform="telegram",
                uid=uid,
                name=name,
                avatar=avatar,
                handle=f"@{uid}",
                follower_count=subscriber_count,
                signature=description,
            )

        except Exception as e:
            logger.debug(f"[Telegram订阅] 获取频道信息失败 channel={uid}: {e}")
            return None
        finally:
            if self._session is None:
                await session.close()

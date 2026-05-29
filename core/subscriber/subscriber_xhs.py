"""Xiaohongshu (小红书) subscriber - polls user notes via __INITIAL_STATE__"""

import json
import re
from typing import Optional

import aiohttp
from astrbot.api import logger

from ..parsers.anti_ban import get_anti_ban
from .base import BaseSubscriber, SubUpdate, SubUserInfo


class XHSSubscriber(BaseSubscriber):
    platform = "xhs"

    PROFILE_URL = "https://www.xiaohongshu.com/user/profile/{uid}"
    NOTE_URL = "https://www.xiaohongshu.com/explore/{note_id}"

    def __init__(self, session: aiohttp.ClientSession | None = None, cookies: str = ""):
        self._session = session
        self._cookies = cookies
        self._anti_ban = get_anti_ban()

    def _get_headers(self) -> dict:
        return {
            "User-Agent": self._anti_ban.random_ua(),
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
                "image/webp,image/apng,*/*;q=0.8"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": "https://www.xiaohongshu.com/",
        }

    def _extract_initial_state(self, html: str) -> dict:
        pattern = r"window\.__INITIAL_STATE__=(.*?)</script>"
        matched = re.search(pattern, html)
        if not matched:
            return {}
        json_str = matched.group(1).replace("undefined", "null")
        try:
            return json.loads(json_str)
        except json.JSONDecodeError:
            return {}

    async def fetch_updates(self, uid: str) -> list[SubUpdate]:
        """Fetch latest notes from a 小红书 user profile page"""
        await self._anti_ban.wait_if_needed("xiaohongshu.com", min_interval=5.0)

        url = self.PROFILE_URL.format(uid=uid)
        headers = self._get_headers()
        if self._cookies:
            headers["cookie"] = self._cookies

        session = self._session or aiohttp.ClientSession()
        try:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    logger.warning(f"[XHS订阅] 获取用户页面失败 uid={uid}, status={resp.status}")
                    self._anti_ban.record_failure("xiaohongshu.com", is_rate_limit=(resp.status == 429))
                    return []
                html = await resp.text()

            self._anti_ban.record_success("xiaohongshu.com")
            data = self._extract_initial_state(html)
            if not data:
                logger.warning(f"[XHS订阅] 无法提取 __INITIAL_STATE__ uid={uid}")
                return []

            # 用户笔记列表在 user.notes 或 user.feeds 中
            user_data = data.get("user", {})
            notes = user_data.get("notes", [])
            if not notes:
                # 尝试其他路径
                notes = user_data.get("feeds", [])

            updates = []
            for note in notes[:5]:
                if not isinstance(note, dict):
                    continue
                note_id = note.get("id") or note.get("noteId") or note.get("note_id", "")
                if not note_id:
                    continue

                title = note.get("title") or note.get("displayTitle", "")
                desc = note.get("desc") or note.get("description", "")
                note_type = note.get("type", "normal")

                # 图片
                image_urls = []
                cover = note.get("cover", {})
                if isinstance(cover, dict):
                    cover_url = cover.get("urlDefault") or cover.get("url", "")
                    if cover_url:
                        image_urls.append(cover_url)

                # 时间戳
                timestamp = 0
                ts = note.get("time") or note.get("timestamp", 0)
                if isinstance(ts, (int, float)):
                    timestamp = int(ts) // 1000 if ts > 1e12 else int(ts)

                update_type = "video" if note_type == "video" else "image"

                updates.append(SubUpdate(
                    id=str(note_id),
                    platform="xhs",
                    uid=uid,
                    type=update_type,
                    title=title[:200] if title else "",
                    text=desc[:500] if desc else "",
                    image_urls=image_urls,
                    url=self.NOTE_URL.format(note_id=note_id),
                    timestamp=timestamp,
                    image_count=len(image_urls),
                    cover_url=image_urls[0] if image_urls else "",
                ))

            return updates

        except Exception as e:
            logger.warning(f"[XHS订阅] 获取更新失败 uid={uid}: {e}")
            self._anti_ban.record_failure("xiaohongshu.com")
            return []
        finally:
            if self._session is None:
                await session.close()

    async def get_user_info(self, uid: str) -> Optional[SubUserInfo]:
        """Get user profile info from 小红书"""
        await self._anti_ban.wait_if_needed("xiaohongshu.com", min_interval=5.0)

        url = self.PROFILE_URL.format(uid=uid)
        headers = self._get_headers()
        if self._cookies:
            headers["cookie"] = self._cookies

        session = self._session or aiohttp.ClientSession()
        try:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    return None
                html = await resp.text()

            data = self._extract_initial_state(html)
            if not data:
                return None

            user_data = data.get("user", {}).get("userPageData", {})
            if not user_data:
                user_data = data.get("user", {})

            nickname = user_data.get("nickname", "")
            avatar = user_data.get("imagebavatar") or user_data.get("avatar", "")
            red_id = user_data.get("redId") or user_data.get("red_id", "")
            desc = user_data.get("desc") or user_data.get("description", "")

            # 粉丝数
            follower_count = 0
            fans = user_data.get("fans") or user_data.get("fansCount", "")
            if isinstance(fans, (int, float)):
                follower_count = int(fans)
            elif isinstance(fans, str) and fans.isdigit():
                follower_count = int(fans)

            handle = f"小红书号 {red_id}" if red_id else ""

            return SubUserInfo(
                platform="xhs",
                uid=uid,
                name=nickname,
                avatar=avatar,
                handle=handle,
                follower_count=follower_count,
                signature=desc,
            )

        except Exception as e:
            logger.debug(f"[XHS订阅] 获取用户信息失败 uid={uid}: {e}")
            return None
        finally:
            if self._session is None:
                await session.close()

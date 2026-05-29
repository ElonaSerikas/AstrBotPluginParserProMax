"""Weibo (微博) subscriber - polls user timeline via mobile API"""

import re
import time
from typing import Optional

import aiohttp
from astrbot.api import logger

from ..parsers.anti_ban import get_anti_ban
from .base import BaseSubscriber, SubUpdate, SubUserInfo


class WeiboSubscriber(BaseSubscriber):
    platform = "weibo"

    TIMELINE_API = "https://m.weibo.cn/api/container/getIndex"
    USER_API = "https://m.weibo.cn/api/container/getIndex"
    WEIBO_URL = "https://m.weibo.cn/detail/{wid}"

    def __init__(self, session: aiohttp.ClientSession | None = None, cookies: str = ""):
        self._session = session
        self._cookies = cookies
        self._anti_ban = get_anti_ban()

    def _get_headers(self) -> dict:
        return {
            "User-Agent": self._anti_ban.random_ua(),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": "https://m.weibo.cn/",
            "X-Requested-With": "XMLHttpRequest",
            "mweibo-pwa": "1",
        }

    async def fetch_updates(self, uid: str) -> list[SubUpdate]:
        """Fetch latest weibo from a user via mobile API"""
        await self._anti_ban.wait_if_needed("m.weibo.cn", min_interval=5.0)

        headers = self._get_headers()
        if self._cookies:
            headers["cookie"] = self._cookies

        # containerid=107603{uid} 获取用户微博列表
        params = {
            "containerid": f"107603{uid}",
            "page": "1",
        }

        session = self._session or aiohttp.ClientSession()
        try:
            async with session.get(
                self.TIMELINE_API,
                params=params,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"[微博订阅] API 请求失败 uid={uid}, status={resp.status}")
                    self._anti_ban.record_failure("m.weibo.cn", is_rate_limit=(resp.status == 429))
                    return []
                data = await resp.json()

            self._anti_ban.record_success("m.weibo.cn")

            cards = data.get("data", {}).get("cards", [])
            updates = []
            for card in cards[:5]:
                if not isinstance(card, dict):
                    continue
                # 微博卡片类型 9 为微博内容
                if card.get("card_type") != 9:
                    continue

                mblog = card.get("mblog", {})
                if not isinstance(mblog, dict):
                    continue

                wid = str(mblog.get("id", ""))
                if not wid:
                    continue

                # 文本（清理 HTML）
                text_raw = mblog.get("text", "")
                text = re.sub(r"<[^>]+>", "", text_raw).strip()[:500]

                # 标题
                title = ""
                page_info = mblog.get("page_info", {})
                if isinstance(page_info, dict):
                    title = page_info.get("title", "")

                # 图片
                image_urls = []
                pics = mblog.get("pics", [])
                if isinstance(pics, list):
                    for pic in pics:
                        if isinstance(pic, dict):
                            # 优先大图
                            large = pic.get("large", {})
                            if isinstance(large, dict) and large.get("url"):
                                image_urls.append(large["url"])
                            elif pic.get("url"):
                                image_urls.append(pic["url"])

                # 视频
                video_url = ""
                cover_url = ""
                if isinstance(page_info, dict):
                    urls = page_info.get("urls", {})
                    if isinstance(urls, dict):
                        for key in ("mp4_720p_mp4", "mp4_hd_mp4", "mp4_ld_mp4"):
                            val = urls.get(key)
                            if val:
                                video_url = f"https:{val}" if val.startswith("//") else val
                                break
                    page_pic = page_info.get("page_pic", {})
                    if isinstance(page_pic, dict) and page_pic.get("url"):
                        cover_url = page_pic["url"]

                # 时间戳
                timestamp = 0
                created_at = mblog.get("created_at", "")
                if created_at:
                    try:
                        from time import mktime, strptime
                        ts = strptime(created_at, "%a %b %d %H:%M:%S %z %Y")
                        timestamp = int(mktime(ts))
                    except Exception:
                        pass

                # 统计
                reposts = mblog.get("reposts_count", 0)
                comments = mblog.get("comments_count", 0)
                likes = mblog.get("attitudes_count", 0)

                update_type = "video" if video_url else ("image" if image_urls else "text")

                updates.append(SubUpdate(
                    id=wid,
                    platform="weibo",
                    uid=uid,
                    type=update_type,
                    title=title[:200],
                    text=text,
                    image_urls=image_urls,
                    url=self.WEIBO_URL.format(wid=wid),
                    timestamp=timestamp,
                    image_count=len(image_urls),
                    video_url=video_url,
                    cover_url=cover_url,
                ))

            return updates

        except Exception as e:
            logger.warning(f"[微博订阅] 获取更新失败 uid={uid}: {e}")
            self._anti_ban.record_failure("m.weibo.cn")
            return []
        finally:
            if self._session is None:
                await session.close()

    async def get_user_info(self, uid: str) -> Optional[SubUserInfo]:
        """Get user profile info from 微博"""
        await self._anti_ban.wait_if_needed("m.weibo.cn", min_interval=5.0)

        headers = self._get_headers()
        if self._cookies:
            headers["cookie"] = self._cookies

        params = {
            "containerid": f"100505{uid}",
        }

        session = self._session or aiohttp.ClientSession()
        try:
            async with session.get(
                self.USER_API,
                params=params,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()

            self._anti_ban.record_success("m.weibo.cn")

            user_info = data.get("data", {}).get("userInfo", {})
            if not user_info:
                return None

            name = user_info.get("screen_name", "")
            avatar = user_info.get("avatar_hd") or user_info.get("profile_image_url", "")
            desc = user_info.get("description", "")
            follower_count = 0
            fans = user_info.get("followers_count")
            if isinstance(fans, int):
                follower_count = fans
            elif isinstance(fans, str) and fans.isdigit():
                follower_count = int(fans)

            return SubUserInfo(
                platform="weibo",
                uid=uid,
                name=name,
                avatar=avatar,
                handle=f"weibo:{uid}",
                follower_count=follower_count,
                signature=desc,
            )

        except Exception as e:
            logger.debug(f"[微博订阅] 获取用户信息失败 uid={uid}: {e}")
            return None
        finally:
            if self._session is None:
                await session.close()

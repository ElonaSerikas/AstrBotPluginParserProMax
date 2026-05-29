"""Kujiequ (库街区) subscriber - polls user posts via Kurobbs API"""

import hashlib
import time
from typing import Optional

import aiohttp
from astrbot.api import logger

from ..parsers.anti_ban import get_anti_ban
from .base import BaseSubscriber, SubUpdate, SubUserInfo


class KujiequSubscriber(BaseSubscriber):
    platform = "kujiequ"

    API_POST_LIST = "https://api.kurobbs.com/forum/getUserPostList"
    API_USER_INFO = "https://api.kurobbs.com/user/getUserInfo"
    POST_URL = "https://www.kurobbs.com/post/{post_id}"

    def __init__(self, session: aiohttp.ClientSession | None = None):
        self._session = session
        self._anti_ban = get_anti_ban()

    def _get_headers(self) -> dict:
        dev_code = hashlib.md5(f"astrbot_{time.time()}".encode()).hexdigest()[:16]
        return {
            "User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36",
            "source": "h5",
            "devcode": dev_code,
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        }

    async def fetch_updates(self, uid: str) -> list[SubUpdate]:
        """Fetch latest posts from a 库街区 user"""
        await self._anti_ban.wait_if_needed("kurobbs.com", min_interval=5.0)

        headers = self._get_headers()
        session = self._session or aiohttp.ClientSession()
        try:
            async with session.post(
                self.API_POST_LIST,
                headers=headers,
                data={
                    "userId": uid,
                    "pageIndex": "1",
                    "pageSize": "10",
                    "showOrderType": "1",
                },
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"[库街区订阅] API 请求失败 uid={uid}, status={resp.status}")
                    self._anti_ban.record_failure("kurobbs.com", is_rate_limit=(resp.status == 429))
                    return []
                data = await resp.json()

            self._anti_ban.record_success("kurobbs.com")

            if data.get("code") != 200:
                logger.warning(f"[库街区订阅] API 返回错误 uid={uid}: {data.get('msg')}")
                return []

            posts = data.get("data", {}).get("postList", [])
            updates = []
            for post in posts[:5]:
                if not isinstance(post, dict):
                    continue

                post_id = str(post.get("postId", ""))
                if not post_id:
                    continue

                title = post.get("postTitle", "")
                content = post.get("postContent", "")
                # 清理 HTML 标签
                import re
                clean_text = re.sub(r"<[^>]+>", "", content).strip()[:500] if content else ""

                # 图片
                image_urls = []
                for img_match in re.finditer(r'<img[^>]+src="([^"]+)"', content or ""):
                    img_url = img_match.group(1)
                    if img_url.startswith("http"):
                        image_urls.append(img_url)

                # 封面图
                cover_url = None
                for cover_key in ("coverImg", "coverUrl", "postCoverImg"):
                    val = post.get(cover_key)
                    if isinstance(val, str) and val.startswith("http"):
                        cover_url = val
                        break
                if not cover_url and image_urls:
                    cover_url = image_urls[0]

                # 时间戳
                timestamp = 0
                create_time = post.get("createTimestamp")
                if isinstance(create_time, (int, float)):
                    timestamp = int(create_time / 1000) if create_time > 1e12 else int(create_time)

                # 视频
                video_url = ""
                video_info = post.get("videoUrl") or post.get("video", "")
                if isinstance(video_info, str) and video_info.startswith("http"):
                    video_url = video_info

                update_type = "video" if video_url else ("image" if image_urls else "article")

                updates.append(SubUpdate(
                    id=post_id,
                    platform="kujiequ",
                    uid=uid,
                    type=update_type,
                    title=title[:200] if title else "",
                    text=clean_text,
                    image_urls=image_urls[:9],
                    url=self.POST_URL.format(post_id=post_id),
                    timestamp=timestamp,
                    image_count=len(image_urls),
                    video_url=video_url,
                    cover_url=cover_url or "",
                ))

            return updates

        except Exception as e:
            logger.warning(f"[库街区订阅] 获取更新失败 uid={uid}: {e}")
            self._anti_ban.record_failure("kurobbs.com")
            return []
        finally:
            if self._session is None:
                await session.close()

    async def get_user_info(self, uid: str) -> Optional[SubUserInfo]:
        """Get user profile info from 库街区"""
        await self._anti_ban.wait_if_needed("kurobbs.com", min_interval=5.0)

        headers = self._get_headers()
        session = self._session or aiohttp.ClientSession()
        try:
            async with session.post(
                self.API_USER_INFO,
                headers=headers,
                data={"userId": uid},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()

            if data.get("code") != 200:
                return None

            user = data.get("data", {})
            if not user:
                return None

            name = user.get("userName", "")
            avatar = user.get("headCodeUrl", "")
            desc = user.get("userSignature", "")
            follower_count = 0
            fans = user.get("fansCount")
            if isinstance(fans, (int, float)):
                follower_count = int(fans)

            return SubUserInfo(
                platform="kujiequ",
                uid=uid,
                name=name,
                avatar=avatar,
                handle=f"kurobbs:{uid}",
                follower_count=follower_count,
                signature=desc,
            )

        except Exception as e:
            logger.debug(f"[库街区订阅] 获取用户信息失败 uid={uid}: {e}")
            return None
        finally:
            if self._session is None:
                await session.close()

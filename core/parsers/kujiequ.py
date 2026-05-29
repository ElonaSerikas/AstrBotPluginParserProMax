import asyncio
import hashlib
import re
import time

import aiohttp
from astrbot.api import logger

from ..config import PluginConfig
from ..cookie import CookieJar
from ..data import Comment, Platform, ParseResult
from ..download import Downloader
from .base import BaseParser, ParseException, handle


class KujiequParser(BaseParser):
    """库街区（Kurobbs）解析器"""

    platform = Platform(name="kujiequ", display_name="库街区")

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.mycfg = config.parser.kujiequ
        self.cookiejar = CookieJar(config, self.mycfg, domain="kurobbs.com")
        if self.cookiejar.cookies_str:
            self.headers["cookie"] = self.cookiejar.cookies_str

    @handle("kurobbs", r"kurobbs\.com/(?:[a-z0-9]+/)?post/(\d+)\/?(?:\?.*)?$")
    async def _parse_post(self, matched):
        post_id = matched.group(1)
        url = f"https://www.kurobbs.com/post/{post_id}"
        api_url = "https://api.kurobbs.com/forum/getPostDetail"

        # 稳定 devcode（同一 AstrBot 实例不变）
        dev_code = hashlib.md5(b"astrbot_kurobbs_device").hexdigest()[:16]
        headers = {
            "User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36",
            "source": "h5",
            "devcode": dev_code,
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            **self.headers,
        }
        # 从 cookie 中提取 token 作为独立 header
        if self.cookiejar.cookies_str:
            for part in self.cookiejar.cookies_str.split(";"):
                part = part.strip()
                if part.startswith("token=") or part.startswith("user_token="):
                    headers["token"] = part.split("=", 1)[1]
                    break

        async def _fetch_post_detail():
            async with self.session.post(
                api_url,
                headers=headers,
                data={
                    "postId": post_id,
                    "isOnlyPublisher": "0",
                    "showOrderType": "2",
                },
            ) as resp:
                if resp.status != 200:
                    raise ParseException(f"API HTTP {resp.status}")
                return await resp.json()

        # 并发获取帖子详情和评论
        post_result, comments_result = await asyncio.gather(
            _fetch_post_detail(),
            self._fetch_comments(post_id, headers),
            return_exceptions=True,
        )

        # 处理帖子详情结果
        if isinstance(post_result, ParseException):
            raise post_result
        if isinstance(post_result, Exception):
            logger.warning(f"库街区 API 请求失败(post_id={post_id}): {post_result}")
            return self.result(
                title=f"库街区帖子 #{post_id}",
                text=f"请求失败: {post_result}",
                url=url,
            )

        data = post_result
        if data.get("code") != 200:
            err_msg = data.get("msg", "未知错误")
            code = data.get("code")
            logger.warning(f"库街区 API 返回错误(post_id={post_id}): code={code}, msg={err_msg}")
            if code == 102:
                return self.result(
                    title=f"库街区帖子 #{post_id}",
                    text=f"API 认证失败 (code=102)。请通过 cookie 设置有效的 user_token。",
                    url=url,
                )
            return self.result(
                title=f"库街区帖子 #{post_id}",
                text=f"API 错误: {err_msg} (code={code})",
                url=url,
            )

        post_data = data.get("data", {}).get("postDetail", {})
        content = post_data.get("postH5Content", "")
        title = post_data.get("postTitle", "")

        # 图片：从 HTML 内容中提取 <img> 标签
        image_urls = []
        for img_match in re.finditer(r'<img[^>]+src="([^"]+)"', content):
            img_url = img_match.group(1)
            if img_url.startswith("http"):
                image_urls.append(img_url)
        contents = self.create_image_contents(image_urls[:9]) if image_urls else []

        # 封面图：优先从 API 字段获取，回退到 HTML 第一张图
        cover_url = None
        for cover_key in ("coverImg", "coverUrl", "postCoverImg", "cover"):
            val = post_data.get(cover_key)
            if isinstance(val, str) and val.startswith("http"):
                cover_url = val
                break
        if not cover_url and image_urls:
            cover_url = image_urls[0]

        # 清理 HTML
        clean_text = re.sub(r"<[^>]+>", "", content).strip()[:500]

        # 作者信息（字段在 postDetail 顶层）
        author_name = post_data.get("userName", "")
        author_avatar = post_data.get("headCodeUrl", "")
        raw_uid = post_data.get("userId")
        author_uid = str(raw_uid) if raw_uid else None
        author_desc = post_data.get("userSignature", "")
        follower_count = None
        fans = post_data.get("fansCount")
        if isinstance(fans, (int, float)):
            follower_count = int(fans)

        # 时间戳（毫秒）
        timestamp = None
        create_time = post_data.get("createTimestamp")
        if isinstance(create_time, (int, float)):
            timestamp = int(create_time / 1000) if create_time > 1e12 else int(create_time)

        # 统计
        stats = {}
        for key, stat_key in (
            ("browseCount", "views"), ("likeCount", "likes"),
            ("commentCount", "comments"), ("collectionCount", "favorites"),
            ("shareCount", "reposts"),
        ):
            val = post_data.get(key)
            if isinstance(val, (int, float)):
                stats[stat_key] = int(val)

        # 处理评论结果（已在上面并发获取）
        pinned_comment, hot_comment = None, None
        if isinstance(comments_result, tuple):
            pinned_comment, hot_comment = comments_result

        author = self.create_author(
            name=author_name or "库街区用户",
            avatar=author_avatar or None,
            uid=author_uid,
            description=author_desc or None,
            follower_count=follower_count,
        ) if author_name else None

        extra: dict = {"post_id": post_id}
        if cover_url:
            extra["cover_url"] = cover_url
        if author_uid:
            extra["handle"] = f"kurobbs:{author_uid}"

        return self.result(
            title=title or None,
            text=clean_text or None,
            url=url,
            author=author,
            contents=contents,
            timestamp=timestamp,
            stats=stats or None,
            pinned_comment=pinned_comment,
            hot_comment=hot_comment,
            extra=extra,
            page_type="post",
        )

    async def _fetch_comments(self, post_id: str, headers: dict) -> tuple[Comment | None, Comment | None]:
        """获取帖子的置顶评论和热评（失败返回 (None, None)）"""
        try:
            api_url = "https://api.kurobbs.com/forum/comment/getPostCommentListV2"
            async with self.session.post(
                api_url,
                headers=headers,
                data={
                    "postId": post_id,
                    "showOrderType": "2",
                    "isOnlyPublisher": "0",
                    "pageIndex": "1",
                    "pageSize": "50",
                },
            ) as resp:
                if resp.status != 200:
                    return None, None
                data = await resp.json()

            if data.get("code") != 200:
                return None, None

            # 热门评论在 data.hotComments 中
            hot_comments = data.get("data", {}).get("hotComments", [])
            if not hot_comments:
                return None, None

            def _build_comment(item: dict) -> Comment | None:
                content_parts = []
                for cc in item.get("commentContent", []):
                    if cc.get("type") == 1:
                        content_parts.append(cc.get("content", ""))
                    for child in cc.get("children", []):
                        if child.get("type") == 1:
                            content_parts.append(child.get("content", ""))
                text = "\n".join(content_parts).strip()
                if not text:
                    return None
                comment_user = item.get("commentUserInfo", {}) or {}
                likes = item.get("likeCount", 0)
                if isinstance(likes, (int, float)):
                    likes = int(likes)
                else:
                    likes = 0
                return Comment(
                    author_name=comment_user.get("userName", ""),
                    content=text,
                    author_avatar=comment_user.get("headCodeUrl", ""),
                    likes=likes,
                    is_hot=True,
                )

            pinned_comment = None
            hot_comment = None
            for c in hot_comments:
                built = _build_comment(c)
                if not built:
                    continue
                if not pinned_comment:
                    pinned_comment = built
                elif not hot_comment:
                    hot_comment = built
                    break

            return pinned_comment, hot_comment
        except Exception as e:
            logger.debug(f"库街区评论获取失败(post_id={post_id}): {e}")
            return None, None


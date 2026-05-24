import re

from ..config import PluginConfig
from ..data import Platform, ParseResult, Author, ImageContent
from ..download import Downloader
from .base import BaseParser, handle


class KujiequParser(BaseParser):
    """库街区（Kurobbs）解析器"""

    platform = Platform(name="kujiequ", display_name="库街区")

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self._token = None

    @handle("kujiequ", r"kurobbs\.com/post/(\d+)")
    async def _parse_post(self, matched):
        import hashlib
        import time

        import aiohttp

        post_id = matched.group(1)
        url = f"https://www.kurobbs.com/post/{post_id}"
        api_url = "https://api.kurobbs.com/forum/getPostDetail"

        # Build device fingerprint
        dev_code = hashlib.md5(
            f"astrbot_{time.time()}".encode()
        ).hexdigest()[:16]
        headers = {
            "User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36",
            "source": "h5",
            "devcode": dev_code,
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        }

        try:
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
                    raise ValueError(f"API HTTP {resp.status}")
                data = await resp.json()
        except Exception:
            # Fallback to basic parse
            return ParseResult(
                platform=self.platform,
                text=f"库街区帖子 #{post_id}",
                url=url,
            )

        if data.get("code") != 200:
            return ParseResult(
                platform=self.platform,
                text=f"库街区帖子 #{post_id}",
                url=url,
            )

        post_data = data.get("data", {})
        content = post_data.get("content", "")
        title = post_data.get("title", "")
        images = []

        # Extract images from HTML content
        for img_match in re.finditer(r'<img[^>]+src="([^"]+)"', content):
            img_url = img_match.group(1)
            if img_url.startswith("http"):
                images.append(ImageContent(img_url, source_url=img_url))

        # Clean HTML from text
        clean_text = re.sub(r"<[^>]+>", "", content).strip()[:500]

        author_data = post_data.get("user", {}) or {}
        author_name = (
            author_data.get("userName", "")
            if isinstance(author_data, dict)
            else ""
        )

        return ParseResult(
            platform=self.platform,
            author=Author(name=author_name) if author_name else None,
            title=title,
            text=clean_text,
            url=url,
            contents=images[:9],
        )


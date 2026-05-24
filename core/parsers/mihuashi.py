import re

from ..config import PluginConfig
from ..data import Platform, ParseResult, Author, ImageContent
from ..download import Downloader
from .base import BaseParser, handle


class MihuashiParser(BaseParser):
    """米画师解析器 -  artworks作品页面"""

    platform = Platform(name="mihuashi", display_name="米画师")

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)

    @handle("mihuashi", r"mihuashi\.com/artworks/(\d+)")
    async def _parse_artwork(self, matched):
        import aiohttp

        artwork_id = matched.group(1)
        url = f"https://www.mihuashi.com/artworks/{artwork_id}"

        async with self.session.get(url, headers={"User-Agent": "Mozilla/5.0"}) as resp:
            if resp.status != 200:
                raise ValueError(f"HTTP {resp.status}")
            html = await resp.text()

        # Parse Open Graph meta tags
        images = []
        title = ""
        author_name = ""

        for img_match in re.finditer(
            r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', html
        ):
            img_url = img_match.group(1)
            if img_url.startswith("http"):
                images.append(ImageContent(img_url, source_url=img_url))

        title_match = re.search(
            r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', html
        )
        if title_match:
            title = title_match.group(1)

        desc_match = re.search(
            r'<meta[^>]+property="og:description"[^>]+content="([^"]+)"', html
        )
        text = desc_match.group(1) if desc_match else ""

        return ParseResult(
            platform=self.platform,
            author=Author(name=author_name or "米画师画师"),
            title=title,
            text=text[:500],
            url=url,
            contents=images,
        )


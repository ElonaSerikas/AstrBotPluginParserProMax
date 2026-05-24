from ..config import PluginConfig
from ..data import Platform, ParseResult, Author, ImageContent
from ..download import Downloader
from .base import BaseParser, handle


class LofterParser(BaseParser):
    """LOFTER（网易乐乎）解析器 - 轻小说/博客平台"""

    platform = Platform(name="lofter", display_name="LOFTER")

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)

    @handle("lofter", r"lofter\.com/lpost/(\w+)_(\w+)")
    async def _parse_lpost(self, matched):
        import aiohttp
        from bs4 import BeautifulSoup

        user_id = matched.group(1)
        post_id = matched.group(2)
        url = f"https://www.lofter.com/lpost/{user_id}_{post_id}"

        async with self.session.get(url, headers={"User-Agent": "Mozilla/5.0"}) as resp:
            if resp.status != 200:
                raise ValueError(f"HTTP {resp.status}")
            html = await resp.text()

        soup = BeautifulSoup(html, "html.parser")
        title = soup.find("title")

        # Extract images
        images = []
        for img in soup.select('img[src*="http"]'):
            src = img.get("src")
            if src and src.startswith("http") and not src.endswith(".ico"):
                images.append(ImageContent(src, source_url=src))

        # Extract text content from meta description
        text_elem = soup.find("meta", attrs={"name": "description"})
        text = text_elem.get("content", "") if text_elem else ""

        # Extract author info
        author_elem = soup.find("meta", attrs={"name": "author"})
        author_name = author_elem.get("content", "") if author_elem else ""

        return ParseResult(
            platform=self.platform,
            author=Author(name=author_name or "LOFTER用户"),
            title=title.text[:100] if title else "",
            text=text[:500],
            url=url,
            contents=images,
        )


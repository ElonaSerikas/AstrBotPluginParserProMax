from ..config import PluginConfig
from ..data import Platform, ParseResult, Author, ImageContent
from ..download import Downloader
from .base import BaseParser, handle


class HuajiaParser(BaseParser):
    """画加（网易）解析器"""

    platform = Platform(name="huajia", display_name="画加")

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)

    @handle("huajia", r"huajia\.163\.com/\S+")
    async def _parse_page(self, matched):
        import aiohttp
        from bs4 import BeautifulSoup

        url = matched.group(0)
        if not url.startswith("http"):
            url = "https://" + url

        try:
            async with self.session.get(
                url, headers={"User-Agent": "Mozilla/5.0"}
            ) as resp:
                if resp.status != 200:
                    raise ValueError(f"HTTP {resp.status}")
                html = await resp.text()
        except Exception:
            return ParseResult(
                platform=self.platform,
                text="画加页面解析暂不支持此链接格式",
                url=url,
            )

        soup = BeautifulSoup(html, "html.parser")
        title = soup.find("title")

        images = []
        for img in soup.select("img"):
            src = img.get("src") or img.get("data-src") or ""
            if src.startswith("http"):
                images.append(ImageContent(src, source_url=src))

        return ParseResult(
            platform=self.platform,
            title=title.text[:100] if title else "",
            url=url,
            contents=images[:9],
        )


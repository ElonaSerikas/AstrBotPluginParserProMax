from ..config import PluginConfig
from ..data import Platform, ParseResult, ImageContent
from ..download import Downloader
from .base import BaseParser, handle


class CPPParser(BaseParser):
    """CPP无差别同人站解析器"""

    platform = Platform(name="cpp", display_name="CPP无差别同人站")

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)

    @handle("cpp", r"allcpp\.cn/(?:d|p)/(\d+)\.do")
    async def _parse_work(self, matched):
        import aiohttp
        from bs4 import BeautifulSoup

        work_id = matched.group(1)
        path_type = "d" if "allcpp.cn/d/" in matched.group(0) else "p"
        url = f"https://www.allcpp.cn/{path_type}/{work_id}.do"

        async with self.session.get(url, headers={"User-Agent": "Mozilla/5.0"}) as resp:
            if resp.status != 200:
                raise ValueError(f"HTTP {resp.status}")
            html = await resp.text()

        soup = BeautifulSoup(html, "html.parser")
        title = soup.find("title")

        images = []
        for img in soup.select("img[src*='http']"):
            src = img.get("src")
            if src:
                images.append(ImageContent(src, source_url=src))

        desc = soup.find("meta", attrs={"name": "description"})
        text = desc.get("content", "") if desc else ""

        return ParseResult(
            platform=self.platform,
            title=title.text[:100] if title else "",
            text=text[:500],
            url=url,
            contents=images[:9],
        )


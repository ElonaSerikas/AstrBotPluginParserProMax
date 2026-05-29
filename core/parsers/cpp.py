import json
import re

from bs4 import BeautifulSoup, Tag

from ..config import PluginConfig
from ..cookie import CookieJar
from ..data import Platform, ParseResult
from ..download import Downloader
from .base import BaseParser, ParseException, handle


class CPPParser(BaseParser):
    """CPP无差别同人站解析器"""

    platform = Platform(name="cpp", display_name="CPP无差别同人站")

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.mycfg = config.parser.cpp
        self.cookiejar = CookieJar(config, self.mycfg, domain="allcpp.cn")
        if self.cookiejar.cookies_str:
            self.headers["cookie"] = self.cookiejar.cookies_str

    @handle("cpp", r"allcpp\.cn/(?:d|p)/(\d+)\.do\/?(?:\?.*)?$")
    async def _parse_work(self, matched):
        work_id = matched.group(1)
        path_type = "d" if "allcpp.cn/d/" in matched.group(0) else "p"
        url = f"https://www.allcpp.cn/{path_type}/{work_id}.do"

        async with self.session.get(url, headers=self.headers) as resp:
            if resp.status != 200:
                raise ParseException(f"HTTP {resp.status}")
            html = await resp.text()

        soup = BeautifulSoup(html, "html.parser")

        # 标题
        title_tag = soup.find("title")
        title = title_tag.get_text(strip=True)[:100] if title_tag else None

        # 文本
        desc = soup.find("meta", attrs={"name": "description"})
        text = desc.get("content", "").strip()[:500] if desc else None

        # 图片
        image_urls = []
        for img in soup.select("img[src*='http']"):
            src = img.get("src", "").strip()
            if src:
                image_urls.append(src)
        contents = self.create_image_contents(image_urls[:9]) if image_urls else []

        # 作者信息（从页面或 JSON-LD 提取）
        author_name = None
        author_avatar = None
        author_uid = None

        # 尝试从 __NEXT_DATA__ 或 script 提取
        json_match = re.search(
            r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S
        )
        if json_match:
            try:
                data = json.loads(json_match.group(1))
                props = data.get("props", {}).get("pageProps", {})
                user = props.get("user", props.get("author", {}))
                if user:
                    author_name = user.get("nickname", user.get("name"))
                    author_avatar = user.get("avatar", user.get("avatarUrl"))
                    author_uid = str(user.get("uid", user.get("id", ""))) or None
            except (json.JSONDecodeError, KeyError):
                pass

        author = self.create_author(
            name=author_name or "CPP用户",
            avatar=author_avatar,
            uid=author_uid,
        ) if author_name else None

        return self.result(
            title=title,
            text=text,
            url=url,
            author=author,
            contents=contents,
        )


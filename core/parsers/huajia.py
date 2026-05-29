import json
import re

from bs4 import BeautifulSoup

from ..config import PluginConfig
from ..cookie import CookieJar
from ..data import Platform, ParseResult
from ..download import Downloader
from .base import BaseParser, ParseException, handle


class HuajiaParser(BaseParser):
    """画加（网易）解析器"""

    platform = Platform(name="huajia", display_name="画加")

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.mycfg = config.parser.huajia
        self.cookiejar = CookieJar(config, self.mycfg, domain="huajia.163.com")
        if self.cookiejar.cookies_str:
            self.headers["cookie"] = self.cookiejar.cookies_str

    @handle("huajia", r"huajia\.163\.com/\S+")
    async def _parse_page(self, matched):
        url = matched.group(0)
        if not url.startswith("http"):
            url = "https://" + url

        try:
            async with self.session.get(url, headers=self.headers) as resp:
                if resp.status != 200:
                    raise ParseException(f"HTTP {resp.status}")
                html = await resp.text()
        except ParseException:
            return self.result(
                text="画加页面解析暂不支持此链接格式",
                url=url,
            )

        soup = BeautifulSoup(html, "html.parser")

        # 标题
        title_tag = soup.find("title")
        title = title_tag.get_text(strip=True)[:100] if title_tag else None

        # 文本
        desc_tag = soup.find("meta", attrs={"name": "description"})
        text = desc_tag.get("content", "").strip()[:500] if desc_tag else None

        # 图片
        image_urls = []
        for img in soup.select("img"):
            src = img.get("src") or img.get("data-src") or ""
            if src.startswith("http"):
                image_urls.append(src)
        contents = self.create_image_contents(image_urls[:9]) if image_urls else []

        # 作者信息（从 __NEXT_DATA__ 或 script 提取）
        author_name = None
        author_avatar = None
        author_uid = None

        json_match = re.search(
            r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S
        )
        if json_match:
            try:
                data = json.loads(json_match.group(1))
                props = data.get("props", {}).get("pageProps", {})
                user = props.get("artist", props.get("user", props.get("author", {})))
                if user:
                    author_name = user.get("nickname", user.get("name"))
                    author_avatar = user.get("avatar", user.get("avatarUrl"))
                    author_uid = str(user.get("id", "")) or None
            except (json.JSONDecodeError, KeyError):
                pass

        author = self.create_author(
            name=author_name or "画加用户",
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


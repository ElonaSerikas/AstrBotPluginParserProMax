import json
import re

from ..config import PluginConfig
from ..cookie import CookieJar
from ..data import Platform, ParseResult
from ..download import Downloader
from .base import BaseParser, ParseException, handle


class MihuashiParser(BaseParser):
    """米画师解析器"""

    platform = Platform(name="mihuashi", display_name="米画师")

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.mycfg = config.parser.mihuashi
        self.cookiejar = CookieJar(config, self.mycfg, domain="mihuashi.com")
        if self.cookiejar.cookies_str:
            self.headers["cookie"] = self.cookiejar.cookies_str

    @handle("mihuashi", r"mihuashi\.com/artworks/(\d+)\/?(?:\?.*)?$")
    async def _parse_artwork(self, matched):
        artwork_id = matched.group(1)
        url = f"https://www.mihuashi.com/artworks/{artwork_id}"

        async with self.session.get(url, headers=self.headers) as resp:
            if resp.status != 200:
                raise ParseException(f"HTTP {resp.status}")
            html = await resp.text()

        # OG meta 提取
        title = ""
        text = ""
        images = []

        title_match = re.search(
            r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', html
        )
        if title_match:
            title = title_match.group(1)

        desc_match = re.search(
            r'<meta[^>]+property="og:description"[^>]+content="([^"]+)"', html
        )
        if desc_match:
            text = desc_match.group(1)[:500]

        for img_match in re.finditer(
            r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', html
        ):
            img_url = img_match.group(1)
            if img_url.startswith("http"):
                images.extend(self.create_image_contents([img_url]))

        # 尝试从 __NEXT_DATA__ 或 script 提取作者和时间戳
        author_name = None
        author_avatar = None
        author_uid = None
        author_desc = None
        timestamp = None

        # JSON-LD 或嵌入数据
        json_match = re.search(
            r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S
        )
        if json_match:
            try:
                data = json.loads(json_match.group(1))
                props = data.get("props", {}).get("pageProps", {})
                artwork = props.get("artwork", {})
                if artwork:
                    artist = artwork.get("artist", artwork.get("user", {}))
                    if artist:
                        author_name = artist.get("nickname", artist.get("name"))
                        author_avatar = artist.get("avatar", artist.get("avatarUrl"))
                        author_uid = str(artist.get("id", "")) or None
                        author_desc = artist.get("description", artist.get("bio"))
                    timestamp_raw = artwork.get("createdAt", artwork.get("createTime"))
                    if isinstance(timestamp_raw, (int, float)):
                        timestamp = int(timestamp_raw) if timestamp_raw > 1e12 else int(timestamp_raw)
                    elif isinstance(timestamp_raw, str):
                        import time
                        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
                            try:
                                timestamp = int(time.mktime(time.strptime(timestamp_raw[:19], fmt)))
                                break
                            except ValueError:
                                continue
            except (json.JSONDecodeError, KeyError):
                pass

        # 回退：正则提取作者
        if not author_name:
            author_match = re.search(
                r'<meta[^>]+name="author"[^>]+content="([^"]+)"', html
            )
            if author_match:
                author_name = author_match.group(1)

        author = self.create_author(
            name=author_name or "米画师画师",
            avatar=author_avatar,
            uid=author_uid,
            description=author_desc,
        )

        return self.result(
            title=title or None,
            text=text or None,
            url=url,
            author=author,
            contents=images,
            timestamp=timestamp,
        )


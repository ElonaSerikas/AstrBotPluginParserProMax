import re
from time import mktime, strptime

from bs4 import BeautifulSoup, Tag

from ..config import PluginConfig
from ..cookie import CookieJar
from ..data import Platform, ParseResult
from ..download import Downloader
from .base import BaseParser, ParseException, handle


class LofterParser(BaseParser):
    """LOFTER（网易乐乎）解析器"""

    platform = Platform(name="lofter", display_name="LOFTER")

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.mycfg = config.parser.lofter
        self.cookiejar = CookieJar(config, self.mycfg, domain="lofter.com")
        if self.cookiejar.cookies_str:
            self.headers["cookie"] = self.cookiejar.cookies_str

    @handle("lofter", r"lofter\.com/lpost/(\w+)_(\w+)\/?(?:\?.*)?$")
    async def _parse_lpost(self, matched):
        user_id = matched.group(1)
        post_id = matched.group(2)
        url = f"https://www.lofter.com/lpost/{user_id}_{post_id}"

        async with self.session.get(url, headers=self.headers) as resp:
            if resp.status != 200:
                raise ParseException(f"HTTP {resp.status}")
            html = await resp.text()

        soup = BeautifulSoup(html, "html.parser")

        # 标题
        title_tag = soup.find("title")
        title = title_tag.get_text(strip=True)[:100] if title_tag else None

        # 正文文本
        text = None
        desc_tag = soup.find("meta", attrs={"name": "description"})
        if desc_tag:
            text = desc_tag.get("content", "").strip()[:500]

        # 作者信息
        author_name = None
        author_avatar = None
        author_desc = None

        # 从 og:tags 或页面元素提取作者
        author_tag = soup.find("meta", attrs={"name": "author"})
        if author_tag:
            author_name = author_tag.get("content", "").strip()

        # 尝试从页面 JS 提取更多作者信息
        if not author_name:
            # 从页面标题提取 (格式通常为 "标题 - 作者名 - LOFTER")
            if title_tag:
                parts = title_tag.get_text(strip=True).split(" - ")
                if len(parts) >= 2:
                    author_name = parts[-2].strip()

        # 提取头像
        avatar_tag = soup.select_one('.avatar img, .author-avatar img, img.avatar')
        if avatar_tag:
            author_avatar = avatar_tag.get("src", "").strip() or None

        # 提取签名
        desc_tag = soup.select_one('.author-desc, .author-description, .desc')
        if desc_tag:
            author_desc = desc_tag.get_text(strip=True)[:200] or None

        # 图片列表
        image_urls = []
        for img in soup.select('img[src*="http"]'):
            src = img.get("src", "").strip()
            if src.startswith("http") and not src.endswith(".ico") and "avatar" not in src.lower():
                image_urls.append(src)

        contents = self.create_image_contents(image_urls) if image_urls else []

        # 时间戳
        timestamp = None
        time_tag = soup.select_one('.time, .post-time, time, .date')
        if time_tag:
            time_text = time_tag.get_text(strip=True)
            for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d"):
                try:
                    timestamp = int(mktime(strptime(time_text, fmt)))
                    break
                except ValueError:
                    continue

        author = self.create_author(
            name=author_name or "LOFTER用户",
            avatar=author_avatar,
            uid=user_id,
            description=author_desc,
        ) if author_name else None

        return self.result(
            title=title,
            text=text,
            url=url,
            author=author,
            contents=contents,
            timestamp=timestamp,
            extra={"uid": str(user_id or ""), "post_id": url.split("/")[-1] if "/" in url else ""},
        )


"""Facebook 帖子解析器"""

import json
import re
import time
from typing import ClassVar

from bs4 import BeautifulSoup, Tag

from astrbot.api import logger

from ..config import PluginConfig
from ..cookie import CookieJar
from ..download import Downloader
from ..exception import ParseException
from .base import BaseParser, Platform, handle


class FacebookParser(BaseParser):
    platform: ClassVar[Platform] = Platform(name="facebook", display_name="Facebook")

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.mycfg = config.parser.facebook
        self.cookiejar = CookieJar(config, self.mycfg, domain="facebook.com")
        # 使用移动端 UA 降低反爬风险
        self.fb_headers = {
            "User-Agent": (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                "Version/18.5 Mobile/15E148 Safari/604.1"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8",
            "Accept-Encoding": "gzip, deflate",
            "Referer": "https://www.facebook.com/",
        }
        if self.cookiejar.cookies_str:
            self.fb_headers["Cookie"] = self.cookiejar.cookies_str

    # 常见 Facebook URL 模式
    # https://www.facebook.com/username/posts/123456
    # https://www.facebook.com/photo.php?fbid=123456
    # https://www.facebook.com/story.php?id=123456
    # https://www.facebook.com/share/p/abc123
    # https://fb.com/username/posts/123456
    @handle(
        "facebook.com",
        r"(?:www\.|web\.|m\.)?facebook\.com/(?P<user>[^/]+)/(?:posts|permalink)/(?P<post_id>\d+)",
    )
    @handle(
        "facebook.com",
        r"(?:www\.|web\.|m\.)?facebook\.com/photo(?:\.php|\?)\??fbid=(?P<post_id>\d+)",
    )
    @handle(
        "facebook.com",
        r"(?:www\.|web\.|m\.)?facebook\.com/story\.php\??id=(?P<post_id>\d+)",
    )
    @handle(
        "facebook.com",
        r"(?:www\.|web\.|m\.)?facebook\.com/share/p/(?P<share_id>[a-zA-Z0-9_-]+)",
    )
    @handle(
        "fb.com",
        r"(?:www\.)?fb\.com/(?P<user>[^/]+)/(?:posts|permalink)/(?P<post_id>\d+)",
    )
    async def _parse(self, searched: re.Match[str]):
        post_id = searched.group("post_id") if "post_id" in searched.groupdict() else None
        share_id = searched.group("share_id") if "share_id" in searched.groupdict() else None
        user = searched.group("user") if "user" in searched.groupdict() else None

        # 优先使用移动端 URL
        if post_id:
            url = f"https://m.facebook.com/story.php?id={post_id}"
        elif share_id:
            url = f"https://m.facebook.com/share/p/{share_id}"
        elif user and post_id:
            url = f"https://m.facebook.com/{user}/posts/{post_id}"
        else:
            url = f"https://{searched.group(0)}"

        logger.debug(f"[Facebook] 解析 URL: {url}")

        async with self.session.get(url, headers=self.fb_headers) as resp:
            if resp.status >= 400:
                raise ParseException(f"Facebook 页面获取失败: HTTP {resp.status}")
            html = await resp.text()
            # 更新 cookies
            set_cookies = resp.headers.getall("Set-Cookie", [])
            self.cookiejar.update_from_response(set_cookies)
            if self.cookiejar.cookies_str:
                self.fb_headers["Cookie"] = self.cookiejar.cookies_str

        # 从 HTML 中提取数据
        author_name, author_avatar, author_uid, author_desc = self._extract_author(html)
        title, text, image_urls, video_url, timestamp = self._extract_content(html)

        # 如果移动端没拿到数据，尝试桌面端
        if not text and not video_url and not image_urls:
            logger.debug("[Facebook] 移动端未获取到数据，尝试桌面端")
            desktop_url = url.replace("m.facebook.com", "www.facebook.com")
            async with self.session.get(desktop_url, headers=self.fb_headers) as resp:
                if resp.status < 400:
                    html = await resp.text()
                    author_name, author_avatar, author_uid, author_desc = self._extract_author(html)
                    title, text, image_urls, video_url, timestamp = self._extract_content(html)

        if not text and not video_url and not image_urls:
            raise ParseException("Facebook 帖子内容为空或无法解析（可能需要登录）")

        # 构建结果
        author = None
        if author_name:
            author = self.create_author(
                author_name,
                avatar=author_avatar,
                uid=author_uid,
                description=author_desc,
            )

        contents = []
        if video_url:
            contents.append(self.create_video_content(video_url, headers=self.fb_headers))
        if image_urls:
            contents.extend(self.create_image_contents(image_urls, headers=self.fb_headers))

        # 使用原始 URL 作为展示链接
        display_url = searched.group(0)
        if not display_url.startswith("http"):
            display_url = f"https://{display_url}"

        return self.result(
            title=title,
            text=text,
            url=display_url,
            author=author,
            timestamp=timestamp,
            contents=contents,
            extra={"uid": str(author_uid or ""), "post_id": display_url.split("/")[-1] if "/" in display_url else ""},
        )

    def _extract_author(
        self, html: str
    ) -> tuple[str | None, str | None, str | None, str | None]:
        """从 HTML 中提取作者信息"""
        soup = BeautifulSoup(html, "html.parser")
        author_name = None
        author_avatar = None
        author_uid = None
        author_desc = None

        # 方法 1: 从 Open Graph meta tags 提取
        og_title = soup.find("meta", property="og:title")
        if og_title and isinstance(og_title, Tag):
            content = og_title.get("content", "")
            if isinstance(content, str) and content:
                # og:title 通常是 "Author Name - 帖子内容" 的格式
                parts = content.split(" - ", 1)
                if len(parts) >= 2:
                    author_name = parts[0].strip()

        # 方法 2: 从 JSON-LD 提取
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
                if isinstance(data, dict):
                    author_obj = data.get("author")
                    if isinstance(author_obj, dict):
                        author_name = author_name or author_obj.get("name")
                        author_avatar = author_obj.get("image", {}).get("url") if isinstance(author_obj.get("image"), dict) else author_obj.get("image")
                        author_uid = str(author_obj.get("identifier")) if author_obj.get("identifier") else None
                    elif isinstance(author_obj, list) and author_obj:
                        first = author_obj[0]
                        if isinstance(first, dict):
                            author_name = author_name or first.get("name")
                            author_avatar = first.get("image", {}).get("url") if isinstance(first.get("image"), dict) else first.get("image")
            except (json.JSONDecodeError, KeyError, TypeError):
                pass

        # 方法 3: 从移动端 HTML 结构提取
        # 移动端的作者通常在 header 或 h3/h4 标签中
        if not author_name:
            # 尝试从 aria-label 或 data-testid 提取
            for tag in soup.find_all(["h3", "h4", "strong", "span"]):
                text = tag.get_text(strip=True)
                # 作者名通常较短且不是通用文本
                if text and 1 < len(text) < 50 and not any(
                    skip in text.lower()
                    for skip in ["like", "comment", "share", "reply", "ago", "http"]
                ):
                    # 检查是否有链接（通常是作者主页链接）
                    link = tag.find("a")
                    if link and isinstance(link, Tag):
                        href = str(link.get("href", ""))
                        if "/profile/" in href or (href.startswith("/") and not any(x in href for x in ["/photo", "/story", "/posts"])):
                            author_name = text
                            # 从 profile URL 提取 UID
                            uid_match = re.search(r"id=(\d+)", href)
                            if uid_match:
                                author_uid = uid_match.group(1)
                            break

        # 提取头像
        if not author_avatar:
            # 移动端头像通常在 profile picture 附近
            for img in soup.find_all("img"):
                if isinstance(img, Tag):
                    src = str(img.get("src", ""))
                    alt = str(img.get("alt", ""))
                    # Facebook 头像 URL 特征
                    if "profile" in src.lower() or (
                        author_name and author_name.lower() in alt.lower()
                    ):
                        if src.startswith("http"):
                            author_avatar = src
                            break

        return author_name, author_avatar, author_uid, author_desc

    def _extract_content(
        self, html: str
    ) -> tuple[str | None, str | None, list[str], str | None, int | None]:
        """从 HTML 中提取帖子内容

        Returns: (title, text, image_urls, video_url, timestamp)
        """
        soup = BeautifulSoup(html, "html.parser")
        title = None
        text = None
        image_urls: list[str] = []
        video_url = None
        timestamp = None

        # 方法 1: 从 Open Graph tags 提取
        og_title = soup.find("meta", property="og:title")
        og_desc = soup.find("meta", property="og:description")
        og_image = soup.find("meta", property="og:image")
        og_video = soup.find("meta", property="og:video")

        if og_title and isinstance(og_title, Tag):
            content = og_title.get("content", "")
            if isinstance(content, str) and content:
                parts = content.split(" - ", 1)
                title = parts[1].strip() if len(parts) >= 2 else parts[0].strip()

        if og_desc and isinstance(og_desc, Tag):
            content = og_desc.get("content", "")
            if isinstance(content, str) and content:
                text = content

        if og_image and isinstance(og_image, Tag):
            src = str(og_image.get("content", ""))
            if src.startswith("http"):
                image_urls.append(src)

        if og_video and isinstance(og_video, Tag):
            src = str(og_video.get("content", ""))
            if src.startswith("http"):
                video_url = src

        # 方法 2: 从 JSON-LD 提取
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
                if not isinstance(data, dict):
                    continue

                # 帖子正文
                if not text:
                    article_body = data.get("articleBody", data.get("text", ""))
                    if isinstance(article_body, str) and article_body:
                        text = article_body

                # 图片
                if not image_urls:
                    image = data.get("image")
                    if isinstance(image, str):
                        image_urls.append(image)
                    elif isinstance(image, list):
                        for img in image:
                            if isinstance(img, str):
                                image_urls.append(img)
                            elif isinstance(img, dict) and img.get("url"):
                                image_urls.append(img["url"])

                # 视频
                if not video_url:
                    video = data.get("video")
                    if isinstance(video, dict):
                        video_url = video.get("contentUrl") or video.get("embedUrl")
                    elif isinstance(video, str):
                        video_url = video

                # 时间
                if not timestamp:
                    date_str = data.get("datePublished", data.get("dateCreated", ""))
                    if isinstance(date_str, str) and date_str:
                        timestamp = self._parse_timestamp(date_str)

            except (json.JSONDecodeError, KeyError, TypeError):
                pass

        # 方法 3: 从移动端 HTML 结构提取文本
        if not text:
            # 移动端帖子正文通常在 data-testid="post-message" 或特定 div 中
            post_msg = soup.find(attrs={"data-testid": "post-message"})
            if post_msg and isinstance(post_msg, Tag):
                text = post_msg.get_text("\n", strip=True)

            # 备用: 从 story body 提取
            if not text:
                story_body = soup.find(id="story_body_container")
                if story_body and isinstance(story_body, Tag):
                    text = story_body.get_text("\n", strip=True)

        # 方法 4: 提取图片（从 img 标签）
        if not image_urls:
            for img in soup.find_all("img"):
                if isinstance(img, Tag):
                    src = str(img.get("src", ""))
                    # Facebook 图片 CDN 特征
                    if any(
                        domain in src
                        for domain in [
                            "scontent.",
                            "fbcdn.",
                            "external.",
                            "lookaside.",
                        ]
                    ):
                        # 过滤掉小图（头像、图标等）
                        width = img.get("width", "")
                        height = img.get("height", "")
                        try:
                            w = int(width) if width else 0
                            h = int(height) if height else 0
                            if w > 200 or h > 200 or (w == 0 and h == 0):
                                image_urls.append(src)
                        except (ValueError, TypeError):
                            image_urls.append(src)

        # 方法 5: 提取视频（从 video 标签或 source 标签）
        if not video_url:
            video_tag = soup.find("video")
            if video_tag and isinstance(video_tag, Tag):
                video_url = video_tag.get("src", "")
                if not video_url:
                    source = video_tag.find("source")
                    if source and isinstance(source, Tag):
                        video_url = source.get("src", "")
                if video_url and not str(video_url).startswith("http"):
                    video_url = None

        # 方法 6: 提取时间戳
        if not timestamp:
            # 从 time 标签提取
            time_tag = soup.find("time")
            if time_tag and isinstance(time_tag, Tag):
                dt = time_tag.get("datetime", "")
                if isinstance(dt, str) and dt:
                    timestamp = self._parse_timestamp(dt)

            # 从 abbr 标签提取（移动端常见）
            if not timestamp:
                abbr_tag = soup.find("abbr")
                if abbr_tag and isinstance(abbr_tag, Tag):
                    data_utime = abbr_tag.get("data-utime", "")
                    if data_utime:
                        try:
                            timestamp = int(data_utime)
                        except (ValueError, TypeError):
                            pass

        # 清理文本
        if text:
            text = self._clean_text(text)
            # 如果标题和正文相同，去掉标题
            if title and title == text:
                title = None

        # 去重图片
        image_urls = list(dict.fromkeys(image_urls))

        return title, text, image_urls, video_url, timestamp

    @staticmethod
    def _parse_timestamp(date_str: str) -> int | None:
        """解析各种格式的时间字符串为 Unix 时间戳"""
        formats = [
            "%Y-%m-%dT%H:%M:%S%z",       # ISO 8601: 2024-01-01T12:00:00+0800
            "%Y-%m-%dT%H:%M:%SZ",          # ISO 8601 UTC: 2024-01-01T12:00:00Z
            "%Y-%m-%dT%H:%M:%S.%f%z",     # ISO 8601 with microseconds
            "%Y-%m-%d %H:%M:%S",           # 2024-01-01 12:00:00
            "%Y-%m-%d",                     # 2024-01-01
        ]
        for fmt in formats:
            try:
                dt = time.strptime(date_str, fmt)
                return int(time.mktime(dt))
            except ValueError:
                continue
        return None

    @staticmethod
    def _clean_text(text: str, max_length: int = 2000) -> str:
        """清理帖子文本"""
        # 移除多余空白
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r"[ \t]+", " ", text)
        text = text.strip()
        if len(text) > max_length:
            text = text[:max_length] + "..."
        return text

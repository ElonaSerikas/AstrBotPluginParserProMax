"""通用网页截图解析器 — 对没有定制 parser 的 URL，用 Playwright 截图并返回 ParseResult。"""

import asyncio
import hashlib
import ipaddress
import json
import re
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from astrbot.api import logger

from ..data import Author, ImageContent, ParseResult, Platform

# 复用 render_html 的共享浏览器实例
from ..render_html.engine import _get_browser

# 提取第一个 URL
_URL_RE = re.compile(r"https?://\S+")

# SSRF 防护：禁止访问的内网地址段
_BLOCKED_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]


def _is_safe_url(url: str) -> bool:
    """检查 URL 是否安全（非内网、非 file 协议）"""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False
        hostname = parsed.hostname
        if not hostname:
            return False
        # 检查是否为内网 IP
        try:
            ip = ipaddress.ip_address(hostname)
            return not any(ip in net for net in _BLOCKED_NETWORKS)
        except ValueError:
            # 域名而非 IP — 允许通过（DNS 解析不在这里做）
            # 但阻止 localhost 等明显内网域名
            blocked_domains = ("localhost", "local", "internal", "intranet")
            return not any(hostname.endswith(d) for d in blocked_domains)
    except Exception:
        return False


def extract_first_url(text: str) -> str | None:
    """从文本中提取第一个安全的 URL"""
    for m in _URL_RE.finditer(text):
        url = m.group(0)
        if _is_safe_url(url):
            return url
    return None


class GenericScreenshotParser:
    """通用网页截图解析器

    不走 BaseParser 注册体系，作为 fallback 在 URL 匹配循环外调用。
    """

    # 截图参数
    VIEWPORT_WIDTH = 1280
    VIEWPORT_HEIGHT = 800
    MAX_SCROLL_ROUNDS = 30  # 最多滚动次数（防止无限滚动网站卡死）
    SCROLL_PAUSE = 0.5  # 每次滚动后等待秒数（等待懒加载内容，优化速度）
    NAV_TIMEOUT = 15_000  # 页面加载超时（ms）

    def __init__(self, cache_dir: Path | None = None):
        self._cache_dir = cache_dir or Path(tempfile.gettempdir())

    async def _download_favicon(self, page, url: str) -> Path | None:
        """下载 favicon 并保存为本地文件"""
        try:
            favicon_url = await page.evaluate(
                """() => {
                    const link = document.querySelector('link[rel="icon"], link[rel="shortcut icon"], link[rel="apple-touch-icon"]');
                    return link ? link.getAttribute('href') || '' : '';
                }"""
            )
            if not favicon_url:
                # 尝试默认 favicon
                parsed = urlparse(url)
                favicon_url = f"{parsed.scheme}://{parsed.netloc}/favicon.ico"

            # 将相对 URL 转为绝对 URL
            if favicon_url.startswith("//"):
                favicon_url = "https:" + favicon_url
            elif favicon_url.startswith("/"):
                parsed = urlparse(url)
                favicon_url = f"{parsed.scheme}://{parsed.netloc}{favicon_url}"

            # 用 page 的网络上下文下载 favicon
            favicon_bytes = await page.evaluate(
                """async (faviconUrl) => {
                    try {
                        const resp = await fetch(faviconUrl);
                        if (!resp.ok) return null;
                        const buf = await resp.arrayBuffer();
                        return Array.from(new Uint8Array(buf));
                    } catch { return null; }
                }""",
                favicon_url,
            )
            if not favicon_bytes:
                return None

            url_hash = hashlib.md5(url.encode()).hexdigest()[:12]
            favicon_path = self._cache_dir / f"favicon_{url_hash}.ico"
            favicon_path.write_bytes(bytes(favicon_bytes))
            return favicon_path if favicon_path.stat().st_size > 0 else None
        except Exception:
            return None

    async def parse(self, url: str) -> ParseResult | None:
        """访问 URL，截取页面截图，提取元信息，返回 ParseResult。

        Returns:
            ParseResult 成功时返回，失败返回 None。
        """
        try:
            browser = await _get_browser()
        except Exception as e:
            logger.error(f"[通用截图] 获取浏览器实例失败: {e}")
            return None

        page = None
        try:
            page = await browser.new_page(
                viewport={
                    "width": self.VIEWPORT_WIDTH,
                    "height": self.VIEWPORT_HEIGHT,
                },
                device_scale_factor=1.5,
            )

            # 访问页面
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=self.NAV_TIMEOUT)
            except Exception as e:
                logger.warning(f"[通用截图] 页面加载超时或失败({url}): {e}")
                # 即使超时也尝试截取已加载的部分

            # 等待内容渲染
            await asyncio.sleep(0.5)

            # 提取元信息
            title = await page.title()
            meta_desc = ""
            og_title = ""
            og_image = ""
            og_site_name = ""
            article_text = ""
            page_images = []
            try:
                meta_data = await page.evaluate(
                    """() => {
                        const getMeta = (prop) => {
                            const el = document.querySelector(`meta[property="${prop}"], meta[name="${prop}"]`);
                            return el ? el.getAttribute('content') || '' : '';
                        };
                        // 提取正文摘要（取前 500 字）
                        let bodyText = '';
                        const article = document.querySelector('article, .article, .content, .post-content, .entry-content, main');
                        if (article) {
                            bodyText = article.innerText.substring(0, 500);
                        }
                        // 提取页面主要图片
                        const imgs = Array.from(document.querySelectorAll('article img, .content img, main img'))
                            .filter(img => img.naturalWidth > 200 && img.naturalHeight > 150)
                            .slice(0, 5)
                            .map(img => img.src || img.dataset.src || '');
                        return {
                            description: getMeta('og:description') || getMeta('description'),
                            ogTitle: getMeta('og:title'),
                            ogImage: getMeta('og:image'),
                            ogSiteName: getMeta('og:site_name'),
                            articleText: bodyText,
                            images: imgs.filter(Boolean),
                        };
                    }"""
                )
                meta_desc = meta_data.get("description", "")
                og_title = meta_data.get("ogTitle", "")
                og_image = meta_data.get("ogImage", "")
                og_site_name = meta_data.get("ogSiteName", "")
                article_text = meta_data.get("articleText", "")
                page_images = meta_data.get("images", [])
            except Exception:
                pass

            # 下载 favicon
            favicon_path = await self._download_favicon(page, url)

            # 滚动到底部（触发懒加载内容），然后截全页
            last_height = await page.evaluate("() => document.body.scrollHeight")
            for _ in range(self.MAX_SCROLL_ROUNDS):
                await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
                await asyncio.sleep(self.SCROLL_PAUSE)
                new_height = await page.evaluate("() => document.body.scrollHeight")
                if new_height == last_height:
                    break
                last_height = new_height

            # 滚回顶部再截全页，确保渲染完整
            await page.evaluate("() => window.scrollTo(0, 0)")
            await asyncio.sleep(1.5)  # 等待页面渲染完成

            # 截图到内存
            screenshot_bytes = await page.screenshot(full_page=True, type="png")

            # 裁剪过高的截图（最大高度 8000px）
            max_height = 8000
            from PIL import Image as PILImage
            import io

            img = PILImage.open(io.BytesIO(screenshot_bytes))
            if img.height > max_height:
                img = img.crop((0, 0, img.width, max_height))
            if img.width > 2400:
                ratio = 2400 / img.width
                img = img.resize((2400, int(img.height * ratio)), PILImage.Resampling.LANCZOS)

            # 保存截图
            url_hash = hashlib.md5(url.encode()).hexdigest()[:12]
            screenshot_path = self._cache_dir / f"generic_{url_hash}.png"
            img.save(str(screenshot_path), "PNG")

            logger.debug(f"[通用截图] 截图成功: {url}, 尺寸={img.width}x{img.height}")

            # 解析域名作为作者名
            parsed_url = urlparse(url)
            domain = parsed_url.netloc or parsed_url.hostname or "网页"
            # 去掉 www. 前缀
            if domain.startswith("www."):
                domain = domain[4:]

            # 构建 Author
            author = Author(
                name=domain,
                avatar=favicon_path,
            )

            # 构建 ParseResult
            platform_name = og_site_name or domain
            platform = Platform(name="generic", display_name=platform_name)
            display_title = og_title or title or domain

            # 截图作为内容
            contents = [ImageContent(path_task=screenshot_path)]

            # 组装正文：摘要 + 文章内容
            text_parts = []
            if meta_desc:
                text_parts.append(meta_desc)
            if article_text and article_text != meta_desc:
                text_parts.append(article_text)
            combined_text = "\n".join(text_parts) if text_parts else None

            return ParseResult(
                platform=platform,
                author=author,
                title=display_title,
                text=combined_text,
                url=url,
                contents=contents,
                extra={
                    "og_image": og_image,
                    "og_site_name": og_site_name,
                    "domain": domain,
                },
                page_type="generic",
            )

        except Exception as e:
            logger.error(f"[通用截图] 解析失败({url}): {e}", exc_info=True)
            return None
        finally:
            if page:
                try:
                    await page.close()
                except Exception:
                    pass

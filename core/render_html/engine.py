"""
HTML 渲染引擎 - 使用本地 Playwright 渲染 HTML 模板为图片。

不依赖远程 T2I 服务，完全本地运行，无网络波动。
需要：pip install playwright && python -m playwright install chromium
"""

import asyncio
import os
import tempfile
from pathlib import Path
from typing import Optional

from jinja2 import Template

from astrbot.api import logger

from .models import RenderPayload
from .constants import (
    CARD_TEMPLATES,
    DEFAULT_TEMPLATE,
    MAX_ATTEMPTS,
    RETRY_DELAY,
)

# 全局 Playwright 实例（懒加载，复用）
_browser = None
_pw_instance = None
_browser_lock = asyncio.Lock()


async def _get_browser():
    """获取或创建全局 Playwright 浏览器实例"""
    global _browser, _pw_instance
    if _browser is not None and _browser.is_connected():
        return _browser
    async with _browser_lock:
        if _browser is not None and _browser.is_connected():
            return _browser
        try:
            # 清理旧的 playwright 实例（浏览器已断开连接时）
            if _pw_instance is not None:
                try:
                    await _pw_instance.stop()
                except Exception:
                    pass
            from playwright.async_api import async_playwright
            _pw_instance = await async_playwright().start()
            _browser = await _pw_instance.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox"],
            )
            logger.debug("[HTML渲染] Playwright 浏览器启动成功")
        except Exception as e:
            logger.error(f"[HTML渲染] Playwright 浏览器启动失败: {e}")
            _browser = None
            _pw_instance = None
            raise
    return _browser


class HtmlCardRenderer:
    """
    HTML 卡片渲染器 - 本地 Playwright 渲染。

    将 RenderPayload + Jinja2 模板渲染为图片，完全本地运行。
    """

    def __init__(self, star=None, config=None):
        self.star = star  # 保留以兼容接口，但不使用 star.html_render
        self.cfg = config
        self._templates: dict[str, str] = {}

    def _load_template(self, style: str) -> str:
        """惰性加载单个模板"""
        if style not in self._templates:
            tmpl_dir = Path(__file__).parent.parent / "templates"
            if not tmpl_dir.exists():
                logger.warning(f"[HTML渲染] 模板目录不存在: {tmpl_dir}")
                return ""
            for f in tmpl_dir.rglob("*.html"):
                if f.stem == style:
                    try:
                        self._templates[style] = f.read_text(encoding="utf-8")
                    except Exception as e:
                        logger.error(f"[HTML渲染] 加载模板失败 {f.name}: {e}")
                    break
        return self._templates.get(style, "")

    def get_template(self, style: str) -> str:
        """获取指定风格的模板 HTML，惰性加载"""
        tmpl = self._load_template(style)
        if tmpl:
            return tmpl
        fallback = self._load_template(DEFAULT_TEMPLATE)
        if fallback:
            return fallback
        raise RuntimeError("没有可用的 HTML 模板")

    async def render_card(
        self,
        payload: RenderPayload,
        style: str = "universal_card",
    ) -> Optional[str]:
        """
        使用本地 Playwright 将 HTML 模板渲染为图片。

        Returns:
            图片文件路径，失败返回 None
        """
        logger.debug(f"[HTML渲染] 开始渲染: style={style}, name={payload.name!r}, images={len(payload.image_urls)}")
        try:
            tmpl_str = self.get_template(style)
        except RuntimeError as e:
            logger.error(f"[HTML渲染] {e}")
            return None

        context = payload.to_template_context()

        # 用 Jinja2 渲染 HTML
        try:
            jinja_tmpl = Template(tmpl_str)
            html = jinja_tmpl.render(**context)
        except Exception as e:
            logger.error(f"[HTML渲染] Jinja2 渲染失败: {e}")
            return None

        # Playwright 渲染
        for attempt in range(MAX_ATTEMPTS):
            try:
                browser = await _get_browser()
                page = await browser.new_page(
                    viewport={"width": 1920, "height": 1080},
                    device_scale_factor=2.0,  # 高清渲染（2x 最高画质）
                )
                try:
                    # 使用 domcontentloaded 避免外部图片加载超时
                    await page.set_content(html, wait_until="domcontentloaded")
                    # 等待图片加载完成（最多 15 秒），失败的图片自动隐藏
                    await page.evaluate("""() => {
                        return new Promise(resolve => {
                            const imgs = Array.from(document.querySelectorAll('img'));
                            if (imgs.length === 0) return resolve();
                            let done = 0;
                            const total = imgs.length;
                            const check = () => { if (++done >= total) resolve(); };
                            imgs.forEach(img => {
                                if (img.complete) return check();
                                img.addEventListener('load', check);
                                img.addEventListener('error', () => {
                                    img.style.display = 'none';
                                    check();
                                });
                            });
                            setTimeout(resolve, 15000);
                        });
                    }""")
                    # 获取卡片实际尺寸，精确设置视口
                    dim = await page.evaluate(
                        "() => {const c=document.querySelector('.card')||document.body;"
                        "return {w:c.scrollWidth,h:c.scrollHeight};}"
                    )
                    vw = max(int(dim["w"]) + 40, 800)
                    vh = max(int(dim["h"]) + 40, 200)
                    await page.set_viewport_size({"width": vw, "height": vh})

                    # 截图到内存（避免 Windows 文件锁问题）
                    import io
                    screenshot_bytes = await page.screenshot(
                        full_page=True,
                        type="png",
                    )
                    await page.close()
                    buf = io.BytesIO(screenshot_bytes)
                    png_size = len(screenshot_bytes)
                    logger.debug(f"[HTML渲染] 截图成功, 大小={png_size // 1024}KB")

                    if png_size <= 1024:
                        return None

                    # 压缩过大的图片（超过 20MB 时转 JPEG 并缩小）
                    MAX_SIZE = 20 * 1024 * 1024  # 20MB
                    if png_size > MAX_SIZE:
                        try:
                            from PIL import Image as PILImage
                            img = PILImage.open(buf)
                            # 如果尺寸过大，等比缩小（保留更多细节）
                            max_dim = 6400
                            if img.width > max_dim or img.height > max_dim:
                                ratio = min(max_dim / img.width, max_dim / img.height)
                                new_size = (int(img.width * ratio), int(img.height * ratio))
                                img = img.resize(new_size, PILImage.Resampling.LANCZOS)
                            # 转为 JPEG 压缩（高质量）
                            if img.mode == "RGBA":
                                bg = PILImage.new("RGB", img.size, (255, 255, 255))
                                bg.paste(img, mask=img.split()[3])
                                img = bg
                            elif img.mode != "RGB":
                                img = img.convert("RGB")
                            compressed = tempfile.NamedTemporaryFile(
                                suffix=".jpg", delete=False
                            )
                            img.save(compressed.name, "JPEG", quality=92, optimize=True)
                            compressed.close()
                            logger.debug(f"[HTML渲染] 图片压缩: {png_size // 1024}KB → {os.path.getsize(compressed.name) // 1024}KB")
                            return compressed.name
                        except Exception as e:
                            logger.warning(f"[HTML渲染] 图片压缩失败: {e}")

                    # 未压缩：写入临时 PNG 文件
                    temp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
                    temp.write(buf.getvalue())
                    temp.close()
                    return temp.name

                except Exception:
                    await page.close()
                    raise

            except Exception as e:
                logger.warning(
                    f"[HTML渲染] 第 {attempt + 1} 次尝试失败: {e}"
                )
                if attempt < MAX_ATTEMPTS - 1:
                    await asyncio.sleep(RETRY_DELAY)

        logger.error("[HTML渲染] 所有尝试均失败")
        return None

    async def render_push_card(
        self, payload: RenderPayload
    ) -> Optional[str]:
        return await self.render_card(payload, style="universal_push")

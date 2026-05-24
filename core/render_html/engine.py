"""
HTML 渲染引擎 - 封装 star.html_render 提供三阶回退渲染。
通过配置决定是否使用 HTML 渲染，失败后自动降级。
"""

import asyncio
import os
from pathlib import Path
from typing import Optional

from astrbot.api import logger

from .models import RenderPayload
from .constants import (
    CARD_TEMPLATES,
    DEFAULT_TEMPLATE,
    MAX_ATTEMPTS,
    RETRY_DELAY,
)


class HtmlCardRenderer:
    """
    HTML 卡片渲染器。

    使用 AstrBot 的 star.html_render() 方法将 RenderPayload
    渲染为图片。支持自定义模板、宽度、重试。
    """

    def __init__(self, star, config=None):
        self.star = star
        self.cfg = config
        self._templates: dict[str, str] = {}
        # 模板改为按需惰性加载，不在初始化时加载全部模板

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
                        logger.debug(f"[HTML渲染] 惰性加载模板: {f.name}")
                    except Exception as e:
                        logger.error(f"[HTML渲染] 加载模板失败 {f.name}: {e}")
                    break
        return self._templates.get(style, "")

    def get_template(self, style: str) -> str:
        """获取指定风格的模板 HTML，惰性加载"""
        tmpl = self._load_template(style)
        if tmpl:
            return tmpl
        # 回退到默认模板
        from .constants import DEFAULT_TEMPLATE
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
        渲染卡片为图片，返回图片路径或 None。

        重试策略：
        - 最多 MAX_ATTEMPTS 次
        - 每次失败后等待 RETRY_DELAY 秒
        - 检查输出文件大小 > 4096 字节
        """
        if not self.star:
            logger.warning("[HTML渲染] star 实例不可用")
            return None

        try:
            tmpl = self.get_template(style)
        except RuntimeError as e:
            logger.error(f"[HTML渲染] {e}")
            return None

        context = payload.to_template_context()
        options = {
            "full_page": True,
            "type": "jpeg",
            "quality": 95,
            "scale": "device",
            "device_scale_factor_level": "ultra",
        }

        for attempt in range(MAX_ATTEMPTS):
            try:
                output = await self.star.html_render(
                    tmpl=tmpl,
                    data=context,
                    return_url=False,
                    options=options,
                )
                if output and os.path.getsize(output) > 4096:
                    return output

                logger.warning(
                    f"[HTML渲染] 第 {attempt + 1} 次尝试输出无效"
                    f" ({output})"
                )
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
        """渲染推送窄卡"""
        return await self.render_card(payload, style="universal_push")

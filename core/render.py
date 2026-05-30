import asyncio
import math
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import lru_cache, wraps
from io import BytesIO
from pathlib import Path
from typing import ClassVar, ParamSpec, TypeVar

import aiofiles
from apilmoji import Apilmoji, EmojiCDNSource
from apilmoji.core import get_font_height
from PIL import Image, ImageDraw, ImageFont, ImageFilter

from astrbot.api import logger

from .config import PluginConfig
from .data import GraphicsContent, ImageContent, ParseResult
from .render_html.engine import HtmlCardRenderer
from .render_html.bridge import parse_result_to_render_payload
from .render_html.constants import get_platform_color

# 定义类型变量
P = ParamSpec("P")
T = TypeVar("T")

Color = tuple[int, int, int]
PILImage = Image.Image


def suppress_exception(
    func: Callable[P, T],
) -> Callable[P, T | None]:
    """装饰器：捕获所有异常并返回 None"""

    @wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> T | None:
        try:
            return func(*args, **kwargs)
        except Exception as e:
            logger.debug(f"函数 {func.__name__} 执行失败: {e}")
            return None

    return wrapper


def suppress_exception_async(
    func: Callable[P, Awaitable[T]],
) -> Callable[P, Awaitable[T | None]]:
    """装饰器：捕获所有异常并返回 None"""

    @wraps(func)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T | None:
        try:
            return await func(*args, **kwargs)
        except Exception as e:
            logger.debug(f"函数 {func.__name__} 执行失败: {e}")
            return None

    return wrapper


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    """将十六进制颜色转换为 RGB 元组"""
    h = hex_color.lstrip("#")
    return tuple(int(h[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore


def _lerp_color(
    c1: tuple[int, int, int], c2: tuple[int, int, int], t: float
) -> tuple[int, int, int]:
    """线性插值两个颜色"""
    return (
        int(c1[0] + (c2[0] - c1[0]) * t),
        int(c1[1] + (c2[1] - c1[1]) * t),
        int(c1[2] + (c2[2] - c1[2]) * t),
    )


def _create_gradient_bg(
    w: int, h: int, c1: str = "#ffdee9", c2: str = "#c8e3ff"
) -> Image.Image:
    """创建渐变背景（对角线方向，使用 numpy 加速）"""
    rgb1, rgb2 = _hex_to_rgb(c1), _hex_to_rgb(c2)
    # 使用小图缩放代替逐像素计算（~100x 加速）
    small_w, small_h = max(1, w // 4), max(1, h // 4)
    img = Image.new("RGB", (small_w, small_h))
    pixels = img.load()
    for y in range(small_h):
        for x in range(small_w):
            t = (x / max(small_w - 1, 1) + y / max(small_h - 1, 1)) / 2
            pixels[x, y] = _lerp_color(rgb1, rgb2, t)
    return img.resize((w, h), Image.Resampling.BILINEAR)


def _create_shadow(
    card_w: int,
    card_h: int,
    radius: int,
    offset_x: int = 6,
    offset_y: int = 8,
    blur: int = 18,
    opacity: int = 50,
) -> Image.Image:
    """为圆角矩形卡片生成柔和阴影"""
    sw = card_w + offset_x + blur * 2
    sh = card_h + offset_y + blur * 2
    shadow = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    bx, by = blur, blur
    sd.rounded_rectangle(
        (bx, by, bx + card_w, by + card_h),
        radius=radius,
        fill=(0, 0, 0, opacity),
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(blur))
    return shadow


def _hex_to_rgba(hex_color: str, alpha: int = 255) -> tuple[int, int, int, int]:
    """将十六进制颜色转换为 RGBA 元组"""
    rgb = _hex_to_rgb(hex_color)
    return (*rgb, alpha)


def _draw_gradient_rect(
    img: Image.Image,
    bbox: tuple[int, int, int, int],
    c1_hex: str,
    c2_hex: str,
    direction: str = "horizontal",
):
    """在 RGBA 图像上绘制渐变矩形"""
    x1, y1, x2, y2 = bbox
    w, h = x2 - x1, y2 - y1
    if w <= 0 or h <= 0:
        return
    rgb1, rgb2 = _hex_to_rgb(c1_hex), _hex_to_rgb(c2_hex)
    draw = ImageDraw.Draw(img)
    if direction == "horizontal":
        for x in range(w):
            t = x / max(w - 1, 1)
            color = (*_lerp_color(rgb1, rgb2, t), 255)
            draw.line([(x1 + x, y1), (x1 + x, y2)], fill=color)
    else:
        for y in range(h):
            t = y / max(h - 1, 1)
            color = (*_lerp_color(rgb1, rgb2, t), 255)
            draw.line([(x1, y1 + y), (x2, y1 + y)], fill=color)


@dataclass(eq=False, frozen=True, slots=True)
class FontInfo:
    """字体信息数据类"""

    font: ImageFont.FreeTypeFont
    line_height: int
    cjk_width: int

    def __hash__(self) -> int:
        """实现哈希方法以支持 @lru_cache"""
        return hash((id(self.font), self.line_height, self.cjk_width))

    @lru_cache(maxsize=400)
    def get_char_width(self, char: str) -> int:
        """获取字符宽度，使用缓存优化"""
        # bbox = self.font.getbbox(char)
        # width = int(bbox[2] - bbox[0])
        # return width
        return int(self.font.getlength(char))

    def get_char_width_fast(self, char: str) -> int:
        """快速获取单个字符宽度"""
        if "\u4e00" <= char <= "\u9fff":
            return self.cjk_width
        else:
            return self.get_char_width(char)

    def get_text_width(self, text: str) -> int:
        """计算文本宽度，使用预计算的字符宽度优化性能

        Args:
            text: 要计算宽度的文本

        Returns:
            文本宽度（像素）
        """
        if not text:
            return 0

        total_width = 0
        for char in text:
            total_width += self.get_char_width_fast(char)
        return total_width


@dataclass(eq=False, frozen=True, slots=True)
class FontSet:
    """字体集数据类"""

    _FONT_SIZES = (
        ("name", 36),
        ("title", 40),
        ("text", 32),
        ("extra", 28),
        ("stats", 26),
        ("indicator", 72),
    )
    """基础字体大小（基于 1200px 卡片宽度）"""

    name_font: FontInfo
    title_font: FontInfo
    text_font: FontInfo
    extra_font: FontInfo
    stats_font: FontInfo
    indicator_font: FontInfo

    @classmethod
    def new(cls, font_path: Path, scale: float = 1.0):
        font_infos: dict[str, FontInfo] = {}
        for name, size in cls._FONT_SIZES:
            scaled_size = max(12, int(size * scale))
            font = ImageFont.truetype(font_path, scaled_size)
            font_infos[f"{name}_font"] = FontInfo(
                font=font,
                line_height=get_font_height(font),
                cjk_width=scaled_size,
            )
        return FontSet(**font_infos)


@dataclass(eq=False, frozen=True, slots=True)
class SectionData:
    """基础部分数据类"""

    height: int


@dataclass(eq=False, frozen=True, slots=True)
class HeaderSectionData(SectionData):
    """Header 部分数据"""

    avatar: PILImage | None
    name_lines: list[str]
    desc_lines: list[str]
    time_lines: list[str]
    text_height: int


@dataclass(eq=False, frozen=True, slots=True)
class TitleSectionData(SectionData):
    """标题部分数据"""

    lines: list[str]


@dataclass(eq=False, frozen=True, slots=True)
class CoverSectionData(SectionData):
    """封面部分数据"""

    cover_img: PILImage


@dataclass(eq=False, frozen=True, slots=True)
class TextSectionData(SectionData):
    """文本部分数据"""

    lines: list[str]


@dataclass(eq=False, frozen=True, slots=True)
class ExtraSectionData(SectionData):
    """额外信息部分数据"""

    lines: list[str]


@dataclass(eq=False, frozen=True, slots=True)
class StatsSectionData(SectionData):
    """统计数据部分数据"""

    items: list[tuple[str, str]]


@dataclass(eq=False, frozen=True, slots=True)
class FooterSectionData(SectionData):
    """页脚部分数据"""

    badge_text: str


@dataclass(eq=False, frozen=True, slots=True)
class RepostSectionData(SectionData):
    """转发部分数据"""

    scaled_image: PILImage


@dataclass(eq=False, frozen=True, slots=True)
class ImageGridSectionData(SectionData):
    """图片网格部分数据"""

    images: list[PILImage]
    cols: int
    rows: int
    has_more: bool
    remaining_count: int


@dataclass(eq=False, frozen=True, slots=True)
class GraphicsSectionData(SectionData):
    """图文内容部分数据"""

    text_lines: list[str]
    image: PILImage
    alt_text: str | None = None


@dataclass
class RenderContext:
    """渲染上下文，存储渲染过程中的状态信息"""

    result: ParseResult
    """解析结果"""
    card_width: int
    """卡片宽度"""
    content_width: int
    """内容宽度"""
    image: PILImage
    """当前图像"""
    draw: ImageDraw.ImageDraw
    """绘图对象"""
    not_repost: bool = True
    """是否为非转发内容"""
    y_pos: int = 0
    """当前绘制位置（绘制阶段使用）"""


class Renderer:
    """统一的渲染器，将解析结果转换为消息"""

    # 卡片配置常量
    PADDING = 25
    """内边距"""
    AVATAR_SIZE = 80
    """头像大小"""
    AVATAR_TEXT_GAP = 15
    """头像和文字之间的间距"""
    MAX_COVER_WIDTH = 1600
    """封面最大宽度"""
    MAX_COVER_HEIGHT = 1600
    """封面最大高度"""
    DEFAULT_CARD_WIDTH = 1200
    """默认卡片宽度"""
    MIN_CARD_WIDTH = 400
    """最小卡片宽度"""
    SECTION_SPACING = 15
    """部分间距"""
    NAME_TIME_GAP = 5
    """名称和时间之间的间距"""
    AVATAR_UPSCALE_FACTOR = 2
    """头像圆形框超采样倍数"""

    # 图片处理配置
    MIN_COVER_WIDTH = 300
    """最小封面宽度"""
    MIN_COVER_HEIGHT = 200
    """最小封面高度"""
    MAX_IMAGE_HEIGHT = 1600
    """图片最大高度限制"""
    IMAGE_3_GRID_SIZE = 600
    """图片3列网格最大尺寸"""
    IMAGE_2_GRID_SIZE = 800
    """图片2列网格最大尺寸"""
    IMAGE_GRID_SPACING = 4
    """图片网格间距"""
    MAX_IMAGES_DISPLAY = 9
    """最大显示图片数量"""
    IMAGE_GRID_COLS = 3
    """图片网格列数"""

    # 转发内容配置
    REPOST_PADDING = 12
    """转发内容内边距"""
    REPOST_SCALE = 0.88
    """转发缩放比例"""

    # 颜色配置
    BG_COLOR: ClassVar[Color] = (255, 255, 255)
    """背景色"""
    TEXT_COLOR: ClassVar[Color] = (24, 25, 28)
    """文本色（与HTML一致）"""
    HEADER_COLOR: ClassVar[Color] = (0, 122, 255)
    """标题色（默认，会被平台色覆盖）"""
    EXTRA_COLOR: ClassVar[Color] = (148, 153, 160)
    """额外信息色（与HTML一致）"""
    STATS_COLOR: ClassVar[Color] = (148, 153, 160)
    """统计数据标签色"""
    STATS_VALUE_COLOR: ClassVar[Color] = (24, 25, 28)
    """统计数据数值色（加粗色）"""
    REPOST_BG_COLOR: ClassVar[Color] = (247, 248, 250)
    """转发背景色（与HTML一致）"""
    REPOST_BORDER_COLOR: ClassVar[Color] = (224, 224, 224)
    """转发边框色"""

    # 渐变/阴影/圆角配置
    CARD_RADIUS = 24
    """卡片圆角半径"""
    SHADOW_OFFSET = (6, 8)
    """阴影偏移"""
    SHADOW_BLUR = 18
    """阴影模糊半径"""
    SHADOW_OPACITY = 45
    """阴影透明度"""
    BODY_GRADIENT = ("#ffdee9", "#c8e3ff")
    """页面渐变色（粉→蓝，与HTML一致）"""
    TOP_BAR_HEIGHT = 5
    """顶部装饰条高度"""
    HEADER_GRADIENT = ("#feedf6", "#ffdce8")
    """Header 渐变色（浅粉）"""
    TITLE_BORDER_COLOR = "#fb7299"
    """标题左侧边框色（默认B站粉）"""
    TITLE_BG_COLOR = "#fff0f5"
    """标题背景色"""
    FOOTER_BADGE = "AstrBot · Parser"
    """页脚徽章文字"""

    # 资源名称
    _EMOJIS = "emojis"
    _RESOURCES = "resources"
    _LOGOS = "logos"
    _BUTTON_FILENAME = "media_button.png"
    _FONT_FILENAME = "MisansTC-Regular.ttf"

    # 路径配置
    RESOURCES_DIR: ClassVar[Path] = Path(__file__).parent / _RESOURCES
    """资源目录"""
    LOGOS_DIR: ClassVar[Path] = RESOURCES_DIR / _LOGOS
    """各平台LOGO目录"""
    DEFAULT_FONT_PATH: ClassVar[Path] = RESOURCES_DIR / _FONT_FILENAME
    """默认字体路径"""
    DEFAULT_VIDEO_BUTTON_PATH: ClassVar[Path] = RESOURCES_DIR / _BUTTON_FILENAME
    """默认视频按钮路径"""

    # 动态缩放配置
    SCALE_BASE_WIDTH = 1200
    """缩放基准宽度（字体以此为 1.0x）"""
    MAX_SCALE = 4.0
    """最大缩放倍数"""
    MAX_DYNAMIC_CARD_WIDTH = 99999
    """动态卡片最大宽度（不设限，由图片原始分辨率决定）"""

    def __init__(self, config: PluginConfig, star=None):
        self.cfg = config
        self.star = star
        self.html_renderer = HtmlCardRenderer(star, config) if star else None
        # 默认启用 HTML 渲染，只要 star 可用就尝试
        self.use_html = getattr(config, "use_html_render", True)
        if self.use_html is None:
            self.use_html = bool(self.star)
        # PIL 渲染宽度使用配置值（默认 800）
        cfg_width = getattr(config, "card_width", 800)
        self.DEFAULT_CARD_WIDTH = int(cfg_width) if cfg_width else 800
        self.EMOJI_SOURCE = EmojiCDNSource(
            base_url=self.cfg.emoji_cdn,
            style=self.cfg.emoji_style,
            cache_dir=self.cfg.cache_dir / self._EMOJIS,
        )
        """Emoji Source"""
        # 当前渲染使用的字体集（可被动态缩放覆盖）
        self._active_fontset: FontSet | None = None
        # 平台颜色（每次渲染时根据 result.platform 更新）
        self._platform_color_hex: str = "#fb7299"
        self._platform_color_rgb: tuple[int, int, int] = _hex_to_rgb("#fb7299")
        # Bot名称（用于页脚显示）
        _bot_name = getattr(config, "bot_name", None) or (star.name if star and hasattr(star, 'name') else None) or "AstrBot"
        self.FOOTER_BADGE = f"{_bot_name} · Parser"

    @property
    def fontset(self) -> FontSet:
        """获取当前活跃的字体集（支持动态缩放）"""
        return self._active_fontset or Renderer.fontset

    def _calc_content_scale(self, result: ParseResult) -> tuple[int, float]:
        """根据内容图片实际分辨率计算动态缩放因子

        Returns:
            (card_width, scale_factor)
        """
        max_img_width = 0

        # 尝试从已下载的图片路径读取实际分辨率
        for cont in result.contents:
            if isinstance(cont, (ImageContent, GraphicsContent)):
                if isinstance(cont.path_task, Path) and cont.path_task.exists():
                    try:
                        with Image.open(cont.path_task) as img:
                            max_img_width = max(max_img_width, img.width)
                    except Exception:
                        pass
            elif isinstance(cont, VideoContent):
                if cont.cover and isinstance(cont.cover, Path) and cont.cover.exists():
                    try:
                        with Image.open(cont.cover) as img:
                            max_img_width = max(max_img_width, img.width)
                    except Exception:
                        pass

        # 如果无法读取实际分辨率，回退到基于数量的估算
        if max_img_width <= 0:
            img_count = len(result.img_contents) + len(result.graphics_contents)
            if img_count >= 3:
                max_img_width = self.SCALE_BASE_WIDTH * 1.5
            elif img_count >= 1:
                max_img_width = self.SCALE_BASE_WIDTH * 1.2

        if max_img_width <= 0:
            return self.DEFAULT_CARD_WIDTH, 1.0

        card_width = min(int(max_img_width), self.MAX_DYNAMIC_CARD_WIDTH)
        scale = min(card_width / self.SCALE_BASE_WIDTH, self.MAX_SCALE)
        return card_width, scale

    @classmethod
    def load_resources(cls):
        """加载资源"""
        cls._load_fonts()
        cls._load_video_button()
        cls._load_platform_logos()

    @classmethod
    def _load_fonts(cls):
        """预加载自定义字体"""

        font_path = cls.DEFAULT_FONT_PATH
        if not font_path.exists():
            logger.error(f"字体文件不存在: {font_path}")
            return
        try:
            cls.fontset = FontSet.new(font_path)
            logger.debug(f"加载字体「{font_path.name}」成功")
        except Exception as e:
            logger.error(f"字体加载失败 {font_path.name}: {e}")

    @classmethod
    def _load_video_button(cls):
        """预加载视频按钮"""
        with Image.open(cls.DEFAULT_VIDEO_BUTTON_PATH) as img:
            cls.video_button_image: PILImage = img.convert("RGBA")

        # 设置透明度为 30%
        alpha = cls.video_button_image.split()[-1]  # 获取 alpha 通道
        alpha = alpha.point(lambda x: int(x * 0.3))  # 将透明度设置为 30%
        cls.video_button_image.putalpha(alpha)

    @classmethod
    def _load_platform_logos(cls) -> None:
        cls.platform_logos = {}
        for p in cls.LOGOS_DIR.rglob("*.png"):
            try:
                with Image.open(p) as img:
                    cls.platform_logos[p.stem] = img.convert("RGBA")
            except Exception:
                continue

    async def text(
        self,
        ctx: RenderContext,
        xy: tuple[int, int],
        lines: list[str],
        font: FontInfo,
        fill: Color,
    ) -> int:
        """绘制文本"""
        await Apilmoji.text(
            ctx.image,
            xy,
            lines,
            font.font,
            fill=fill,
            line_height=font.line_height,
            source=self.EMOJI_SOURCE,
        )
        return font.line_height * len(lines)

    async def _create_card_image(
        self,
        result: ParseResult,
        not_repost: bool = True,
    ) -> PILImage:
        """创建卡片图片（用于递归调用）

        Args:
            result: 解析结果
            not_repost: 是否为非转发内容，转发内容为 False

        Returns:
            PIL Image 对象
        """
        # 更新平台颜色
        if not_repost:
            self._platform_color_hex = get_platform_color(result.platform.name)
            self._platform_color_rgb = _hex_to_rgb(self._platform_color_hex)

        # 动态缩放：根据内容图片分辨率调整卡片宽度和字体大小
        old_fontset = self._active_fontset
        old_consts = {}  # 保存被覆盖的类常量
        if not_repost:
            card_width, scale = self._calc_content_scale(result)
            if scale > 1.05:
                self._active_fontset = FontSet.new(
                    Renderer.DEFAULT_FONT_PATH, scale=scale
                )
                # 临时覆盖实例属性（实例属性优先于类属性）
                for const_name in ("PADDING", "AVATAR_SIZE", "AVATAR_TEXT_GAP",
                                   "SECTION_SPACING", "NAME_TIME_GAP",
                                   "MAX_COVER_HEIGHT", "MAX_IMAGE_HEIGHT",
                                   "IMAGE_3_GRID_SIZE", "IMAGE_2_GRID_SIZE",
                                   "IMAGE_GRID_SPACING"):
                    old_val = getattr(Renderer, const_name)
                    old_consts[const_name] = old_val
                    setattr(self, const_name, int(old_val * scale))
            else:
                self._active_fontset = None
        else:
            card_width = self.DEFAULT_CARD_WIDTH

        content_width = card_width - 2 * self.PADDING

        # 计算各部分内容的高度（+footer）
        sections = await self._calculate_sections(result, content_width)
        # 添加页脚
        sections.append(FooterSectionData(
            height=self.fontset.extra_font.line_height + 10,
            badge_text=self.FOOTER_BADGE,
        ))

        # 计算总高度
        card_height = sum(section.height for section in sections)
        card_height += self.PADDING * 2 + self.SECTION_SPACING * (len(sections) - 1)
        # 顶部装饰条额外高度
        if not_repost:
            card_height += self.TOP_BAR_HEIGHT

        # --- 创建卡片内容（RGBA，用于圆角裁剪）---
        card = Image.new("RGBA", (card_width, card_height), (255, 255, 255, 255))

        # 绘制顶部装饰条（平台色渐变）
        if not_repost:
            _draw_gradient_rect(
                card,
                (0, 0, card_width, self.TOP_BAR_HEIGHT),
                self._platform_color_hex,
                "#ffb1cc",
            )

        # 创建渲染上下文（y_pos 起始位置考虑装饰条）
        start_y = (self.TOP_BAR_HEIGHT + self.PADDING) if not_repost else self.PADDING
        ctx = RenderContext(
            result=result,
            card_width=card_width,
            content_width=content_width,
            image=card,
            draw=ImageDraw.Draw(card),
            not_repost=not_repost,
            y_pos=start_y,
        )
        # 绘制各部分内容
        await self._draw_sections(ctx, sections)

        # --- 应用圆角遮罩 ---
        radius = self.CARD_RADIUS if not_repost else 12
        mask = Image.new("L", (card_width, card_height), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            (0, 0, card_width - 1, card_height - 1),
            radius=radius,
            fill=255,
        )
        card.putalpha(mask)

        # --- 合成到渐变背景 + 阴影 ---
        if not_repost:
            bg = _create_gradient_bg(
                card_width + 60, card_height + 60, *self.BODY_GRADIENT
            )
            shadow = _create_shadow(
                card_width,
                card_height,
                radius,
                *self.SHADOW_OFFSET,
                blur=self.SHADOW_BLUR,
                opacity=self.SHADOW_OPACITY,
            )
            ox, oy = self.SHADOW_OFFSET
            sx = self.SHADOW_BLUR - ox
            sy = self.SHADOW_BLUR - oy
            bg.paste(shadow, (0, 0), shadow)
            bg.paste(card, (sx, sy), card)
            image = bg
        else:
            image = Image.new("RGB", (card_width, card_height), self.REPOST_BG_COLOR)
            image.paste(card, (0, 0), card)

        # 如果卡片过大，等比缩放整张图片
        MAX_TOTAL_PIXELS = 64000000  # 64MP
        w, h = image.size
        if w * h > MAX_TOTAL_PIXELS:
            s = math.sqrt(MAX_TOTAL_PIXELS / (w * h))
            new_w = max(int(w * s), 800)
            new_h = max(int(h * s), 200)
            image = image.resize((new_w, new_h), Image.Resampling.LANCZOS)
            logger.debug(f"[渲染] 卡片等比缩放: ({w}x{h}) -> ({new_w}x{new_h}), scale={s:.2f}")

        # 恢复原始字体集和常量
        self._active_fontset = old_fontset
        for const_name, old_val in old_consts.items():
            setattr(self, const_name, old_val)

        # 确保返回 RGB
        if image.mode != "RGB":
            image = image.convert("RGB")

        return image

    async def render_card(
        self, result: ParseResult, style: str = "universal_card"
    ) -> Path | None:
        """渲染卡片 - 三阶回退: HTML → PIL → 纯文本+图片"""
        # 第一阶: HTML 渲染 (remote T2I service)
        if self.use_html and self.html_renderer:
            try:
                from .render_html.bridge import download_images_to_data_uri
                payload = parse_result_to_render_payload(
                    result,
                    card_width=getattr(self.cfg, "card_width", "1440px"),
                    bot_name=getattr(self.cfg, "bot_name", None) or (self.star.name if self.star and hasattr(self.star, 'name') else None) or "AstrBot",
                )
                logger.debug(f"[渲染] HTML payload: name={payload.name!r}, title={payload.title!r}, signature={payload.signature!r}, timestamp={payload.timestamp!r}, stats={payload.stats}, pinned_comment={payload.pinned_comment is not None}, images={len(payload.image_urls)}")
                # 下载远程图片为 data URI，绕过防盗链
                await download_images_to_data_uri(
                    payload,
                    headers={"Referer": "https://www.bilibili.com/"},
                    proxy=getattr(self.cfg, "proxy", None),
                )
                logger.debug(f"[渲染] 下载后 images={len(payload.image_urls)}, avatar_is_data={payload.avatar.startswith('data:') if payload.avatar else False}")
                img_path = await self.html_renderer.render_card(payload, style=style)
                if img_path:
                    return Path(img_path)
                logger.warning("[渲染] HTML渲染失败，降级到PIL")
            except Exception as e:
                logger.warning(f"[渲染] HTML渲染异常: {e}，降级到PIL")

        # 第二阶: PIL 渲染（本地，不依赖外部服务）
        cache = self.cfg.cache_dir / f"card_{uuid.uuid4().hex}.png"
        try:
            img = await self._create_card_image(result)
            buf = BytesIO()
            await asyncio.to_thread(img.save, buf, format="PNG")
            async with aiofiles.open(cache, "wb") as fp:
                await fp.write(buf.getvalue())
            return cache
        except Exception as e:
            logger.warning(f"[渲染] PIL渲染失败: {e}，降级到纯文本")
            return None

    @suppress_exception
    def _load_and_resize_cover(
        self,
        cover_path: Path | None,
        content_width: int,
    ) -> PILImage | None:
        """加载并调整封面尺寸

        Args:
            cover_path: 封面路径
            content_width: 内容区域宽度, 封面会缩放到此宽度以确保左右padding一致
        """
        if not cover_path or not cover_path.exists():
            return None

        with Image.open(cover_path) as original_img:
            # 转换为 RGB 模式以确保兼容性
            if original_img.mode not in ("RGB", "RGBA"):
                cover_img = original_img.convert("RGB")
            else:
                cover_img = original_img

            # 封面宽度应该等于内容区域宽度，以确保左右padding一致
            target_width = content_width

            # 计算缩放比例（保持宽高比，不裁剪高度）
            if cover_img.width != target_width:
                scale_ratio = target_width / cover_img.width
                new_width = target_width
                new_height = int(cover_img.height * scale_ratio)

                cover_img = cover_img.resize(
                    (new_width, new_height),
                    Image.Resampling.LANCZOS,
                )
            elif cover_img is original_img:
                # 如果没有做任何转换，需要 copy 一份，因为原图会在 with 结束时关闭
                cover_img = cover_img.copy()

            return cover_img

    @suppress_exception
    def _load_and_process_avatar(self, avatar: Path | None) -> PILImage | None:
        """加载并处理头像（圆形裁剪，带抗锯齿）"""
        if not avatar or not avatar.exists():
            return None

        with Image.open(avatar) as original_img:
            # 转换为 RGBA 模式（用于更好的抗锯齿效果）
            if original_img.mode != "RGBA":
                avatar_img = original_img.convert("RGBA")
            else:
                avatar_img = original_img

            # 使用超采样技术提高质量：先放大到指定倍数
            scale = self.AVATAR_UPSCALE_FACTOR
            temp_size = self.AVATAR_SIZE * scale
            avatar_img = avatar_img.resize(
                (temp_size, temp_size),
                Image.Resampling.LANCZOS,
            )

            # 创建高分辨率圆形遮罩（带抗锯齿）
            mask = Image.new("L", (temp_size, temp_size), 0)
            mask_draw = ImageDraw.Draw(mask)
            mask_draw.ellipse((0, 0, temp_size - 1, temp_size - 1), fill=255)

            # 应用遮罩
            output_avatar = Image.new(
                "RGBA",
                (temp_size, temp_size),
                (0, 0, 0, 0),
            )
            output_avatar.paste(avatar_img, (0, 0))
            output_avatar.putalpha(mask)

            # 缩小到目标尺寸（抗锯齿缩放）
            output_avatar = output_avatar.resize(
                (self.AVATAR_SIZE, self.AVATAR_SIZE),
                Image.Resampling.LANCZOS,
            )

            return output_avatar

    async def _calculate_sections(
        self, result: ParseResult, content_width: int
    ) -> list[SectionData]:
        """计算各部分内容的高度和数据"""
        sections: list[SectionData] = []

        # 1. Header 部分
        header_section = await self._calculate_header_section(result, content_width)
        if header_section is not None:
            sections.append(header_section)

        # 2. 标题部分
        if result.title:
            title_lines = self._wrap_text(
                result.title,
                content_width,
                self.fontset.title_font,
            )
            title_height = len(title_lines) * self.fontset.title_font.line_height
            sections.append(TitleSectionData(height=title_height, lines=title_lines))

        # 3. 封面，图集，图文内容
        if cover_img := self._load_and_resize_cover(
            await result.cover_path,
            content_width=content_width,
        ):
            sections.append(
                CoverSectionData(height=cover_img.height, cover_img=cover_img)
            )
        elif result.img_contents:
            # 如果没有封面但有图片，处理图片列表
            img_grid_section = await self._calculate_image_grid_section(
                result,
                content_width,
            )
            if img_grid_section:
                sections.append(img_grid_section)
        elif result.graphics_contents:
            for graphics_content in result.graphics_contents:
                graphics_section = await self._calculate_graphics_section(
                    graphics_content,
                    content_width,
                )
                if graphics_section:
                    sections.append(graphics_section)

        # 5. 文本内容
        if result.text:
            text_lines = self._wrap_text(
                result.text,
                content_width,
                self.fontset.text_font,
            )
            text_height = len(text_lines) * self.fontset.text_font.line_height
            sections.append(TextSectionData(height=text_height, lines=text_lines))

        # 6. 额外信息
        if result.extra_info:
            extra_lines = self._wrap_text(
                result.extra_info,
                content_width,
                self.fontset.extra_font,
            )
            extra_height = len(extra_lines) * self.fontset.extra_font.line_height
            sections.append(ExtraSectionData(height=extra_height, lines=extra_lines))

        # 7. 统计数据
        if result.stats:
            stat_items = []
            STAT_LABELS = {
                "views": "播放", "danmaku": "弹幕", "likes": "赞",
                "favorites": "收藏", "coins": "硬币", "comments": "评论",
                "reposts": "转发", "quotes": "引用", "bookmarks": "书签",
            }
            for key in ("views", "danmaku", "likes", "favorites", "coins", "comments", "reposts", "quotes", "bookmarks"):
                val = result.stats.get(key)
                if val:
                    stat_items.append((str(val), STAT_LABELS.get(key, key)))
            if stat_items:
                stats_height = self.fontset.stats_font.line_height + 10
                sections.append(StatsSectionData(height=stats_height, items=stat_items))

        # 8. 转发内容
        if result.repost:
            repost_section = await self._calculate_repost_section(result.repost)
            sections.append(repost_section)

        return sections

    @suppress_exception_async
    async def _calculate_graphics_section(
        self, graphics_content: GraphicsContent, content_width: int
    ) -> GraphicsSectionData | None:
        """计算图文内容部分的高度和内容"""
        # 加载图片
        img_path = await graphics_content.get_path()
        with Image.open(img_path) as original_img:
            # 调整图片尺寸以适应内容宽度
            if original_img.width > content_width:
                ratio = content_width / original_img.width
                new_height = int(original_img.height * ratio)
                image = original_img.resize(
                    (content_width, new_height),
                    Image.Resampling.LANCZOS,
                )
            else:
                # 如果不需要缩放，copy 一份
                image = original_img.copy()

            # 处理文本内容
            text_lines = []
            if graphics_content.text:
                text_lines = self._wrap_text(
                    graphics_content.text,
                    content_width,
                    self.fontset.text_font,
                )

            # 计算总高度：文本高度 + 图片高度 + alt文本高度 + 间距
            text_height = (
                len(text_lines) * self.fontset.text_font.line_height
                if text_lines
                else 0
            )
            alt_height = (
                self.fontset.extra_font.line_height if graphics_content.alt else 0
            )
            total_height = text_height + image.height + alt_height
            if text_lines:
                total_height += self.SECTION_SPACING  # 文本和图片之间的间距
            if graphics_content.alt:
                total_height += self.SECTION_SPACING  # 图片和alt文本之间的间距

            return GraphicsSectionData(
                height=total_height,
                text_lines=text_lines,
                image=image,
                alt_text=graphics_content.alt,
            )

    async def _calculate_header_section(
        self,
        result: ParseResult,
        content_width: int,
    ) -> HeaderSectionData | None:
        """计算 header 部分的高度和内容"""
        if result.author is None:
            return None

        # 加载头像
        avatar_img = self._load_and_process_avatar(
            await result.author.get_avatar_path()
        )

        # 计算文字区域宽度（始终预留头像空间）
        text_area_width = content_width - (self.AVATAR_SIZE + self.AVATAR_TEXT_GAP)

        # 发布者名称
        name_lines = self._wrap_text(
            result.author.name,
            text_area_width,
            self.fontset.name_font,
        )

        # 签名/描述
        desc_lines: list[str] = []
        if result.author.description:
            desc_lines = self._wrap_text(
                result.author.description,
                text_area_width,
                self.fontset.extra_font,
            )

        # 时间
        time_text = result.formatted_datetime()
        time_lines = self._wrap_text(
            time_text,
            text_area_width,
            self.fontset.extra_font,
        )

        # 粉丝数
        has_follower = (
            result.author
            and result.author.follower_count
            and (result.author.follower_count > 0 if isinstance(result.author.follower_count, int) else True)
        )
        has_tags = bool(
            result.extra.get("bvid")
            or result.extra.get("handle")
            or (result.author and result.author.uid)
        )

        # 计算 header 高度（取头像和文字中较大者）
        text_height = len(name_lines) * self.fontset.name_font.line_height
        # UID/BVID 标签（在名称下方）
        if has_tags:
            badge_h = self.fontset.extra_font.line_height + 6
            text_height += self.NAME_TIME_GAP + badge_h
        if desc_lines:
            text_height += (
                self.NAME_TIME_GAP
                + len(desc_lines) * self.fontset.extra_font.line_height
            )
        if has_follower:
            text_height += (
                self.NAME_TIME_GAP
                + self.fontset.extra_font.line_height
            )
        if time_lines:
            text_height += (
                self.NAME_TIME_GAP
                + len(time_lines) * self.fontset.extra_font.line_height
            )
        header_height = max(self.AVATAR_SIZE, text_height)

        return HeaderSectionData(
            height=header_height,
            avatar=avatar_img,
            name_lines=name_lines,
            desc_lines=desc_lines,
            time_lines=time_lines,
            text_height=text_height,
        )

    async def _calculate_repost_section(self, repost: ParseResult) -> RepostSectionData:
        """计算转发内容的高度和内容（递归调用绘制方法）"""
        repost_image = await self._create_card_image(repost, False)
        # 缩放图片
        scaled_width = int(repost_image.width * self.REPOST_SCALE)
        scaled_height = int(repost_image.height * self.REPOST_SCALE)
        repost_image_scaled = repost_image.resize(
            (scaled_width, scaled_height),
            Image.Resampling.LANCZOS,
        )

        return RepostSectionData(
            height=scaled_height + self.REPOST_PADDING * 2,  # 加上转发容器的内边距
            scaled_image=repost_image_scaled,
        )

    async def _calculate_image_grid_section(
        self, result: ParseResult, content_width: int
    ) -> ImageGridSectionData | None:
        """计算图片网格部分的高度和内容"""
        if not result.img_contents:
            return None

        # 检查是否有超过最大显示数量的图片
        total_images = len(result.img_contents)
        has_more = total_images > self.MAX_IMAGES_DISPLAY

        # 如果超过最大显示数量，处理前N张，最后一张显示+N效果
        if has_more:
            img_contents = result.img_contents[: self.MAX_IMAGES_DISPLAY]
            remaining_count = total_images - self.MAX_IMAGES_DISPLAY
        else:
            img_contents = result.img_contents[: self.MAX_IMAGES_DISPLAY]
            remaining_count = 0

        processed_images = []
        img_count = len(img_contents)

        for img_content in img_contents:
            img_path = await img_content.get_path()
            # 使用装饰器保护的方法，失败会返回 None
            img = await self._load_and_process_grid_image(
                img_path, content_width, img_count
            )
            if img is not None:
                processed_images.append(img)

        if not processed_images:
            return None

        # 计算网格布局
        image_count = len(processed_images)

        if image_count == 1:
            # 单张图片
            cols, rows = 1, 1
        elif image_count in (2, 4):
            # 2张或4张图片，使用2列布局
            cols, rows = 2, (image_count + 1) // 2
        else:
            # 多张图片，使用3列布局（九宫格）
            cols = self.IMAGE_GRID_COLS
            rows = (image_count + cols - 1) // cols

        # 计算高度
        max_img_height = max(img.height for img in processed_images)
        if len(processed_images) == 1:
            # 单张图片
            grid_height = max_img_height
        else:
            # 多张图片：上间距 + (图片 + 间距) * 行数
            grid_height = self.IMAGE_GRID_SPACING + rows * (
                max_img_height + self.IMAGE_GRID_SPACING
            )

        return ImageGridSectionData(
            height=grid_height,
            images=processed_images,
            cols=cols,
            rows=rows,
            has_more=has_more,
            remaining_count=remaining_count,
        )

    @suppress_exception_async
    async def _load_and_process_grid_image(
        self,
        img_path: Path,
        content_width: int,
        img_count: int,
    ) -> PILImage | None:
        """加载并处理网格图片

        Args:
            img_path: 图片路径
            content_width: 内容宽度
            img_count: 图片总数（用于决定处理方式）

        Returns:
            处理后的图片对象，失败返回 None
        """
        if not img_path.exists():
            return None

        with Image.open(img_path) as original_img:
            img = original_img

            # 根据图片数量决定处理方式
            if img_count >= 2:
                # 2张及以上图片，统一为方形
                img = self._crop_to_square(img)

            # 计算图片尺寸
            if img_count == 1:
                # 单张图片，根据卡片宽度调整，保持原始宽高比
                max_width = content_width
                if img.width > max_width:
                    ratio = max_width / img.width
                    new_size = (int(img.width * ratio), int(img.height * ratio))
                    img = img.resize(new_size, Image.Resampling.LANCZOS)
                elif img is original_img:
                    # 如果没有做任何转换，需要 copy 一份
                    img = img.copy()
            else:
                # 多张图片，计算最大尺寸
                if img_count in (2, 4):
                    # 2张或4张图片，使用2列布局
                    num_gaps = 3  # 2列有3个间距
                    max_size = (content_width - self.IMAGE_GRID_SPACING * num_gaps) // 2
                else:
                    # 多张图片，使用3列布局
                    num_gaps = self.IMAGE_GRID_COLS + 1
                    max_size = (
                        content_width - self.IMAGE_GRID_SPACING * num_gaps
                    ) // self.IMAGE_GRID_COLS

                # 调整多张图片的尺寸
                if img.width > max_size or img.height > max_size:
                    ratio = min(max_size / img.width, max_size / img.height)
                    new_size = (int(img.width * ratio), int(img.height * ratio))
                    img = img.resize(new_size, Image.Resampling.LANCZOS)
                elif img is original_img:
                    # 如果没有做任何转换，需要 copy 一份
                    img = img.copy()

            return img

    def _crop_to_square(self, img: PILImage) -> PILImage:
        """将图片裁剪为方形（上下切割）"""
        width, height = img.size

        if width == height:
            return img

        if width > height:
            # 宽图片，左右切割
            left = (width - height) // 2
            right = left + height
            return img.crop((left, 0, right, height))
        else:
            # 高图片，上下切割
            top = (height - width) // 2
            bottom = top + width
            return img.crop((0, top, width, bottom))

    async def _draw_sections(
        self, ctx: RenderContext, sections: list[SectionData]
    ) -> None:
        """绘制所有内容到画布上"""
        for section in sections:
            match section:
                case HeaderSectionData() as header:
                    await self._draw_header(ctx, header)
                case TitleSectionData() as title:
                    await self._draw_title(ctx, title.lines)
                case CoverSectionData() as cover:
                    self._draw_cover(ctx, cover.cover_img)
                case TextSectionData() as text:
                    await self._draw_text(ctx, text.lines)
                case GraphicsSectionData() as graphics:
                    await self._draw_graphics(ctx, graphics)
                case ExtraSectionData() as extra:
                    await self._draw_extra(ctx, extra.lines)
                case StatsSectionData() as stats:
                    self._draw_stats(ctx, stats)
                case FooterSectionData() as footer:
                    self._draw_footer(ctx, footer)
                case RepostSectionData() as repost:
                    self._draw_repost(ctx, repost)
                case ImageGridSectionData() as image_grid:
                    self._draw_image_grid(ctx, image_grid)

    def _create_avatar_placeholder(self) -> PILImage:
        """创建默认头像占位符"""
        # 头像占位符配置常量
        placeholder_bg_color = (230, 230, 230, 255)
        placeholder_fg_color = (200, 200, 200, 255)
        head_ratio = 0.35  # 头部位置比例
        head_radius_ratio = 1 / 6  # 头部半径比例
        shoulder_y_ratio = 0.55  # 肩部 Y 位置比例
        shoulder_width_ratio = 0.55  # 肩部宽度比例
        shoulder_height_ratio = 0.6  # 肩部高度比例

        placeholder = Image.new(
            "RGBA",
            (self.AVATAR_SIZE, self.AVATAR_SIZE),
            (0, 0, 0, 0),
        )
        draw = ImageDraw.Draw(placeholder)

        # 绘制圆形背景
        draw.ellipse(
            (0, 0, self.AVATAR_SIZE - 1, self.AVATAR_SIZE - 1),
            fill=placeholder_bg_color,
        )

        # 绘制简单的用户图标（圆形头部 + 肩部）
        center_x = self.AVATAR_SIZE // 2

        # 头部圆形
        head_radius = int(self.AVATAR_SIZE * head_radius_ratio)
        head_y = int(self.AVATAR_SIZE * head_ratio)
        draw.ellipse(
            (
                center_x - head_radius,
                head_y - head_radius,
                center_x + head_radius,
                head_y + head_radius,
            ),
            fill=placeholder_fg_color,
        )

        # 肩部
        shoulder_y = int(self.AVATAR_SIZE * shoulder_y_ratio)
        shoulder_width = int(self.AVATAR_SIZE * shoulder_width_ratio)
        shoulder_height = int(self.AVATAR_SIZE * shoulder_height_ratio)
        draw.ellipse(
            (
                center_x - shoulder_width // 2,
                shoulder_y,
                center_x + shoulder_width // 2,
                shoulder_y + shoulder_height,
            ),
            fill=placeholder_fg_color,
        )

        # 创建圆形遮罩确保不超出边界
        mask = Image.new("L", (self.AVATAR_SIZE, self.AVATAR_SIZE), 0)
        mask_draw = ImageDraw.Draw(mask)
        mask_draw.ellipse((0, 0, self.AVATAR_SIZE - 1, self.AVATAR_SIZE - 1), fill=255)

        # 应用遮罩
        placeholder.putalpha(mask)
        return placeholder

    async def _draw_header(
        self, ctx: RenderContext, section: HeaderSectionData
    ) -> None:
        """绘制 header 部分（带渐变背景和平台色）"""
        # 绘制 header 渐变背景（仅非转发）
        if ctx.not_repost:
            _draw_gradient_rect(
                ctx.image,
                (0, ctx.y_pos - self.SECTION_SPACING,
                 ctx.card_width, ctx.y_pos + section.height),
                *self.HEADER_GRADIENT,
                direction="vertical",
            )

        x_pos = self.PADDING

        # 绘制头像或占位符
        avatar = section.avatar if section.avatar else self._create_avatar_placeholder()
        ctx.image.paste(avatar, (x_pos, ctx.y_pos), avatar)

        # 文字始终从头像位置后面开始
        text_x = self.PADDING + self.AVATAR_SIZE + self.AVATAR_TEXT_GAP

        # 计算文字垂直居中位置（对齐头像中轴）
        avatar_center = ctx.y_pos + self.AVATAR_SIZE // 2
        text_start_y = avatar_center - section.text_height // 2
        text_y = text_start_y

        # 发布者名称（平台色）
        text_y += await self.text(
            ctx,
            (text_x, text_y),
            section.name_lines,
            self.fontset.name_font,
            fill=self._platform_color_rgb,
        )

        # UID/BVID/Handle 标签（平台色边框小标签，位于名称下方）
        if ctx.not_repost:
            uid = str(ctx.result.extra.get("uid", "")) or (
                ctx.result.author.uid if ctx.result.author else ""
            )
            bvid = str(ctx.result.extra.get("bvid", ""))
            handle = str(ctx.result.extra.get("handle", ""))
            if uid or bvid or handle:
                text_y += self.NAME_TIME_GAP
                tag_y = text_y
                tag_x = text_x
                if uid:
                    tag_x = self._draw_badge(
                        ctx, tag_x, tag_y, f"UID {uid}",
                        self._platform_color_hex, font_info=self.fontset.extra_font,
                    )
                if handle:
                    tag_x = self._draw_badge(
                        ctx, tag_x + (6 if tag_x != text_x else 0), tag_y, handle,
                        self._platform_color_hex, font_info=self.fontset.extra_font,
                    )
                if bvid:
                    self._draw_badge(
                        ctx, tag_x + 6, tag_y, bvid,
                        "#008ac5", font_info=self.fontset.extra_font,
                    )
                text_y += self.fontset.extra_font.line_height + 6

        # 签名（灰色）
        if section.desc_lines:
            text_y += self.NAME_TIME_GAP
            text_y += await self.text(
                ctx,
                (text_x, text_y),
                section.desc_lines,
                self.fontset.extra_font,
                fill=self.EXTRA_COLOR,
            )

        # 粉丝数（灰色）
        if ctx.not_repost and ctx.result.author and ctx.result.author.follower_count:
            fc = ctx.result.author.follower_count
            if isinstance(fc, int):
                fc_text = f"{fc} 粉丝" if fc < 10000 else f"{fc / 10000:.1f}万 粉丝"
            else:
                fc_text = f"{fc} 粉丝"
            text_y += self.NAME_TIME_GAP
            text_y += await self.text(
                ctx,
                (text_x, text_y),
                [fc_text],
                self.fontset.extra_font,
                fill=self.EXTRA_COLOR,
            )

        # 时间（灰色）
        if section.time_lines:
            text_y += self.NAME_TIME_GAP
            text_y += await self.text(
                ctx,
                (text_x, text_y),
                section.time_lines,
                self.fontset.extra_font,
                fill=self.EXTRA_COLOR,
            )

        # 在右侧绘制平台 logo（仅在非转发内容时绘制）
        if ctx.not_repost:
            platform_name = ctx.result.platform.name
            if platform_name in self.platform_logos:
                logo_img = self.platform_logos[platform_name]
                logo_x = ctx.image.width - self.PADDING - logo_img.width
                logo_y = ctx.y_pos + (self.AVATAR_SIZE - logo_img.height) // 2
                ctx.image.paste(logo_img, (logo_x, logo_y), logo_img)

        ctx.y_pos += section.height + self.SECTION_SPACING

    async def _draw_title(self, ctx: RenderContext, lines: list[str]) -> None:
        """绘制标题（带平台色左边框和浅色背景）"""
        font_info = self.fontset.title_font
        line_h = font_info.line_height
        total_h = line_h * len(lines) + 12  # 上下各 6px padding

        # 浅色背景 + 左边框
        x1, y1 = self.PADDING, ctx.y_pos
        x2, y2 = ctx.card_width - self.PADDING, ctx.y_pos + total_h
        # 背景矩形
        ctx.draw.rounded_rectangle(
            (x1, y1, x2, y2), radius=6, fill=_hex_to_rgba(self.TITLE_BG_COLOR)
        )
        # 左边框（4px 宽，平台色）
        border_color = (*self._platform_color_rgb, 255)
        ctx.draw.rectangle(
            (x1, y1, x1 + 4, y2), fill=border_color
        )

        # 绘制文字（左边框 + 12px 偏移）
        text_x = x1 + 16
        text_y = y1 + 6
        await self.text(ctx, (text_x, text_y), lines, font_info, self.TEXT_COLOR)

        ctx.y_pos += total_h + self.SECTION_SPACING

    @staticmethod
    def _apply_rounded_corners(img: PILImage, radius: int = 12) -> PILImage:
        """给图片应用圆角"""
        if img.mode != "RGBA":
            img = img.convert("RGBA")
        mask = Image.new("L", img.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            (0, 0, img.size[0] - 1, img.size[1] - 1),
            radius=radius,
            fill=255,
        )
        img.putalpha(mask)
        return img

    def _draw_cover(self, ctx: RenderContext, cover_img: PILImage) -> None:
        """绘制封面（带圆角和阴影）"""
        x_pos = self.PADDING
        # 应用圆角
        cover_rgba = self._apply_rounded_corners(cover_img, radius=12)
        ctx.image.paste(cover_rgba, (x_pos, ctx.y_pos), cover_rgba)

        # 添加视频播放按钮（居中）
        button_size = 128  # 固定使用 128x128 尺寸
        button_x = x_pos + (cover_img.width - button_size) // 2
        button_y = ctx.y_pos + (cover_img.height - button_size) // 2
        ctx.image.paste(
            self.video_button_image,
            (button_x, button_y),
            self.video_button_image,
        )

        ctx.y_pos += cover_img.height + self.SECTION_SPACING

    async def _draw_text(self, ctx: RenderContext, lines: list[str]) -> None:
        """绘制文本内容"""
        ctx.y_pos += await self.text(
            ctx,
            (self.PADDING, ctx.y_pos),
            lines,
            self.fontset.text_font,
            fill=self.TEXT_COLOR,
        )
        ctx.y_pos += self.SECTION_SPACING

    async def _draw_graphics(
        self, ctx: RenderContext, section: GraphicsSectionData
    ) -> None:
        """绘制图文内容（带圆角图片）"""
        if section.text_lines:
            ctx.y_pos += await self.text(
                ctx,
                (self.PADDING, ctx.y_pos),
                section.text_lines,
                self.fontset.text_font,
                fill=self.TEXT_COLOR,
            )
            ctx.y_pos += self.SECTION_SPACING

        # 绘制图片（居中，带圆角）
        x_pos = self.PADDING + (ctx.content_width - section.image.width) // 2
        img_rgba = self._apply_rounded_corners(section.image, radius=12)
        ctx.image.paste(img_rgba, (x_pos, ctx.y_pos), img_rgba)
        ctx.y_pos += section.image.height

        # 绘制 alt 文本（如果有，居中显示）
        if section.alt_text:
            ctx.y_pos += self.SECTION_SPACING  # 图片和alt文本之间的间距
            # 计算文本居中位置
            extra_font_info = self.fontset.extra_font
            text_width = extra_font_info.get_text_width(section.alt_text)
            text_x = self.PADDING + (ctx.content_width - text_width) // 2
            ctx.y_pos += await self.text(
                ctx,
                (text_x, ctx.y_pos),
                [section.alt_text],
                self.fontset.extra_font,
                fill=self.EXTRA_COLOR,
            )

        ctx.y_pos += self.SECTION_SPACING

    async def _draw_extra(self, ctx: RenderContext, lines: list[str]) -> None:
        """绘制额外信息"""
        ctx.y_pos += await self.text(
            ctx,
            (self.PADDING, ctx.y_pos),
            lines,
            self.fontset.extra_font,
            fill=self.EXTRA_COLOR,
        )

    def _draw_stats(self, ctx: RenderContext, section: StatsSectionData) -> None:
        """绘制统计数据（带浅灰背景条）"""
        # 背景条
        bar_y1 = ctx.y_pos
        bar_y2 = ctx.y_pos + section.height + 8
        ctx.draw.rectangle(
            (0, bar_y1, ctx.card_width, bar_y2),
            fill=(250, 250, 250, 255),
        )
        # 顶部分隔线
        ctx.draw.line(
            [(self.PADDING, bar_y1), (ctx.card_width - self.PADDING, bar_y1)],
            fill=(240, 240, 240, 255), width=1,
        )

        x = self.PADDING + 4
        y = ctx.y_pos + 4
        font_info = self.fontset.stats_font
        separator = "  |  "
        for i, (value, label) in enumerate(section.items):
            # 数值（加粗色）
            ctx.draw.text((x, y), value, fill=self.STATS_VALUE_COLOR, font=font_info.font)
            x += font_info.get_text_width(value)
            # 标签（灰色）
            ctx.draw.text((x, y), label, fill=self.STATS_COLOR, font=font_info.font)
            x += font_info.get_text_width(label)
            # 分隔符
            if i < len(section.items) - 1:
                ctx.draw.text((x, y), separator, fill=self.STATS_COLOR, font=font_info.font)
                x += font_info.get_text_width(separator)
        ctx.y_pos += section.height + 8 + self.SECTION_SPACING

    def _draw_badge(
        self,
        ctx: RenderContext,
        x: int,
        y: int,
        text: str,
        color_hex: str,
        font_info: FontInfo | None = None,
    ) -> int:
        """绘制圆角小标签（如 UID、BVID），返回标签右端 x 坐标"""
        fi = font_info or self.fontset.extra_font
        pad_x, pad_y = 8, 2
        tw = fi.get_text_width(text)
        th = fi.line_height
        badge_w = tw + pad_x * 2
        badge_h = th + pad_y * 2
        # 边框色（半透明）
        border_rgba = _hex_to_rgba(color_hex, 80)
        fill_rgba = _hex_to_rgba(color_hex, 15)
        text_rgb = _hex_to_rgb(color_hex)

        ctx.draw.rounded_rectangle(
            (x, y, x + badge_w, y + badge_h),
            radius=12,
            fill=fill_rgba,
            outline=border_rgba,
            width=1,
        )
        ctx.draw.text((x + pad_x, y + pad_y), text, fill=text_rgb, font=fi.font)
        return x + badge_w

    def _draw_footer(self, ctx: RenderContext, section: FooterSectionData) -> None:
        """绘制页脚（AstrBot · Parser 徽章）"""
        # 背景渐变
        y1 = ctx.y_pos
        y2 = ctx.y_pos + section.height
        _draw_gradient_rect(
            ctx.image,
            (0, y1, ctx.card_width, y2),
            "#f3f6f8", "#ebedf0",
            direction="vertical",
        )
        # 徽章
        fi = self.fontset.extra_font
        text = section.badge_text
        tw = fi.get_text_width(text)
        badge_w = tw + 24
        badge_h = fi.line_height + 8
        badge_x = ctx.card_width - self.PADDING - badge_w
        badge_y = y1 + (section.height - badge_h) // 2
        # 蓝色圆角徽章
        ctx.draw.rounded_rectangle(
            (badge_x, badge_y, badge_x + badge_w, badge_y + badge_h),
            radius=50,
            fill=(230, 247, 255, 255),
            outline=(0, 138, 197, 40),
            width=1,
        )
        ctx.draw.text(
            (badge_x + 12, badge_y + 4),
            text,
            fill=(0, 138, 197, 255),
            font=fi.font,
        )
        ctx.y_pos += section.height + self.SECTION_SPACING

    def _draw_repost(self, ctx: RenderContext, section: RepostSectionData) -> None:
        """绘制转发内容"""
        # 获取缩放后的转发图片
        repost_image = section.scaled_image

        # 转发框占满整个内容区域，左右和边缘对齐
        repost_x = self.PADDING
        repost_y = ctx.y_pos
        repost_width = ctx.content_width  # 转发框宽度等于内容区域宽度
        repost_height = section.height

        # 绘制转发背景（圆角矩形）
        self._draw_rounded_rectangle(
            ctx.image,
            (repost_x, repost_y, repost_x + repost_width, repost_y + repost_height),
            self.REPOST_BG_COLOR,
            radius=8,
        )

        # 绘制转发边框
        self._draw_rounded_rectangle_border(
            ctx.draw,
            (repost_x, repost_y, repost_x + repost_width, repost_y + repost_height),
            self.REPOST_BORDER_COLOR,
            radius=8,
            width=1,
        )

        # 转发图片在转发容器中居中
        card_x = repost_x + (repost_width - repost_image.width) // 2
        card_y = repost_y + self.REPOST_PADDING

        # 将缩放后的转发图片贴到主画布上
        ctx.image.paste(repost_image, (card_x, card_y))

        ctx.y_pos += repost_height + self.SECTION_SPACING

    def _draw_image_grid(
        self, ctx: RenderContext, section: ImageGridSectionData
    ) -> None:
        """绘制图片网格（带圆角）"""
        images = section.images
        cols = section.cols
        rows = section.rows
        has_more = section.has_more
        remaining_count = section.remaining_count

        if not images:
            return

        available_width = ctx.content_width
        img_spacing = self.IMAGE_GRID_SPACING

        if len(images) == 1:
            max_img_size = available_width
        else:
            num_gaps = cols + 1
            calculated_size = (available_width - img_spacing * num_gaps) // cols
            max_img_size = (
                self.IMAGE_2_GRID_SIZE if cols == 2 else self.IMAGE_3_GRID_SIZE
            )
            max_img_size = min(calculated_size, max_img_size)

        current_y = ctx.y_pos

        for row in range(rows):
            row_start = row * cols
            row_end = min(row_start + cols, len(images))
            row_images = images[row_start:row_end]

            max_height = max(img.height for img in row_images)

            for i, img in enumerate(row_images):
                img_x = self.PADDING + img_spacing + i * (max_img_size + img_spacing)
                img_y = current_y + img_spacing

                y_offset = (max_height - img.height) // 2
                # 应用圆角后粘贴
                img_rgba = self._apply_rounded_corners(img, radius=8)
                ctx.image.paste(img_rgba, (img_x, img_y + y_offset), img_rgba)

                if (
                    has_more
                    and row == rows - 1
                    and i == len(row_images) - 1
                    and len(images) == self.MAX_IMAGES_DISPLAY
                ):
                    self._draw_more_indicator(
                        ctx.image,
                        img_x,
                        img_y,
                        max_img_size,
                        max_height,
                        remaining_count,
                    )

            current_y += img_spacing + max_height

        ctx.y_pos = current_y + img_spacing + self.SECTION_SPACING

    def _draw_more_indicator(
        self,
        image: PILImage,
        img_x: int,
        img_y: int,
        img_width: int,
        img_height: int,
        count: int,
    ):
        """在图片上绘制+N指示器"""
        draw = ImageDraw.Draw(image)

        # 创建半透明黑色遮罩（透明度 1/4）
        overlay = Image.new("RGBA", (img_width, img_height), (0, 0, 0, 0))
        overlay_draw = ImageDraw.Draw(overlay)
        overlay_draw.rectangle(
            (0, 0, img_width - 1, img_height - 1), fill=(0, 0, 0, 100)
        )

        # 将遮罩贴到图片上
        image.paste(overlay, (img_x, img_y), overlay)

        # 绘制+N文字
        text = f"+{count}"
        font_info = self.fontset.indicator_font
        # 计算文字位置（居中）
        text_width = font_info.get_text_width(text)
        text_x = img_x + (img_width - text_width) // 2
        text_y = img_y + (img_height - font_info.line_height) // 2

        # 绘制50%透明白色文字
        draw.text((text_x, text_y), text, fill=(255, 255, 255), font=font_info.font)

    def _draw_rounded_rectangle(
        self,
        image: PILImage,
        bbox: tuple[int, int, int, int],
        fill_color: Color,
        radius: int = 8,
    ):
        """绘制圆角矩形"""
        x1, y1, x2, y2 = bbox
        draw = ImageDraw.Draw(image)

        # 绘制主体矩形
        draw.rectangle((x1 + radius, y1, x2 - radius, y2), fill=fill_color)
        draw.rectangle((x1, y1 + radius, x2, y2 - radius), fill=fill_color)

        # 绘制四个圆角
        draw.pieslice(
            (x1, y1, x1 + 2 * radius, y1 + 2 * radius), 180, 270, fill=fill_color
        )
        draw.pieslice(
            (x2 - 2 * radius, y1, x2, y1 + 2 * radius), 270, 360, fill=fill_color
        )
        draw.pieslice(
            (x1, y2 - 2 * radius, x1 + 2 * radius, y2), 90, 180, fill=fill_color
        )
        draw.pieslice(
            (x2 - 2 * radius, y2 - 2 * radius, x2, y2), 0, 90, fill=fill_color
        )

    def _draw_rounded_rectangle_border(
        self,
        draw: ImageDraw.ImageDraw,
        bbox: tuple[int, int, int, int],
        border_color: Color,
        radius: int = 8,
        width: int = 1,
    ):
        """绘制圆角矩形边框"""
        x1, y1, x2, y2 = bbox

        # 绘制主体边框
        draw.rectangle(
            (x1 + radius, y1, x2 - radius, y1 + width), fill=border_color
        )  # 上
        draw.rectangle(
            (x1 + radius, y2 - width, x2 - radius, y2), fill=border_color
        )  # 下
        draw.rectangle(
            (x1, y1 + radius, x1 + width, y2 - radius), fill=border_color
        )  # 左
        draw.rectangle(
            (x2 - width, y1 + radius, x2, y2 - radius), fill=border_color
        )  # 右

        # 绘制四个圆角边框
        draw.arc(
            (x1, y1, x1 + 2 * radius, y1 + 2 * radius),
            180,
            270,
            fill=border_color,
            width=width,
        )
        draw.arc(
            (x2 - 2 * radius, y1, x2, y1 + 2 * radius),
            270,
            360,
            fill=border_color,
            width=width,
        )
        draw.arc(
            (x1, y2 - 2 * radius, x1 + 2 * radius, y2),
            90,
            180,
            fill=border_color,
            width=width,
        )
        draw.arc(
            (x2 - 2 * radius, y2 - 2 * radius, x2, y2),
            0,
            90,
            fill=border_color,
            width=width,
        )

    def _wrap_text(
        self, text: str | None, max_width: int, font_info: FontInfo
    ) -> list[str]:
        """优化的文本自动换行算法，考虑中英文字符宽度相同

        Args:
            text: 要处理的文本
            max_width: 最大宽度（像素）
            font_info: 字体信息对象

        Returns:
            换行后的文本列表
        """
        if not text:
            return []

        lines: list[str] = []
        paragraphs = text.splitlines()

        def is_punctuation(char: str) -> bool:
            """判断是否为不能为行首的标点符号"""
            return (
                char in "，。！？；：、）】》〉」』〕〗〙〛…—·" or char in ",.;:!?)]}"
            )

        for paragraph in paragraphs:
            if not paragraph:
                lines.append("")
                continue

            current_line = ""
            current_line_width = 0
            remaining_text = paragraph

            while remaining_text:
                next_char = remaining_text[0]
                char_width = font_info.get_char_width_fast(next_char)
                # 如果当前行为空，直接添加字符
                if not current_line:
                    current_line = next_char
                    current_line_width = char_width
                    remaining_text = remaining_text[1:]
                    continue

                # 如果是标点符号，直接添加到当前行（标点符号不应该单独成行）
                if is_punctuation(next_char):
                    current_line += next_char
                    current_line_width += char_width
                    remaining_text = remaining_text[1:]
                    continue

                # 测试添加下一个字符后的宽度
                test_width = current_line_width + char_width

                if test_width <= max_width:
                    # 宽度合适，继续添加
                    current_line += next_char
                    current_line_width = test_width
                    remaining_text = remaining_text[1:]
                else:
                    # 宽度超限，需要断行
                    lines.append(current_line)
                    current_line = next_char
                    current_line_width = char_width
                    remaining_text = remaining_text[1:]

            # 保存最后一行
            if current_line:
                lines.append(current_line)

        return lines

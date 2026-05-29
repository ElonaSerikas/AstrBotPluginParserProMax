"""ParseResult ↔ RenderPayload 双向桥接"""

import base64
import html as html_mod
import re
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Optional

from ..ai_summary import strip_html_tags
from ..data import ParseResult, Author, ImageContent, GraphicsContent, VideoContent, TextContent, Comment, MusicInfo
from .models import RenderPayload, ForwardPayload
from .constants import get_platform_color

# 允许通过 HTML 渲染的安全标签白名单
_ALLOWED_HTML_TAGS = frozenset({"br", "b", "i", "u", "a", "img", "span", "div", "p"})


def sanitize_html(html_str: str) -> str:
    """过滤危险 HTML，只保留安全标签，移除事件处理器和危险协议。"""
    if not html_str:
        return ""
    # 先将 HTML 实体解码，防止实体编码绕过（如 &#106;&#97;&#118;&#97;）
    html_str = html_mod.unescape(html_str)
    # 移除事件处理器属性 (onclick, onerror, onload 等)
    html_str = re.sub(
        r'\s+on\w+\s*=\s*(?:"[^"]*"|\'[^\']*\'|[^\s>]+)',
        "",
        html_str,
        flags=re.IGNORECASE,
    )
    # 移除危险协议的 href/src (javascript:, data:, vbscript:)
    html_str = re.sub(
        r'\s+(?:href|src)\s*=\s*(?:"(?:javascript|data|vbscript):[^"]*"|\'(?:javascript|data|vbscript):[^\']*\'|(?:javascript|data|vbscript):[^\s>]+)',
        "",
        html_str,
        flags=re.IGNORECASE,
    )
    # 移除 style 属性（防止 CSS 注入）
    html_str = re.sub(
        r'\s+style\s*=\s*(?:"[^"]*"|\'[^\']*\')',
        "",
        html_str,
        flags=re.IGNORECASE,
    )
    # 剥离非白名单标签
    def _clean_tag(m):
        full_tag = m.group(0)
        tag_match = re.match(r"</?(\w+)", full_tag)
        if tag_match and tag_match.group(1).lower() in _ALLOWED_HTML_TAGS:
            return full_tag
        return ""

    return re.sub(r"<[^>]*>", _clean_tag, html_str)


def _ensure_px(value: str | int) -> str:
    """确保卡片宽度值包含 px 后缀"""
    if isinstance(value, int):
        return f"{value}px"
    if value.isdigit():
        return f"{value}px"
    if not value.endswith("px"):
        return f"{value}px"
    return value


def _wrap_hashtags(text: str) -> str:
    """包装 #话题 为 <span class="hashtag">，跳过 HTML 标签内部的 #号"""
    if not text or "#" not in text:
        return text
    parts, pos = [], 0
    for m in re.finditer(r'<[^>]*>|#[一-鿿\w]+', text):
        parts.append(text[pos : m.start()])
        if m.group(0).startswith("<"):
            parts.append(m.group(0))
        else:
            parts.append(f'<span class="hashtag">{m.group(0)}</span>')
        pos = m.end()
    parts.append(text[pos:])
    return "".join(parts)


def parse_result_to_render_payload(
    result: ParseResult,
    card_width: str = "1440px",
    banner_base64: str = "",
) -> RenderPayload:
    """Convert ParseResult to RenderPayload for HTML rendering."""
    card_width = _ensure_px(card_width)
    platform_color = get_platform_color(result.platform.name)

    # avatar 必须为字符串 URL，不能传入 Task/Path 对象（会导致 JSON 序列化失败）
    _avatar = ""
    if result.author and result.author.avatar:
        if isinstance(result.author.avatar, str):
            _avatar = result.author.avatar
        elif isinstance(result.author.avatar, Path):
            _avatar = str(result.author.avatar)
    # 计算字体缩放因子：基于卡片宽度（基准 1000px → 1.0）
    card_width_px = int(card_width.replace("px", "")) if card_width.replace("px", "").isdigit() else 1440
    font_scale = round(card_width_px / 1200, 2)
    font_scale = max(0.8, min(font_scale, 2.0))

    payload = RenderPayload(
        name=result.author.name if result.author else "",
        avatar=_avatar,
        title=result.title or "",
        text=sanitize_html(result.text or ""),
        url=result.url or "",
        type=result.platform.name,
        platform_display=result.platform.display_name or result.platform.name,
        uid=str(result.extra.get("uid", "")) or (
            result.author.uid if result.author else ""
        ),
        banner=banner_base64,
        platform_color=platform_color,
        card_width=card_width,
        font_scale=font_scale,
    )

    # 包装 hashtags（#话题 → <span class="hashtag">）
    payload.text = _wrap_hashtags(payload.text)

    # 提取 bvid
    bvid = str(result.extra.get("bvid", "")) or ""
    if bvid:
        payload.bvid = bvid

    # 提取平台专属ID（如 Twitter @handle、小红书号等）
    handle = str(result.extra.get("handle", "")) or ""
    if handle:
        payload.handle = handle

    # 提取发布时间戳
    ts = getattr(result, "timestamp", None)
    if ts:
        payload.timestamp = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")

    # 作者扩展信息
    if result.author:
        payload.signature = result.author.description or ""
        payload.follower_count = (
            str(result.author.follower_count)
            if result.author.follower_count
            else ""
        )

    # 映射图片（排除二维码，HTML渲染器自己生成QR）
    for cont in result.contents:
        if isinstance(cont, ImageContent) and cont.is_qr:
            continue
        if isinstance(cont, (ImageContent, GraphicsContent)):
            # 优先使用 source_url（原始链接，无需下载）
            if cont.source_url and isinstance(cont.source_url, str):
                payload.image_urls.append(cont.source_url)
            # 兼容旧路径：部分代码将字符串 URL 存入 path_task
            elif isinstance(cont.path_task, str) and cont.path_task.startswith("http"):
                payload.image_urls.append(cont.path_task)

    # 视频封面（VideoContent 的 cover）
    for cont in result.contents:
        if isinstance(cont, VideoContent):
            # 优先使用原始封面 URL（用于 HTML 渲染）
            if cont.cover_url:
                payload.image_urls.append(cont.cover_url)
            elif cont.cover and isinstance(cont.cover, str):
                payload.image_urls.append(cont.cover)
            # Task[Path] / Path — 本地路径不能用于 HTML 渲染，跳过

    # 统计数据
    if result.stats:
        payload.stats = {
            "views": _fmt_stat(result.stats.get("views")),
            "danmaku": _fmt_stat(result.stats.get("danmaku")),
            "likes": _fmt_stat(result.stats.get("likes")),
            "favorites": _fmt_stat(result.stats.get("favorites")),
            "coins": _fmt_stat(result.stats.get("coins")),
            "comments": _fmt_stat(result.stats.get("comments")),
            "reposts": _fmt_stat(result.stats.get("reposts")),
            "quotes": _fmt_stat(result.stats.get("quotes")),
            "bookmarks": _fmt_stat(result.stats.get("bookmarks")),
        }

    # 生成二维码
    if result.url:
        try:
            import qrcode
            import base64
            from io import BytesIO
            qr = qrcode.make(result.url)
            buf = BytesIO()
            qr.save(buf, format="PNG")
            qr_b64 = base64.b64encode(buf.getvalue()).decode()
            payload.qrcode = f"data:image/png;base64,{qr_b64}"
        except Exception:
            pass

    # 置顶评论
    if result.pinned_comment:
        payload.pinned_comment = {
            "author": result.pinned_comment.author_name,
            "content": result.pinned_comment.content,
            "likes": str(result.pinned_comment.likes),
            "time": _fmt_comment_time(result.pinned_comment.timestamp),
        }

    # 热评（与置顶评论不同的热门评论）
    if result.hot_comment:
        payload.hot_comment = {
            "author": result.hot_comment.author_name,
            "content": result.hot_comment.content,
            "likes": str(result.hot_comment.likes),
            "time": _fmt_comment_time(result.hot_comment.timestamp),
        }

    # 评论列表
    if result.comments:
        payload.comments = [
            {
                "author": c.author_name,
                "content": c.content,
                "likes": str(c.likes),
                "time": _fmt_comment_time(c.timestamp),
                "is_hot": c.is_hot,
            }
            for c in result.comments[:10]
        ]

    # 递归 repost
    if result.repost:
        payload.forward = _result_to_forward(result.repost)

    return payload


def _result_to_forward(result: ParseResult) -> ForwardPayload:
    # 收集转发中的图片 URL
    fwd_image_urls: list[str] = []
    for c in result.contents:
        if isinstance(c, (ImageContent, GraphicsContent)):
            if c.source_url and isinstance(c.source_url, str):
                fwd_image_urls.append(c.source_url)
            elif isinstance(c.path_task, str) and c.path_task.startswith("http"):
                fwd_image_urls.append(c.path_task)
        elif isinstance(c, VideoContent) and c.cover_url:
            fwd_image_urls.append(c.cover_url)

    _fwd_avatar = ""
    if result.author and result.author.avatar:
        if isinstance(result.author.avatar, str):
            _fwd_avatar = result.author.avatar
        elif isinstance(result.author.avatar, Path):
            _fwd_avatar = str(result.author.avatar)
    return ForwardPayload(
        name=result.author.name if result.author else "",
        avatar=_fwd_avatar,
        text=_wrap_hashtags(sanitize_html(result.text or "")),
        image_urls=fwd_image_urls,
        url=result.url or "",
        title=result.title or "",
        type=result.platform.name,
        platform_display=result.platform.display_name or result.platform.name,
    )


def render_payload_to_parse_result(
    payload: RenderPayload,
    platform_name: str = "bilibili",
) -> ParseResult:
    """Convert RenderPayload back to ParseResult (for subscription push path)."""
    from ..data import Platform, Author

    result = ParseResult(
        platform=Platform(name=platform_name, display_name=payload.platform_display or payload.type or platform_name),
        author=Author(name=payload.name) if payload.name else None,
        title=payload.title,
        text=strip_html_tags(payload.text),
        url=payload.url,
        extra={"uid": payload.uid, "type": payload.type, "summary": payload.summary},
    )
    for url in payload.image_urls:
        if url.startswith("http"):
            result.contents.append(ImageContent(url))
    if payload.forward:
        repost = ParseResult(
            platform=result.platform,
            author=Author(name=payload.forward.name) if payload.forward.name else None,
            title=payload.forward.title,
            text=payload.forward.text,
            url=payload.forward.url,
        )
        result.repost = repost
    return result


def _fmt_stat(value: Optional[int]) -> str:
    if value is None:
        return ""
    return str(value)


def _fmt_comment_time(ts: Optional[int]) -> str:
    if ts is None:
        return ""
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


async def download_images_to_data_uri(
    payload: RenderPayload,
    session=None,
    headers: dict | None = None,
    proxy: str | None = None,
    timeout: int = 10,
) -> None:
    """将 payload 中的远程图片 URL 下载为 base64 data URI，绕过防盗链。

    直接修改 payload.image_urls 和 payload.avatar。
    失败的 URL 保留原样（不阻塞渲染）。
    """
    import asyncio
    from aiohttp import ClientSession, ClientTimeout

    async def _fetch_one(url: str) -> str | None:
        if not url.startswith("http"):
            return url
        try:
            if session:
                async with session.get(url, headers=headers, proxy=proxy, timeout=ClientTimeout(total=timeout)) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.read()
            else:
                async with ClientSession() as sess:
                    async with sess.get(url, headers=headers, proxy=proxy, timeout=ClientTimeout(total=timeout)) as resp:
                        if resp.status != 200:
                            return None
                        data = await resp.read()
            # 检测 content-type
            ct = resp.headers.get("content-type", "image/jpeg")
            if ";" in ct:
                ct = ct.split(";")[0].strip()
            b64 = base64.b64encode(data).decode()
            return f"data:{ct};base64,{b64}"
        except Exception:
            return None

    # 下载头像
    if payload.avatar and payload.avatar.startswith("http"):
        result = await _fetch_one(payload.avatar)
        if result:
            payload.avatar = result

    # 下载图片列表
    if payload.image_urls:
        results = await asyncio.gather(
            *[_fetch_one(u) for u in payload.image_urls],
            return_exceptions=True,
        )
        payload.image_urls = [
            (r if isinstance(r, str) and r else orig)
            for r, orig in zip(results, payload.image_urls)
        ]

    # 下载转发中的图片
    if payload.forward and payload.forward.image_urls:
        results = await asyncio.gather(
            *[_fetch_one(u) for u in payload.forward.image_urls],
            return_exceptions=True,
        )
        payload.forward.image_urls = [
            (r if isinstance(r, str) and r else orig)
            for r, orig in zip(results, payload.forward.image_urls)
        ]

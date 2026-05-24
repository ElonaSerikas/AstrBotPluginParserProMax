"""ParseResult ↔ RenderPayload 双向桥接"""

import html as html_mod
import re
from typing import Optional

from ..ai_summary import strip_html_tags
from ..data import ParseResult, Author, ImageContent, TextContent, Comment, MusicInfo
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
    # 剥离非白名单标签
    def _clean_tag(m):
        full_tag = m.group(0)
        tag_match = re.match(r"</?(\w+)", full_tag)
        if tag_match and tag_match.group(1).lower() in _ALLOWED_HTML_TAGS:
            return full_tag
        return ""

    return re.sub(r"<[^>]*>", _clean_tag, html_str)


def parse_result_to_render_payload(
    result: ParseResult,
    card_width: str = "1440px",
    banner_base64: str = "",
) -> RenderPayload:
    """Convert ParseResult to RenderPayload for HTML rendering."""
    platform_color = get_platform_color(result.platform.name)

    payload = RenderPayload(
        name=result.author.name if result.author else "",
        avatar=(
            result.author.avatar
            if result.author and result.author.avatar
            else ""
        ),
        title=result.title or "",
        text=sanitize_html(result.text or ""),
        url=result.url or "",
        type=result.platform.name,
        uid=str(result.extra.get("uid", "")) or (
            result.author.uid if result.author else ""
        ),
        banner=banner_base64,
        platform_color=platform_color,
        card_width=card_width,
    )

    # 作者扩展信息
    if result.author:
        payload.signature = result.author.description or ""
        payload.follower_count = (
            str(result.author.follower_count)
            if result.author.follower_count
            else ""
        )

    # 映射图片
    for cont in result.contents:
        if isinstance(cont, ImageContent):
            # 优先使用 source_url（原始链接，无需下载）
            if cont.source_url and isinstance(cont.source_url, str):
                payload.image_urls.append(cont.source_url)
            # 兼容旧路径：部分代码将字符串 URL 存入 path_task
            elif isinstance(cont.path_task, str) and cont.path_task.startswith("http"):
                payload.image_urls.append(cont.path_task)

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
        }

    # 置顶评论
    if result.pinned_comment:
        payload.pinned_comment = {
            "author": result.pinned_comment.author_name,
            "content": result.pinned_comment.content,
            "likes": str(result.pinned_comment.likes),
            "time": _fmt_comment_time(result.pinned_comment.timestamp),
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
        if isinstance(c, ImageContent):
            if c.source_url and isinstance(c.source_url, str):
                fwd_image_urls.append(c.source_url)
            elif isinstance(c.path_task, str) and c.path_task.startswith("http"):
                fwd_image_urls.append(c.path_task)

    return ForwardPayload(
        name=result.author.name if result.author else "",
        avatar=(
            result.author.avatar
            if result.author and result.author.avatar
            else ""
        ),
        text=sanitize_html(result.text or ""),
        image_urls=fwd_image_urls,
        url=result.url or "",
        title=result.title or "",
        type=result.platform.name,
    )


def render_payload_to_parse_result(
    payload: RenderPayload,
    platform_name: str = "bilibili",
) -> ParseResult:
    """Convert RenderPayload back to ParseResult (for subscription push path)."""
    from ..data import Platform, Author

    result = ParseResult(
        platform=Platform(name=platform_name, display_name=payload.type or "B站"),
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
    if value >= 10000:
        return f"{value / 10000:.1f}万"
    return str(value)


def _fmt_comment_time(ts: Optional[int]) -> str:
    if ts is None:
        return ""
    from datetime import datetime

    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")

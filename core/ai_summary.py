"""
AI 内容概况与总结系统。

为所有已解析内容生成 AI 摘要，支持图片渲染和纯文本两种模式。
"""

import re
from typing import Any, Optional

from astrbot.api import logger


def strip_html_tags(text: str) -> str:
    """去除 HTML 标签"""
    return re.sub(r"<[^>]+>", "", text).strip()


def build_summary_prompt(
    platform: str,
    title: str = "",
    text: str = "",
    author: str = "",
    page_type: str = "",
    stats: Optional[dict[str, Any]] = None,
    comments: Optional[list[Any]] = None,
) -> str:
    """构建 AI 摘要提示词

    Args:
        platform: 平台名称
        title: 标题
        text: 正文
        author: 作者名
        page_type: 页面类型(video/article/dynamic等)
        stats: 统计数据
        comments: 评论列表

    Returns:
        格式化提示词
    """
    parts = [f"请用简洁的中文概括以下{platform}内容（100字以内）："]

    if author:
        parts.append(f"\n作者: {author}")
    if title:
        parts.append(f"\n标题: {title}")
    if page_type:
        parts.append(f"\n类型: {page_type}")
    if text:
        clean = strip_html_tags(text)[:500]
        parts.append(f"\n正文: {clean}")

    if stats:
        stat_str = "、".join(
            f"{k}: {v}" for k, v in stats.items() if v
        )
        if stat_str:
            parts.append(f"\n数据: {stat_str}")

    if comments:
        top = comments[:3]
        comment_str = " | ".join(
            f"{c.author_name}: {strip_html_tags(c.content)[:80]}"
            for c in top
        )
        if comment_str:
            parts.append(f"\n热门评论: {comment_str}")

    parts.append("\n\n请输出格式：\n📌 一句话概括\n📊 关键看点\n💬 评论风向")
    return "\n".join(parts)


def build_text_summary(
    summary: str,
    url: str = "",
    platform: str = "",
    stats: Optional[dict[str, Any]] = None,
) -> str:
    """构建纯文本模式的 AI 摘要文案

    Args:
        summary: AI 返回的摘要
        url: 原文链接
        platform: 平台名
        stats: 统计数据

    Returns:
        格式化后的文本摘要
    """
    lines = ["🤖 AI 内容概况", "━" * 20]
    lines.append(summary)

    if stats:
        line = " | ".join(
            f"{k}: {v}" for k, v in stats.items() if v
        )
        if line:
            lines.append(f"\n📊 {line}")

    if url:
        lines.append(f"\n🔗 {url}")

    return "\n".join(lines)


async def generate_summary(
    context,
    sub_user: str,
    prompt: str,
    session_id: Optional[str] = None,
) -> str | None:
    """调用 LLM 生成摘要

    Args:
        context: AstrBot Context
        sub_user: 统一消息来源
        prompt: 提示词
        session_id: 会话 ID（用于持久化）

    Returns:
        摘要文本或 None
    """
    try:
        result = await context.llm_generate(
            prompt_text=prompt,
            session_id=session_id or sub_user,
            image_urls=None,
        )
        if result:
            return str(result).strip()
    except Exception as e:
        logger.warning(f"[AI摘要] 生成失败: {e}")
    return None


async def generate_and_persist_summary(
    context,
    sub_user: str,
    prompt: str,
    session_id: str,
) -> str | None:
    """生成摘要并持久化到会话历史"""
    summary = await generate_summary(context, sub_user, prompt, session_id)
    if summary:
        try:
            await context.conversation_append_message(
                session_id=session_id,
                role="user",
                msg=prompt[:200],
            )
            await context.conversation_append_message(
                session_id=session_id,
                role="assistant",
                msg=summary,
            )
        except Exception as e:
            logger.debug(f"[AI摘要] 持久化失败: {e}")
    return summary

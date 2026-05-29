"""Telegram 频道/消息解析器"""

import re
from datetime import datetime, timezone
from typing import Any, ClassVar

from bs4 import BeautifulSoup, Tag
from astrbot.api import logger

from ..config import PluginConfig
from ..data import Comment, ImageContent, ParseResult, Platform, VideoContent
from ..download import Downloader
from .base import BaseParser, ParseException, handle


def _parse_count(text: str) -> int | None:
    """解析 Telegram 显示的数字格式：'10.6M' → 10600000, '716K' → 716000"""
    if not text:
        return None
    text = text.strip().replace(",", "").replace(" ", "")
    multipliers = {"K": 1000, "M": 1_000_000, "B": 1_000_000_000}
    for suffix, mult in multipliers.items():
        if text.upper().endswith(suffix):
            try:
                return int(float(text[:-1]) * mult)
            except (ValueError, TypeError):
                return None
    try:
        return int(text)
    except (ValueError, TypeError):
        return None


def _extract_bg_url(style: str) -> str | None:
    """从 CSS background-image: url(...) 中提取 URL"""
    if not style:
        return None
    m = re.search(r"url\(['\"]?(.*?)['\"]?\)", style)
    return m.group(1) if m else None


class TelegramParser(BaseParser):
    """Telegram 频道消息解析器"""

    platform: ClassVar[Platform] = Platform(name="telegram", display_name="Telegram")

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.mycfg = config.parser.telegram

    # ── 公开频道 ──────────────────────────────────────────────
    @handle(
        "t.me",
        r"https?://(?:www\.)?t\.me/(?P<channel>[a-zA-Z_][a-zA-Z0-9_]*)(?:/(?P<post_id>\d+))?/?(?:\?.*)?$",
    )
    async def _parse_public(self, searched: re.Match[str]) -> ParseResult:
        channel = searched.group("channel")
        post_id = searched.group("post_id")
        url = searched.group(0)

        # 排除 t.me/c/ 私有频道（由 _parse_private 处理）
        if channel == "c":
            raise ParseException("私有频道链接，跳过")

        # 频道链接（无 post_id）→ 用 /s/ 预览页获取频道信息
        if not post_id:
            preview_url = f"https://t.me/s/{channel}"
        else:
            preview_url = f"https://t.me/s/{channel}/{post_id}"
        try:
            html = await self._fetch_page(preview_url)
        except Exception as e:
            logger.warning(f"Telegram 抓取失败({preview_url}): {e}")
            return self.result(title="[Telegram 抓取失败]", url=url, text=str(e))

        soup = BeautifulSoup(html, "html.parser")
        return self._parse_page(soup, url, channel, post_id)

    # ── 私有频道 ──────────────────────────────────────────────
    @handle(
        "t.me/c",
        r"https?://(?:www\.)?t\.me/c/(?P<chat_id>\d+)/(?P<post_id>\d+)/?(?:\?.*)?$",
    )
    async def _parse_private(self, searched: re.Match[str]) -> ParseResult:
        chat_id = searched.group("chat_id")
        post_id = searched.group("post_id")
        url = searched.group(0)

        # TODO: 如果配置了 bot_token，用 python-telegram-bot 获取
        return self.result(
            title="[私有频道]",
            text="此为私有 Telegram 频道消息。配置 bot_token 后可解析。",
            url=url,
            extra={"chat_id": chat_id, "post_id": post_id, "private": True},
        )

    # ── Web 客户端 URL ────────────────────────────────────────
    @handle(
        "web.telegram.org",
        r"https?://web\.telegram\.org/a/#-(?P<chat_id>\d+)/(?P<post_id>\d+)",
    )
    async def _parse_web_url(self, searched: re.Match[str]) -> ParseResult:
        chat_id = searched.group("chat_id")
        post_id = searched.group("post_id")
        url = searched.group(0)
        return self.result(
            title="[私有频道]",
            text="此为 Telegram Web 客户端链接。配置 bot_token 后可解析。",
            url=url,
            extra={"chat_id": chat_id, "post_id": post_id, "private": True},
        )

    # ── 页面抓取 ──────────────────────────────────────────────
    async def _fetch_page(self, url: str) -> str:
        """抓取 t.me/s/ 预览页面"""
        from .anti_ban import get_anti_ban
        anti_ban = get_anti_ban()
        await anti_ban.wait_if_needed("telegram", min_interval=3.0)
        headers = anti_ban.get_headers("telegram", base_headers=self.headers)
        async with self.session.get(url, headers=headers, proxy=self.proxy) as resp:
            if resp.status == 429:
                anti_ban.record_failure("telegram", is_rate_limit=True)
                raise ParseException(f"Telegram 限流 (429)")
            if resp.status != 200:
                anti_ban.record_failure("telegram")
                raise ParseException(f"Telegram HTTP {resp.status}")
            anti_ban.record_success("telegram")
            return await resp.text()

    # ── HTML 解析 ─────────────────────────────────────────────
    def _parse_page(
        self, soup: BeautifulSoup, url: str, channel: str, post_id: str | None
    ) -> ParseResult:
        """从 t.me/s/ 预览页解析完整信息"""

        # ── 频道信息（侧边栏）──
        channel_name = ""
        channel_avatar = ""
        channel_username = ""
        subscriber_count = None
        channel_desc = ""

        info = soup.select_one(".tgme_channel_info")
        if info:
            title_el = info.select_one(".tgme_channel_info_header_title span[dir=auto]")
            if title_el:
                channel_name = title_el.get_text(strip=True)

            avatar_el = info.select_one(".tgme_page_photo_image img")
            if avatar_el and avatar_el.get("src"):
                channel_avatar = avatar_el["src"]

            username_el = info.select_one(".tgme_channel_info_header_username a")
            if username_el:
                channel_username = username_el.get_text(strip=True)

            desc_el = info.select_one(".tgme_channel_info_description")
            if desc_el:
                channel_desc = desc_el.get_text(strip=True)

            # 订阅数
            for counter in info.select(".tgme_channel_info_counter"):
                type_el = counter.select_one(".counter_type")
                value_el = counter.select_one(".counter_value")
                if type_el and value_el and "subscriber" in type_el.get_text().lower():
                    subscriber_count = _parse_count(value_el.get_text())
                    break

        # ── 频道链接（无 post_id）→ 只返回频道信息 ──
        if not post_id:
            author = self.create_author(
                channel_name or channel,
                avatar=channel_avatar or None,
                uid=channel,
                description=channel_desc or None,
                follower_count=subscriber_count,
            )
            return self.result(
                title=f"Telegram 频道: {channel_name or channel}",
                text=channel_desc or None,
                url=url,
                author=author,
                extra={
                    "channel": channel,
                    "username": channel_username,
                    "subscriber_count": subscriber_count,
                },
            )

        # ── 帖子内容 ──
        msg = soup.select_one(f'.tgme_widget_message[data-post="{channel}/{post_id}"]')
        if not msg:
            # 如果找不到精确匹配，取页面上最后一个消息
            messages = soup.select(".tgme_widget_message")
            msg = messages[-1] if messages else None

        if not msg:
            return self.result(
                title=f"Telegram #{channel}/{post_id}",
                text="未找到消息内容",
                url=url,
                author=self.create_author(
                    channel_name or channel,
                    avatar=channel_avatar or None,
                    uid=channel,
                    description=channel_desc or None,
                    follower_count=subscriber_count,
                ),
                extra={"channel": channel, "post_id": post_id},
            )

        return self._parse_message(msg, url, channel, post_id, channel_name, channel_avatar, channel_username, subscriber_count, channel_desc)

    def _parse_message(
        self,
        msg: Tag,
        url: str,
        channel: str,
        post_id: str,
        channel_name: str,
        channel_avatar: str,
        channel_username: str,
        subscriber_count: int | None,
        channel_desc: str,
    ) -> ParseResult:
        """解析单条消息的所有字段"""

        # ── 作者信息 ──
        author_name = channel_name
        author_avatar = channel_avatar

        owner_el = msg.select_one(".tgme_widget_message_owner_name span[dir=auto]")
        if owner_el:
            author_name = owner_el.get_text(strip=True)

        user_photo_el = msg.select_one(".tgme_widget_message_user_photo img")
        if user_photo_el and user_photo_el.get("src"):
            author_avatar = user_photo_el["src"]

        author = self.create_author(
            author_name or channel,
            avatar=author_avatar or None,
            uid=channel,
            description=channel_desc or None,
            follower_count=subscriber_count,
        )

        # ── 正文 ──
        text = ""
        text_el = msg.select_one(".tgme_widget_message_text")
        if text_el:
            text = text_el.get_text("\n", strip=True)

        # ── 发布时间 ──
        timestamp = None
        time_el = msg.select_one(".tgme_widget_message_date time[datetime]")
        if time_el:
            try:
                dt = datetime.fromisoformat(time_el["datetime"])
                timestamp = int(dt.timestamp())
            except (ValueError, TypeError, KeyError):
                pass

        # ── 浏览量 ──
        view_count = None
        views_el = msg.select_one(".tgme_widget_message_views")
        if views_el:
            view_count = _parse_count(views_el.get_text())

        # ── 编辑标记 ──
        is_edited = False
        meta_el = msg.select_one(".tgme_widget_message_meta")
        if meta_el and "edited" in meta_el.get_text().lower():
            is_edited = True

        # ── 来源作者（转发场景）──
        from_author = ""
        from_author_el = msg.select_one(".tgme_widget_message_from_author")
        if from_author_el:
            from_author = from_author_el.get_text(strip=True)

        # ── 图片 ──
        image_urls: list[str] = []
        # 单图
        for photo_wrap in msg.select(".tgme_widget_message_photo_wrap"):
            style = photo_wrap.get("style", "")
            img_url = _extract_bg_url(style)
            if img_url and img_url not in image_urls:
                image_urls.append(img_url)

        image_count = len(image_urls)

        # ── 视频 ──
        video_url = None
        video_cover = None
        video_duration = ""

        video_el = msg.select_one("video.tgme_widget_message_video")
        if video_el and video_el.get("src"):
            video_url = video_el["src"]

        video_thumb = msg.select_one(".tgme_widget_message_video_thumb")
        if video_thumb:
            style = video_thumb.get("style", "")
            video_cover = _extract_bg_url(style)

        duration_el = msg.select_one(".message_video_duration")
        if duration_el:
            video_duration = duration_el.get_text(strip=True)

        # ── 反应/点赞 ──
        reaction_count = 0
        reactions_container = msg.select_one(".tgme_widget_message_reactions")
        if reactions_container:
            for reaction_el in reactions_container.select(".tgme_reaction"):
                count_text = reaction_el.get_text(strip=True)
                # 提取数字部分（可能在 emoji 之后）
                nums = re.findall(r"[\d.]+[KMBkmb]?", count_text)
                for n in nums:
                    parsed = _parse_count(n)
                    if parsed:
                        reaction_count += parsed

        # ── 链接预览 ──
        link_preview = None
        link_el = msg.select_one(".tgme_widget_message_link_preview")
        if link_el:
            link_url = link_el.get("href", "")
            link_title_el = link_el.select_one(".link_preview_title")
            link_desc_el = link_el.select_one(".link_preview_description")
            link_site_el = link_el.select_one(".link_preview_site_name")
            link_img_el = link_el.select_one(".link_preview_image")

            link_preview = {
                "url": link_url,
                "title": link_title_el.get_text(strip=True) if link_title_el else "",
                "description": link_desc_el.get_text(strip=True) if link_desc_el else "",
                "site": link_site_el.get_text(strip=True) if link_site_el else "",
                "image": _extract_bg_url(link_img_el.get("style", "")) if link_img_el else "",
            }

        # ── 转发消息 ──
        repost = None
        forward_el = msg.select_one(".tgme_widget_message_forward")
        if forward_el:
            fwd_author_el = forward_el.select_one(".tgme_widget_message_forward_author")
            fwd_text_el = forward_el.select_one(".tgme_widget_message_text")
            fwd_author_name = fwd_author_el.get_text(strip=True) if fwd_author_el else ""
            fwd_text = fwd_text_el.get_text("\n", strip=True) if fwd_text_el else ""
            if fwd_author_name or fwd_text:
                repost = self.result(
                    title=fwd_text[:100] if fwd_text else None,
                    text=fwd_text,
                    author=self.create_author(fwd_author_name) if fwd_author_name else None,
                )

        # ── 构建内容列表 ──
        contents: list[Any] = []
        if image_urls:
            contents.extend(self.create_image_contents(image_urls))
        if video_url:
            contents.append(self.create_video_content(video_url, video_cover, 0))

        # ── 统计数据 ──
        stats: dict[str, int] = {}
        if view_count:
            stats["views"] = view_count
        if reaction_count:
            stats["likes"] = reaction_count  # reactions 映射为「赞」（标准 stats key）

        # ── 标题 ──
        title = text[:100] if text else None

        # ── 额外信息 ──
        extra: dict[str, Any] = {
            "uid": channel,
            "channel": channel,
            "post_id": post_id,
        }
        if channel_username:
            extra["handle"] = channel_username
        if is_edited:
            extra["edited"] = True
        if video_duration:
            extra["video_duration"] = video_duration
        if image_count > 1:
            extra["image_count"] = image_count
        if from_author:
            extra["from_author"] = from_author
        if link_preview:
            extra["link_preview"] = link_preview

        return self.result(
            title=title,
            text=text or None,
            author=author,
            url=url,
            contents=contents,
            timestamp=timestamp,
            stats=stats or None,
            extra=extra,
            repost=repost,
            page_type="message",
        )

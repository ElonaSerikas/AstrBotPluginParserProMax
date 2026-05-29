import asyncio
import re
from typing import ClassVar

import msgspec
import yt_dlp
from aiohttp import ClientError
from msgspec import Struct

from astrbot.api import logger

from ..config import PluginConfig
from ..cookie import CookieJar
from ..data import Author, Comment, ParseResult
from ..download import Downloader
from .base import BaseParser, Platform, handle


class YouTubeParser(BaseParser):
    # 平台信息
    platform: ClassVar[Platform] = Platform(name="youtube", display_name="YouTube")

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.mycfg = config.parser.youtube
        if not self.mycfg.cookies:
            logger.warning("油管Cookie未配置，将无法解析相关媒体")
        self.headers.update({"Referer": "https://www.youtube.com/"})
        self.cookiejar = CookieJar(config, self.mycfg, domain="youtube.com")

    @handle("youtu", r"youtu\.be/[A-Za-z\d\._\?%&\+\-=/#]+\/?(?:\?.*)?$")
    @handle(
        "youtube",
        r"youtube\.com/(?:watch|shorts)(?:/[A-Za-z\d_\-]+|\?v=[A-Za-z\d_\-]+(?:&.*)?)(?:\/?(?:\?.*)?)?$",
    )
    @handle(
        "youtube.com/live",
        r"youtube\.com/live/(?P<video_id>[A-Za-z\d_-]{11})\/?(?:\?.*)?$",
    )
    async def _parse_video(self, searched: re.Match[str]):
        return await self.parse_video(searched)

    # YouTube 频道/用户主页
    @handle("youtube.com/channel", r"youtube\.com/channel/(?P<channel_id>[A-Za-z0-9_-]+)\/?(?:\?.*)?$")
    @handle("youtube.com/@handle", r"youtube\.com/@(?P<handle>[A-Za-z0-9_.-]+)(?:/(?:videos|shorts|streams|releases|playlists|posts|featured))?(?:\?.*)?$")
    async def _parse_channel(self, searched: re.Match[str]):
        """解析 YouTube 频道主页"""
        channel_id = searched.group("channel_id") if "channel_id" in searched.groupdict() else None
        handle = searched.group("handle") if "handle" in searched.groupdict() else None
        return await self.parse_channel(channel_id=channel_id, handle=handle)

    async def parse_channel(self, channel_id: str = None, handle: str = None):
        """解析 YouTube 频道信息"""
        # 如果只有 handle，需要先获取 channel_id
        if not channel_id and handle:
            try:
                url = f"https://www.youtube.com/@{handle}"
                async with self.session.get(url, headers=self.headers, proxy=self.proxy) as resp:
                    html = await resp.text()
                    # 从 HTML 中提取 channel_id
                    m = re.search(r'"channelId"\s*:\s*"([A-Za-z0-9_-]+)"', html)
                    if m:
                        channel_id = m.group(1)
            except Exception as e:
                logger.debug(f"YouTube 获取 channel_id 失败: {e}")

        if not channel_id:
            raise ParseException("无法获取 YouTube 频道 ID")

        # 使用 browse API 获取频道信息
        author, channel_handle = await self._fetch_author_info(channel_id)

        if not author:
            raise ParseException("YouTube 频道信息获取失败")

        # 获取频道描述
        description = author.description or ""

        return self.result(
            title=f"{author.name} - YouTube",
            text=description[:500] if description else f"{author.name} 的 YouTube 频道",
            author=author,
            contents=[],  # 频道主页不下载视频
            url=f"https://www.youtube.com/channel/{channel_id}",
            extra={
                "handle": channel_handle or f"@{handle}" if handle else "",
                "channel_id": channel_id,
            },
        )

    async def parse_video(self, searched: re.Match[str]):
        # 从匹配对象中获取原始URL
        url = searched.group(0)

        # 移除 list 参数（YouTube Mix 自动生成播放列表），避免 yt-dlp 尝试解析播放列表
        from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
        parsed = urlparse(url)
        qs = parse_qs(parsed.query, keep_blank_values=True)
        qs.pop("list", None)
        clean_query = urlencode(qs, doseq=True)
        url = urlunparse(parsed._replace(query=clean_query))

        # yt-dlp 提取（可降级）
        video_info = None
        try:
            video_info = await self.downloader.ytdlp_extract_info(
                url,
                cookiefile=self.cookiejar.cookie_file,
                headers=self.headers,
                proxy=self.proxy,
            )
        except Exception as e:
            logger.warning(f"YouTube 标准解析失败(url={url}): {e}")
            # 回退 1: 尝试使用 android client
            try:
                android_opts = {
                    "quiet": True,
                    "extractor_args": {"youtube": {"player_client": ["android"]}},
                    "cookiefile": self.cookiejar.cookie_file,
                    "http_headers": self.headers,
                    "socket_timeout": 20,
                }
                if self.proxy:
                    android_opts["proxy"] = self.proxy
                with yt_dlp.YoutubeDL(android_opts) as ydl:
                    video_info = ydl.extract_info(url, download=False)
                logger.info(f"YouTube android client 回退成功: {url}")
            except Exception as e2:
                logger.warning(f"YouTube android client 回退失败: {e2}")

        # 回退 2: 元数据提取（并发尝试多种 client，取第一个成功结果）
        if not video_info:
            def _try_meta_client(client: str):
                meta_opts = {
                    "quiet": True,
                    "listformats": True,
                    "cookiefile": self.cookiejar.cookie_file,
                    "http_headers": self.headers,
                    "socket_timeout": 15,
                    "extractor_args": {"youtube": {"player_client": [client]}},
                }
                if self.proxy:
                    meta_opts["proxy"] = self.proxy
                with yt_dlp.YoutubeDL(meta_opts) as ydl:
                    return ydl.extract_info(url, download=False), client

            pending_tasks = set()
            for client in ["web", "tv", "mweb"]:
                pending_tasks.add(asyncio.to_thread(_try_meta_client, client))

            try:
                for coro in asyncio.as_completed(pending_tasks, timeout=30):
                    try:
                        meta, client = await coro
                        # 取消其他未完成的任务
                        for t in pending_tasks:
                            t.cancel()
                        # 使用频道头像作为头像（不用视频封面）
                        avatar_url = meta.get("channel_avatar") or meta.get("uploader_avatar")
                        author = self.create_author(
                            meta.get("channel", "") or meta.get("uploader", ""),
                            avatar=avatar_url,
                            uid=meta.get("channel_id", ""),
                            description=meta.get("description"),
                            follower_count=meta.get("channel_follower_count"),
                        )
                        contents = []
                        if meta.get("thumbnail"):
                            contents.extend(self.create_image_contents([meta["thumbnail"]]))
                        stats = {}
                        if meta.get("view_count"):
                            stats["views"] = meta["view_count"]
                        if meta.get("like_count"):
                            stats["likes"] = meta["like_count"]
                        if meta.get("comment_count"):
                            stats["comments"] = meta["comment_count"]
                        logger.info(f"YouTube 元数据提取成功(client={client}): {url}")
                        vid_m = re.search(r"(?:v=|youtu\.be/|shorts/|live/)([a-zA-Z0-9_-]{11})", url)
                        extra_meta = {"video_id": vid_m.group(1), "post_id": vid_m.group(1)} if vid_m else {}
                        return self.result(
                            title=meta.get("title", "[解析失败]"),
                            text=meta.get("description") or None,
                            author=author,
                            contents=contents,
                            url=url,
                            stats=stats,
                            timestamp=meta.get("timestamp"),
                            extra=extra_meta,
                        )
                    except Exception as e3:
                        logger.debug(f"YouTube 元数据提取失败: {e3}")
                        continue
            except (asyncio.TimeoutError, TimeoutError):
                logger.debug("YouTube 元数据提取并发超时")
                for t in pending_tasks:
                    t.cancel()

            # 所有 client 都失败，回退到 embed 页面
            logger.warning(f"YouTube 所有元数据提取均失败，回退到 embed")
            return await self._fallback_embed(url)

        # 检查标准提取结果的完整性：如果缺少关键字段，尝试元数据回退补充
        if not video_info.description and video_info.view_count is None and video_info.like_count is None:
            logger.warning(f"YouTube 标准提取缺少 description/stats，尝试元数据回退补充: {url}")
            try:
                meta_opts = {
                    "quiet": True,
                    "cookiefile": self.cookiejar.cookie_file,
                    "http_headers": self.headers,
                    "socket_timeout": 15,
                    "extractor_args": {"youtube": {"player_client": ["web"]}},
                }
                if self.proxy:
                    meta_opts["proxy"] = self.proxy
                with yt_dlp.YoutubeDL(meta_opts) as ydl:
                    meta = ydl.extract_info(url, download=False)
                if meta:
                    if not video_info.description and meta.get("description"):
                        video_info.description = meta["description"]
                    if video_info.view_count is None and meta.get("view_count") is not None:
                        video_info.view_count = meta["view_count"]
                    if video_info.like_count is None and meta.get("like_count") is not None:
                        video_info.like_count = meta["like_count"]
                    if video_info.comment_count is None and meta.get("comment_count") is not None:
                        video_info.comment_count = meta["comment_count"]
                    if video_info.timestamp == 0 and meta.get("timestamp"):
                        video_info.timestamp = meta["timestamp"]
                    logger.info(f"YouTube 元数据补充成功: description={bool(video_info.description)}, views={video_info.view_count}")
            except Exception as e:
                logger.debug(f"YouTube 元数据补充失败: {e}")

        # 作者信息 + 置顶评论（并发获取）
        author = None
        pinned_comment, hot_comment = None, None
        channel_handle = None
        vid_match = re.search(r"(?:v=|youtu\.be/|shorts/|live/)([a-zA-Z0-9_-]{11})", url)
        video_id = vid_match.group(1) if vid_match else ""
        aux_tasks = [self._fetch_author_info(video_info.channel_id)]
        if video_id:
            aux_tasks.append(self._get_pinned_comment(video_id))
        aux_results = await asyncio.gather(*aux_tasks, return_exceptions=True)
        for r in aux_results:
            if isinstance(r, Exception):
                logger.warning(f"YouTube 辅助 API 调用失败: {r}")
            elif isinstance(r, tuple) and len(r) == 2:
                if isinstance(r[0], Comment) or r[0] is None:
                    pinned_comment, hot_comment = r
                else:
                    author, channel_handle = r
        if author is None:
            # 优先使用频道头像，不要用视频封面作为头像
            channel_avatar = getattr(video_info, "channel_avatar", None) or getattr(video_info, "uploader_avatar", None)
            author = self.create_author(
                video_info.author_name,
                avatar=channel_avatar,
                uid=video_info.channel_id,
                description=video_info.description or None,
                follower_count=getattr(video_info, "channel_follower_count", None),
            )

        contents = []
        if video_info.duration <= self.cfg.max_duration:
            video = self.downloader.ytdlp_download_video_relaxed(
                url,
                cookiefile=self.cookiejar.cookie_file,
                headers=self.headers,
                proxy=self.proxy,
                format="bv*+ba/b",
                node=True,
            )
            contents.append(
                self.create_video_content(
                    video,
                    video_info.thumbnail,
                    video_info.duration,
                )
            )
        else:
            contents.extend(self.create_image_contents([video_info.thumbnail]))

        # 统计数据
        stats = {}
        if video_info.view_count is not None:
            stats["views"] = video_info.view_count
        if video_info.like_count is not None:
            stats["likes"] = video_info.like_count
        if video_info.comment_count is not None:
            stats["comments"] = video_info.comment_count
        if video_info.repost_count is not None:
            stats["reposts"] = video_info.repost_count

        extra = {"handle": channel_handle} if channel_handle else {}
        if video_id:
            extra["video_id"] = video_id
            extra["post_id"] = video_id
        if author and author.uid:
            extra["uid"] = author.uid

        return self.result(
            title=video_info.title,
            text=video_info.description or None,
            author=author,
            contents=contents,
            timestamp=video_info.timestamp,
            url=url,
            stats=stats,
            pinned_comment=pinned_comment,
            hot_comment=hot_comment,
            extra=extra,
            page_type="video",
        )

    @handle(
        "ym",
        r"^ym(?P<url>https?://(?:www\.)?(youtu\.be/[A-Za-z\d_-]+|youtube\.com/(?:watch|shorts)(?:\?v=[A-Za-z\d_-]+|/[A-Za-z\d_-]+)))",
    )
    async def ym(self, searched: re.Match[str]):
        """获取油管的音频(需加ym前缀)"""
        url = searched.group("url")

        # yt-dlp 提取（可降级）
        video_info = None
        try:
            video_info = await self.downloader.ytdlp_extract_info(
                url,
                cookiefile=self.cookiejar.cookie_file,
                headers=self.headers,
                proxy=self.proxy,
            )
        except Exception as e:
            logger.warning(f"YouTube 音频解析失败(url={url}): {e}")
            return self.result(
                title="[解析失败]",
                url=url,
            )

        # 作者信息（可降级，使用视频缩略图作为头像回退）
        author = None
        try:
            author, _ = await self._fetch_author_info(video_info.channel_id)
        except Exception as e:
            logger.warning(f"获取油管作者信息失败(channel={video_info.channel_id}): {e}")
            channel_avatar = getattr(video_info, "channel_avatar", None) or getattr(video_info, "uploader_avatar", None)
            author = self.create_author(
                video_info.author_name,
                avatar=channel_avatar,
                uid=video_info.channel_id,
                description=video_info.description or None,
                follower_count=getattr(video_info, "channel_follower_count", None),
            )

        contents = []
        contents.extend(self.create_image_contents([video_info.thumbnail]))

        if video_info.duration <= self.cfg.max_duration:
            audio_task = self.downloader.ytdlp_download_audio(
                url,
                cookiefile=self.cookiejar.cookie_file,
                headers=self.headers,
                proxy=self.proxy,
            )
            contents.append(
                self.create_audio_content(audio_task, duration=video_info.duration)
            )

        # 统计数据
        stats = {}
        if video_info.view_count is not None:
            stats["views"] = video_info.view_count
        if video_info.like_count is not None:
            stats["likes"] = video_info.like_count
        if video_info.comment_count is not None:
            stats["comments"] = video_info.comment_count
        if video_info.repost_count is not None:
            stats["reposts"] = video_info.repost_count

        vid_m = re.search(r"(?:v=|youtu\.be/|shorts/|live/)([a-zA-Z0-9_-]{11})", url)
        extra_ym = {"video_id": vid_m.group(1), "post_id": vid_m.group(1)} if vid_m else {}

        return self.result(
            title=video_info.title,
            text=video_info.description or None,
            author=author,
            contents=contents,
            timestamp=video_info.timestamp,
            url=url,
            stats=stats,
            extra=extra_ym,
        )

    async def _fallback_embed(self, url: str) -> ParseResult:
        """从 YouTube oEmbed API + embed 页面提取基础信息"""
        # 从 URL 提取 video_id
        vid_match = re.search(r"(?:v=|youtu\.be/|shorts/|live/)([a-zA-Z0-9_-]{11})", url)
        if not vid_match:
            return self.result(title="[解析失败]", url=url)

        video_id = vid_match.group(1)

        # 优先尝试 oEmbed API（可靠的 JSON 接口）
        title = None
        author_name = None
        author_url = None
        try:
            oembed_url = f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json"
            async with self.session.get(oembed_url, headers=self.headers, proxy=self.proxy) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    title = data.get("title")
                    author_name = data.get("author_name")
                    author_url = data.get("author_url")
        except Exception as e:
            logger.debug(f"YouTube oEmbed 失败: {e}")

        # 从 embed 页面提取更多信息
        description = None
        upload_date = None
        view_count = None
        channel_avatar = None
        html = ""
        try:
            embed_url = f"https://www.youtube.com/embed/{video_id}"
            async with self.session.get(embed_url, headers=self.headers, proxy=self.proxy) as resp:
                if resp.status >= 400:
                    if not title:
                        return self.result(title="[解析失败]", url=url)
                else:
                    html = await resp.text()

            if html:
                if not title:
                    for pattern in [
                        r'"title"\s*:\s*"([^"]+)"',
                        r'"videoTitle"\s*:\s*"([^"]+)"',
                        r'<title>([^<]+)</title>',
                    ]:
                        m = re.search(pattern, html)
                        if m:
                            raw_title = m.group(1)
                            if raw_title and raw_title != "YouTube":
                                title = raw_title
                                break

                if not author_name:
                    author_match = re.search(r'"author"\s*:\s*"([^"]+)"', html)
                    if author_match:
                        author_name = author_match.group(1)

                # 提取 description
                desc_match = re.search(r'"shortDescription"\s*:\s*"((?:[^"\\]|\\.)*)"', html)
                if desc_match:
                    description = desc_match.group(1).replace("\\n", "\n").replace('\\"', '"')

                # 提取 upload date
                date_match = re.search(r'"publishDate"\s*:\s*"(\d{4}-\d{2}-\d{2})"', html)
                if date_match:
                    try:
                        from datetime import datetime
                        upload_date = int(datetime.strptime(date_match.group(1), "%Y-%m-%d").timestamp())
                    except (ValueError, TypeError):
                        pass

                # 提取 view count
                view_match = re.search(r'"viewCount"\s*:\s*"(\d+)"', html)
                if view_match:
                    view_count = int(view_match.group(1))

                # 提取 channel avatar
                avatar_match = re.search(r'"channelAvatar"\s*:\s*"([^"]+)"', html)
                if avatar_match:
                    channel_avatar = avatar_match.group(1)

        except Exception as e:
            logger.warning(f"YouTube embed 页面提取失败: {e}")

        if not title:
            title = "[解析失败]"

        # 缩略图（hqdefault 始终存在，maxresdefault 不一定有）
        thumbnail = f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg"

        # 构建作者（不用视频封面作为头像）
        avatar = channel_avatar
        author = None
        if author_name:
            # 从 author_url 提取 channel_id
            channel_id = None
            if author_url:
                ch_match = re.search(r"youtube\.com/channel/([a-zA-Z0-9_-]+)", author_url)
                if ch_match:
                    channel_id = ch_match.group(1)
            author = self.create_author(author_name, avatar=avatar, uid=channel_id or video_id, description=description)

        # 统计数据
        stats = {}
        if view_count:
            stats["views"] = view_count

        extra = {"video_id": video_id, "post_id": video_id} if video_id else {}
        pinned_comment, hot_comment = None, None

        # 尝试获取更完整的作者信息和评论
        channel_id = None
        if author_url:
            ch_match = re.search(r"youtube\.com/channel/([a-zA-Z0-9_-]+)", author_url)
            if ch_match:
                channel_id = ch_match.group(1)

        if channel_id or video_id:
            aux_tasks = []
            if channel_id:
                aux_tasks.append(self._fetch_author_info(channel_id))
            if video_id:
                aux_tasks.append(self._get_pinned_comment(video_id))
            if aux_tasks:
                aux_results = await asyncio.gather(*aux_tasks, return_exceptions=True)
                for r in aux_results:
                    if isinstance(r, Exception):
                        logger.debug(f"YouTube embed 辅助 API 调用失败: {r}")
                    elif isinstance(r, tuple) and len(r) == 2:
                        if isinstance(r[0], Comment) or r[0] is None:
                            pinned_comment, hot_comment = r
                        else:
                            full_author, channel_handle = r
                            if full_author:
                                author = full_author
                                if channel_handle:
                                    extra["handle"] = channel_handle

        return self.result(
            title=title,
            text=description,
            author=author,
            contents=self.create_image_contents([thumbnail]),
            url=url,
            timestamp=upload_date,
            stats=stats or None,
            extra=extra,
            pinned_comment=pinned_comment,
            hot_comment=hot_comment,
        )

    async def _fetch_author_info(self, channel_id: str):
        url = "https://www.youtube.com/youtubei/v1/browse?prettyPrint=false"
        payload = {
            "context": {
                "client": {
                    "hl": "zh-HK",
                    "gl": "US",
                    "deviceMake": "Apple",
                    "deviceModel": "",
                    "clientName": "WEB",
                    "clientVersion": "2.20251002.00.00",
                    "osName": "Macintosh",
                    "osVersion": "10_15_7",
                },
                "user": {"lockedSafetyMode": False},
                "request": {
                    "useSsl": True,
                    "internalExperimentFlags": [],
                    "consistencyTokenJars": [],
                },
            },
            "browseId": channel_id,
        }
        async with self.session.post(
            url,
            json=payload,
            headers=self.headers,
            proxy=self.proxy,
        ) as resp:
            if resp.status >= 400:
                raise ClientError(f"YouTube browse API {resp.status} {resp.reason}")
            raw_bytes = await resp.read()
            browse = msgspec.json.decode(raw_bytes, type=BrowseResponse)

        # 尝试提取订阅者数（YouTube browse API 响应中可能包含）
        subscriber_count = None
        try:
            raw_json = msgspec.json.decode(raw_bytes)
            # 尝试从 header 中提取 subscriberCountText
            header = raw_json.get("header", {})
            for renderer_key in ("c4TabbedHeaderRenderer", "pageHeaderRenderer"):
                renderer = header.get(renderer_key, {})
                sub_text = renderer.get("subscriberCountText", {})
                if isinstance(sub_text, dict):
                    text = sub_text.get("simpleText") or sub_text.get("accessibility", {}).get("accessibilityData", {}).get("label", "")
                else:
                    text = str(sub_text)
                if text:
                    # 解析 "1.2M subscribers" 或 "12.3万 订阅者" 等格式
                    m = re.search(r"([\d,.]+)\s*([KkMmBb万亿]?)", text.replace(",", ""))
                    if m:
                        num_str = m.group(1)
                        suffix = m.group(2)
                        num = float(num_str)
                        multiplier = {"K": 1000, "k": 1000, "M": 1000000, "m": 1000000, "B": 1000000000, "b": 1000000000, "万": 10000, "亿": 100000000}.get(suffix, 1)
                        subscriber_count = int(num * multiplier)
                        break
        except (ValueError, AttributeError, KeyError):
            pass

        # 尝试提取频道专属 handle (@xxx)
        channel_handle = None
        try:
            raw_json2 = msgspec.json.decode(raw_bytes)
            header = raw_json2.get("header", {})
            for renderer_key in ("c4TabbedHeaderRenderer", "pageHeaderRenderer"):
                renderer = header.get(renderer_key, {})
                # 直接的 handle 字段
                for handle_key in ("channelHandleText", "handle", "vanityChannelUrl"):
                    val = renderer.get(handle_key)
                    if isinstance(val, dict):
                        val = val.get("simpleText") or val.get("runs", [{}])[0].get("text")
                    if isinstance(val, str) and val.startswith("@"):
                        channel_handle = val
                        break
                    elif isinstance(val, str) and "/" in val and "@" in val:
                        # vanityChannelUrl 格式: /@channelname
                        m = re.search(r"@([a-zA-Z0-9_-]+)", val)
                        if m:
                            channel_handle = f"@{m.group(1)}"
                            break
                if channel_handle:
                    break
            # 回退: 从 metadata 提取
            if not channel_handle:
                metadata = raw_json2.get("metadata", {}).get("channelMetadataRenderer", {})
                vanity = metadata.get("vanityChannelUrl", "")
                if vanity and "@" in vanity:
                    m = re.search(r"@([a-zA-Z0-9_-]+)", vanity)
                    if m:
                        channel_handle = f"@{m.group(1)}"
        except Exception:
            pass

        author = self.create_author(browse.name, browse.avatar_url, description=browse.description, uid=channel_id, follower_count=subscriber_count)
        return author, channel_handle

    async def _get_pinned_comment(self, video_id: str) -> tuple[Comment | None, Comment | None]:
        """通过 InnerTube next API 获取置顶评论和热评（失败返回 (None, None)）"""
        try:
            url = "https://www.youtube.com/youtubei/v1/next?prettyPrint=false"
            payload = {
                "context": {
                    "client": {
                        "hl": "zh-HK",
                        "gl": "US",
                        "deviceMake": "Apple",
                        "deviceModel": "",
                        "clientName": "WEB",
                        "clientVersion": "2.20251002.00.00",
                        "osName": "Macintosh",
                        "osVersion": "10_15_7",
                    },
                    "user": {"lockedSafetyMode": False},
                },
                "videoId": video_id,
            }
            async with self.session.post(
                url, json=payload, headers=self.headers, proxy=self.proxy,
            ) as resp:
                if resp.status >= 400:
                    logger.debug(f"YouTube next API 返回 {resp.status}")
                    return None, None
                raw = await resp.json()

            # 从 items[0].itemSectionRenderer.contents 中找评论
            items = raw.get("contents", {})
            two_col = items.get("twoColumnWatchNextResults", {})
            results = two_col.get("results", {}).get("results", {}).get("contents", [])

            def _build_comment(comment_obj: dict) -> Comment | None:
                author_name = ""
                author_avatar = ""
                author_texts = comment_obj.get("authorText", {})
                if author_texts:
                    author_name = author_texts.get("simpleText", "")
                avatars = comment_obj.get("authorThumbnail", {}).get("thumbnails", [])
                if avatars:
                    author_avatar = avatars[-1].get("url", "")
                content_runs = comment_obj.get("contentText", {}).get("runs", [])
                content = "".join(r.get("text", "") for r in content_runs)
                if not content:
                    return None
                likes = 0
                like_text = comment_obj.get("voteCount", {}).get("simpleText", "")
                if like_text:
                    m = re.search(r"([\d,.]+)\s*([KkMmBb万亿]?)", like_text.replace(",", ""))
                    if m:
                        num = float(m.group(1))
                        suffix = m.group(2)
                        multiplier = {"K": 1000, "k": 1000, "M": 1000000, "m": 1000000, "B": 1000000000, "b": 1000000000, "万": 10000, "亿": 100000000}.get(suffix, 1)
                        likes = int(num * multiplier)
                is_pinned = bool(comment_obj.get("pinnedCommentBadge"))
                return Comment(
                    author_name=author_name,
                    content=content,
                    author_avatar=author_avatar,
                    likes=likes,
                    is_pinned=is_pinned,
                    is_hot=True,
                )

            pinned_comment = None
            hot_comment = None
            for section in results:
                isr = section.get("itemSectionRenderer")
                if not isr:
                    continue
                for item in isr.get("contents", []):
                    thread = item.get("commentThreadRenderer")
                    if not thread:
                        continue
                    comment_obj = thread.get("comment", {}).get("commentRenderer", {})
                    if not comment_obj:
                        continue
                    built = _build_comment(comment_obj)
                    if not built:
                        continue
                    if built.is_pinned and not pinned_comment:
                        pinned_comment = built
                        logger.debug(f"YouTube 置顶评论获取成功: {built.author_name}: {built.content[:50]}")
                    elif not hot_comment:
                        hot_comment = built
                        logger.debug(f"YouTube 热评获取成功: {built.author_name}: {built.content[:50]}")
                    if pinned_comment and hot_comment:
                        break
                if pinned_comment and hot_comment:
                    break

            # 如果没有找到标记为 pinned 的，第一条作为 pinned
            if not pinned_comment and hot_comment:
                pinned_comment = hot_comment
                hot_comment = None

            return pinned_comment, hot_comment
        except Exception as e:
            logger.debug(f"YouTube 置顶评论获取失败: {e}")
            return None, None


class Thumbnail(Struct):
    url: str


class AvatarInfo(Struct):
    thumbnails: list[Thumbnail]


class ChannelMetadataRenderer(Struct):
    title: str
    description: str
    avatar: AvatarInfo


class Metadata(Struct):
    channelMetadataRenderer: ChannelMetadataRenderer


class Avatar(Struct):
    thumbnails: list[Thumbnail]


class BrowseResponse(Struct):
    metadata: Metadata

    @property
    def name(self) -> str:
        return self.metadata.channelMetadataRenderer.title

    @property
    def avatar_url(self) -> str | None:
        thumbnails = self.metadata.channelMetadataRenderer.avatar.thumbnails
        return thumbnails[0].url if thumbnails else None

    @property
    def description(self) -> str:
        return self.metadata.channelMetadataRenderer.description

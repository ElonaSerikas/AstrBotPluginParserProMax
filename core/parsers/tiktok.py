import re
from typing import ClassVar

from ..config import PluginConfig
from ..cookie import CookieJar
from ..data import Platform, VideoContent
from ..download import Downloader
from .base import BaseParser, handle


class TikTokParser(BaseParser):
    # 平台信息
    platform: ClassVar[Platform] = Platform(name="tiktok", display_name="TikTok")

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.headers.update(
            {"Origin": "https://www.tiktok.com", "Referer": "https://www.tiktok.com/"}
        )
        self.mycfg = config.parser.tiktok
        self.cookiejar = CookieJar(config, self.mycfg, "tiktok.com")

    @handle("tiktok.com", r"(www|vt|vm)\.tiktok\.com/[A-Za-z0-9._?%&+\-=/#@]*")
    async def _parse(self, searched: re.Match[str]):
        # 从匹配对象中获取原始URL
        url, prefix = searched.group(0), searched.group(1)
        original_url = f"https://{url}"

        if prefix in ("vt", "vm"):
            url = await self.get_redirect_url(url)

        # 获取视频信息
        video_info = await self.downloader.ytdlp_extract_info(
            url, headers=self.headers, proxy=self.proxy
        )

        # 下载封面和视频
        cover = self.downloader.download_img(
            url=video_info.thumbnail, headers=self.headers, proxy=self.proxy
        )
        video = self.downloader.ytdlp_download_video(
            url,
            cookiefile=self.cookiejar.cookie_file,
            headers=self.headers,
            proxy=self.proxy,
            format="best",
        )

        # 统计数据
        stats = {}
        if video_info.view_count is not None:
            stats["views"] = str(video_info.view_count)
        if video_info.like_count is not None:
            stats["likes"] = str(video_info.like_count)
        if video_info.comment_count is not None:
            stats["comments"] = str(video_info.comment_count)
        if video_info.repost_count is not None:
            stats["reposts"] = str(video_info.repost_count)

        return self.result(
            title=video_info.title,
            url=original_url,
            author=self.create_author(name=video_info.channel, uid=video_info.channel_id, description=video_info.description or None),
            contents=[VideoContent(video, cover, duration=video_info.duration)],
            timestamp=video_info.timestamp,
            stats=stats,
            extra={"uid": str(video_info.channel_id or ""), "post_id": video_info.id or "", "handle": f"@{video_info.channel}"} if video_info.channel else {"uid": str(video_info.channel_id or ""), "post_id": video_info.id or ""},
        )

import re as re_module
from re import Match
from typing import ClassVar

from aiohttp import ClientError

from ..config import PluginConfig
from ..data import Platform
from ..download import Downloader
from .base import BaseParser, handle


class KuWoParser(BaseParser):
    """酷我音乐解析器"""

    platform: ClassVar[Platform] = Platform(name="kuwo", display_name="酷我音乐")

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.headers.update({"Referer": "https://www.kuwo.cn/"})
        self.mycfg = config.parser.kuwo
        self._csrf_token: str | None = None

    # ------------------------------------------------------------------
    # CSRF helper (kw_token cookie + csrf header)
    # ------------------------------------------------------------------

    async def _ensure_csrf(self):
        """访问酷我首页获取 kw_token，并将其注入请求头"""
        if self._csrf_token:
            return
        try:
            async with self.session.get(
                "https://www.kuwo.cn/",
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    )
                },
            ) as resp:
                if "kw_token" in resp.cookies:
                    self._csrf_token = resp.cookies["kw_token"].value
                else:
                    html = await resp.text()
                    m = re_module.search(
                        r'kw_token\s*=\s*["\']([^"\']+)["\']', html
                    )
                    if m:
                        self._csrf_token = m.group(1)

                if self._csrf_token:
                    self.headers["Cookie"] = f"kw_token={self._csrf_token}"
                    self.headers["csrf"] = self._csrf_token
        except Exception:
            pass  # 没有 csrf 仍然尝试请求

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    @handle("kuwo.cn/yinyue", r"kuwo\.cn/yinyue/(\d+)")
    async def _parse_song(self, searched: Match[str]):
        """单曲解析"""
        rid = searched.group(1)
        url = f"https://www.kuwo.cn/yinyue/{rid}"
        return await self._fetch_song_data(rid, url)

    @handle("kuwo.cn/playlist_detail", r"kuwo\.cn/playlist_detail/(\d+)")
    async def _parse_playlist(self, searched: Match[str]):
        """歌单解析（仅返回基础信息）"""
        pid = searched.group(1)
        url = f"https://www.kuwo.cn/playlist_detail/{pid}"
        return self.result(
            title="酷我歌单",
            text=f"歌单ID: {pid}",
            url=url,
        )

    # ------------------------------------------------------------------
    # Core data fetch
    # ------------------------------------------------------------------

    async def _fetch_song_data(self, rid: str, source_url: str):
        """获取歌曲完整数据"""
        await self._ensure_csrf()

        # 1. 歌曲元信息（XML）
        info_url = (
            f"http://player.kuwo.cn/webmusic/st/getNewMuiseByRid"
            f"?rid=MUSIC_{rid}"
        )
        async with self.session.get(info_url, headers=self.headers) as resp:
            if resp.status >= 400:
                raise ClientError(
                    f"[KuWo] 获取歌曲信息失败 HTTP {resp.status}"
                )
            xml_text = await resp.text()

        def _tag(tag: str) -> str:
            m = re_module.search(
                f"<{tag}>(.*?)</{tag}>", xml_text, re_module.DOTALL
            )
            return m.group(1).strip() if m else ""

        title = _tag("name") or _tag("songName") or "未知歌曲"
        artist = _tag("artist") or "未知歌手"
        album = _tag("album")
        cover_url = _tag("pic")
        duration_str = _tag("duration")
        duration = (
            int(duration_str)
            if duration_str and duration_str.isdigit()
            else 0
        )
        artist_pic = _tag("artist_pic")
        avatar_url = artist_pic or cover_url

        # 2. 播放地址
        play_url = await self._get_play_url(rid)
        if not play_url:
            raise ValueError(
                "[KuWo] 无法获取播放地址，歌曲可能受版权保护"
            )

        author = self.create_author(artist, avatar_url)
        audio = self.create_audio_content(play_url, duration=duration)
        contents = [audio]

        # 3. 歌词
        lyrics_text = await self._get_lyrics(rid)

        # 4. 组装
        text_parts: list[str] = []
        if album:
            text_parts.append(f"专辑：{album}")

        if lyrics_text:
            text_parts.append("")
            text_parts.append("--- 歌词 ---")
            text_parts.extend(lyrics_text.strip().split("\n")[:30])

        return self.result(
            title=title,
            text="\n".join(text_parts) if text_parts else None,
            author=author,
            contents=contents,
            url=source_url,
        )

    # ------------------------------------------------------------------
    # Sub-requests
    # ------------------------------------------------------------------

    async def _get_play_url(self, rid: str) -> str:
        """从 antiserver 获取 mp3 直链"""
        play_api = (
            f"https://antiserver.kuwo.cn/anti.s"
            f"?type=convert_url3&rid={rid}&format=mp3"
        )
        try:
            async with self.session.get(
                play_api, headers=self.headers, allow_redirects=True
            ) as resp:
                if resp.status >= 400:
                    return ""
                text = await resp.text()
                text = text.strip().strip('"')
                if text.startswith(("http://", "https://")):
                    return text
                return ""
        except Exception:
            return ""

    async def _get_lyrics(self, rid: str) -> str:
        """获取 LRC 格式歌词"""
        lyrics_url = (
            f"https://www.kuwo.cn/newh5/singles/songinfoandlrc"
            f"?musicId={rid}"
        )
        try:
            async with self.session.get(
                lyrics_url, headers=self.headers
            ) as resp:
                if resp.status >= 400:
                    return ""
                data = await resp.json()
        except Exception:
            return ""

        if data.get("code") != 200:
            return ""

        lrc_list = data.get("data", {}).get("lrclist", [])
        if not lrc_list:
            return ""

        lines: list[str] = []
        for item in lrc_list:
            time = item.get("time", "")
            lyric = item.get("lineLyric", "")
            lines.append(f"[{time}]{lyric}")

        return "\n".join(lines)

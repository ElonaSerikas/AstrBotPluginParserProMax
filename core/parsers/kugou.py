import hashlib
import random
from re import Match
from typing import ClassVar
from urllib.parse import parse_qs, urlparse

from aiohttp import ClientError

from ..config import PluginConfig
from ..data import Comment, LyricLine, MusicInfo, Platform, TextContent
from ..download import Downloader
from .base import BaseParser, handle


class KuGouParser(BaseParser):
    """酷狗音乐解析器"""

    platform: ClassVar[Platform] = Platform(name="kugou", display_name="酷狗音乐")

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.headers.update({"Referer": "https://www.kugou.com/"})
        self.mycfg = config.parser.kugou

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _generate_mid(self) -> str:
        """生成随机设备 mid"""
        raw = str(random.random())
        return hashlib.md5(raw.encode()).hexdigest()[:16]

    @staticmethod
    def _extract_hash(url: str) -> str | None:
        """从 URL 片段或查询字符串中提取 hash"""
        parsed = urlparse(url)
        for source in (parsed.fragment, parsed.query):
            params = parse_qs(source)
            if h := params.get("hash", [None])[0]:
                return h
        return None

    async def _get_artist_info(self, author_id: str) -> dict:
        """获取歌手详情（粉丝数、签名等）"""
        try:
            url = f"http://www.kugou.com/yy/singer/info/{author_id}"
            async with self.session.get(url, headers=self.headers) as resp:
                if resp.status >= 400:
                    return {}
                data = await resp.json()
            info = data.get("data", {}) or data.get("info", {})
            return {
                "follower_count": info.get("fanscount", 0) or info.get("fans", 0),
                "description": info.get("intro", "") or info.get("description", ""),
                "avatar": info.get("imgurl", "") or info.get("avatar", ""),
            }
        except Exception:
            return {}

    async def _get_comments(self, hash_val: str) -> tuple[Comment | None, Comment | None, int]:
        """获取置顶评论、热评和评论总数（失败返回 (None, None, 0)）"""
        try:
            url = f"http://www.kugou.com/yy/index.php?r=comments/getcomments&hash={hash_val}&p=1&pagesize=20"
            async with self.session.get(url, headers=self.headers) as resp:
                if resp.status >= 400:
                    return None, None, 0
                data = await resp.json()

            comments = data.get("data", {}).get("list", []) or data.get("list", [])
            comment_count = data.get("data", {}).get("count", 0) or data.get("count", 0) or len(comments)
            if not comments:
                return None, None, 0

            def _build_comment(c: dict) -> Comment | None:
                content = c.get("content", "") or c.get("msg", "")
                if not content:
                    return None
                user = c.get("user", {}) or c
                return Comment(
                    author_name=user.get("nickname", "") or user.get("username", ""),
                    content=content,
                    author_avatar=user.get("avatar", "") or user.get("headpic", ""),
                    likes=c.get("likecount", 0) or c.get("like", 0),
                    is_hot=True,
                )

            pinned_comment = None
            hot_comment = None
            for c in comments[:5]:
                built = _build_comment(c)
                if not built:
                    continue
                if not pinned_comment:
                    pinned_comment = built
                elif not hot_comment:
                    hot_comment = built
                    break

            return pinned_comment, hot_comment, comment_count
        except Exception:
            return None, None

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    @handle("t1.kugou.com", r"t1\.kugou\.com/(?P<code>\w+)")
    async def _parse_short(self, searched: Match[str]):
        """短链接 t1.kugou.com/xxx -> 重定向到真实页面"""
        short_url = f"https://t1.kugou.com/{searched.group('code')}"
        return await self.parse_with_redirect(short_url)

    @handle("kugou.com/mixsong", r"kugou\.com/mixsong/(?P<encode_id>[^/\?#]+)\/?(?:\?.*)?$")
    async def _parse_song(self, searched: Match[str]):
        """单曲解析（hash 从 URL 片段或查询参数中提取）"""
        full_url = searched.string
        hash_val = self._extract_hash(full_url)
        if not hash_val:
            raise ValueError("[KuGou] 无法从链接中提取 hash 参数")
        return await self._fetch_song_data(hash_val, full_url)

    @handle(
        "kugou.com/yy/special",
        r"kugou\.com/yy/special/single/(?P<special_id>\d+)\.html\/?(?:\?.*)?$",
    )
    async def _parse_playlist(self, searched: Match[str]):
        """歌单解析"""
        special_id = searched.group("special_id")
        url = f"https://kugou.com/yy/special/single/{special_id}.html"
        api_url = f"http://mobilecdn.kugou.com/api/v3/special/song?specialid={special_id}&page=1&pagesize=50"

        try:
            async with self.session.get(api_url, headers=self.headers) as resp:
                if resp.status >= 400:
                    return self.result(title="酷狗歌单", text=f"歌单ID: {special_id}", url=url)
                data = await resp.json()

            info = data.get("data", {})
            songs = info.get("info", [])
            total = info.get("total", len(songs))

            lines = [f"🎵 酷狗歌单 ({total}首)", ""]
            for i, song in enumerate(songs[:50], 1):
                name = song.get("songname", "未知歌曲")
                singer = song.get("singername", "未知歌手")
                lines.append(f"{i}. {singer} - {name}")

            if total > 50:
                lines.append(f"\n... 共 {total} 首，仅显示前 50 首")

            return self.result(
                title=f"酷狗歌单 #{special_id}",
                text="\n".join(lines),
                url=url,
                extra={"playlist_id": special_id},
            )
        except Exception:
            return self.result(title="酷狗歌单", text=f"歌单ID: {special_id}", url=url)

    @handle(
        "kugou.com/yy/album",
        r"kugou\.com/yy/album/single/(?P<album_id>\d+)\.html\/?(?:\?.*)?$",
    )
    async def _parse_album(self, searched: Match[str]):
        """专辑解析"""
        album_id = searched.group("album_id")
        url = f"https://kugou.com/yy/album/single/{album_id}.html"
        api_url = f"http://mobilecdn.kugou.com/api/v3/album/song?albumid={album_id}&page=1&pagesize=50"

        try:
            async with self.session.get(api_url, headers=self.headers) as resp:
                if resp.status >= 400:
                    return self.result(title="酷狗专辑", text=f"专辑ID: {album_id}", url=url)
                data = await resp.json()

            info = data.get("data", {})
            songs = info.get("info", [])
            total = info.get("total", len(songs))

            lines = [f"💿 酷狗专辑 ({total}首)", ""]
            for i, song in enumerate(songs[:50], 1):
                name = song.get("songname", "未知歌曲")
                singer = song.get("singername", "未知歌手")
                lines.append(f"{i}. {singer} - {name}")

            if total > 50:
                lines.append(f"\n... 共 {total} 首，仅显示前 50 首")

            return self.result(
                title=f"酷狗专辑 #{album_id}",
                text="\n".join(lines),
                url=url,
                extra={"album_id": album_id},
            )
        except Exception:
            return self.result(title="酷狗专辑", text=f"专辑ID: {album_id}", url=url)

    # ------------------------------------------------------------------
    # Core data fetch
    # ------------------------------------------------------------------

    async def _fetch_song_data(self, hash_val: str, source_url: str):
        """通过 hash 调用酷狗 API 获取歌曲完整数据"""
        mid = self._generate_mid()
        api_url = (
            f"http://www.kugou.com/yy/index.php"
            f"?r=play/getdata&hash={hash_val}&mid={mid}"
        )

        async with self.session.get(api_url, headers=self.headers) as resp:
            if resp.status >= 400:
                raise ClientError(
                    f"[KuGou] 获取歌曲数据失败 HTTP {resp.status}"
                )
            data = await resp.json()

        if data.get("status") != 1:
            raise ValueError(
                "[KuGou] 获取歌曲数据失败，请检查 hash 是否正确"
            )

        song_data = data.get("data", {})
        if not song_data:
            raise ValueError("[KuGou] 歌曲数据为空")

        play_url = song_data.get("play_url", "")
        if not play_url:
            raise ValueError(
                "[KuGou] 无法获取播放地址，歌曲可能受版权保护"
            )

        title = song_data.get("song_name", "") or "未知歌曲"
        author_name = song_data.get("author_name", "") or "未知歌手"
        author_id = song_data.get("author_id", "") or ""
        album_name = song_data.get("album_name", "") or ""
        album_id = song_data.get("album_id", "") or ""
        cover_url = song_data.get("img", "") or ""
        duration_ms = song_data.get("time_length", 0)
        duration = (
            duration_ms / 1000
            if isinstance(duration_ms, (int, float))
            else 0
        )
        lyrics_text = song_data.get("lyrics", "") or ""
        bitrate = song_data.get("bitrate", 0) or 0
        raw_timestamp = song_data.get("publish_time") or song_data.get("release_date")
        if isinstance(raw_timestamp, (int, float)):
            timestamp = int(raw_timestamp)
        else:
            timestamp = None

        # 并发获取：歌手详情、评论
        import asyncio
        artist_info, (pinned_comment, hot_comment, comment_count) = await asyncio.gather(
            self._get_artist_info(author_id) if author_id else asyncio.sleep(0, result={}),
            self._get_comments(hash_val),
        )

        # 作者 & 音频
        follower_count = artist_info.get("follower_count") or None
        description = artist_info.get("description") or None
        author = self.create_author(
            author_name, cover_url, uid=str(author_id) if author_id else None,
            description=description, follower_count=follower_count,
        )
        audio = self.create_audio_content(play_url, duration=duration)
        contents = [audio]

        # 封面图作为独立图片内容
        if cover_url:
            contents.append(self.create_graphics_content(cover_url))

        # 展示文字
        text_parts: list[str] = []
        if album_name:
            text_parts.append(f"专辑：{album_name}")
        if bitrate:
            text_parts.append(f"音质：{bitrate}kbps")

        # 解析歌词为 LyricLine
        lyrics_lines = []
        if lyrics_text.strip():
            import re as _re
            lrc_lines = [
                line.strip()
                for line in lyrics_text.strip().split("\n")
                if line.strip()
            ]
            text_parts.append("")
            text_parts.append("--- 歌词 ---")
            text_parts.extend(lrc_lines[:30])
            for line in lrc_lines:
                m = _re.match(r"\[(\d+:\d+[\.:]\d+)\](.*)", line)
                if m:
                    lyrics_lines.append(LyricLine(time=m.group(1), text=m.group(2).strip()))

        # 统计数据
        stats = {}
        play_count = song_data.get("play_count", 0) or song_data.get("playcount", 0)
        if play_count:
            stats["views"] = play_count
        if comment_count:
            stats["comments"] = comment_count

        return self.result(
            title=title,
            text="\n".join(text_parts) if text_parts else None,
            author=author,
            contents=contents,
            timestamp=timestamp,
            url=source_url,
            music_info=MusicInfo(
                title=title,
                artist=author_name,
                album=album_name,
                cover_url=cover_url,
                duration=int(duration),
                lyrics=lyrics_lines,
            ),
            stats=stats or None,
            extra={
                "uid": str(author_id or ""),
                "post_id": hash_val,
                "hash": hash_val,
                "album_id": str(album_id),
                "artist_id": str(author_id),
                "handle": f"歌手:{author_name}" if author_name else "",
            },
            pinned_comment=pinned_comment,
            hot_comment=hot_comment,
            page_type="song",
        )

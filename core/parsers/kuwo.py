import re as re_module
from re import Match
from typing import ClassVar

from aiohttp import ClientError

from ..config import PluginConfig
from ..data import Comment, LyricLine, MusicInfo, Platform, TextContent
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

    async def _get_artist_info(self, artist_id: str) -> dict:
        """获取歌手详情（粉丝数、签名等）"""
        try:
            await self._ensure_csrf()
            url = f"https://www.kuwo.cn/api/www/artist/artistInfo?artistid={artist_id}"
            async with self.session.get(url, headers=self.headers) as resp:
                if resp.status >= 400:
                    return {}
                data = await resp.json()
            info = data.get("data", {})

            # 尝试获取粉丝数（可能在不同字段）
            follower_count = (
                info.get("fansCount", 0)
                or info.get("fans_count", 0)
                or info.get("artistFans", 0)
                or info.get("musicNum", 0)  # 回退到歌曲数
            )

            return {
                "follower_count": follower_count,
                "description": info.get("info", "") or info.get("desc", "") or info.get("briefDesc", ""),
                "avatar": info.get("pic", "") or info.get("avatar", "") or info.get("img", ""),
            }
        except Exception:
            return {}

    async def _get_comments(self, rid: str) -> tuple[Comment | None, Comment | None, int]:
        """获取置顶评论、热评和评论总数（失败返回 (None, None, 0)）"""
        try:
            await self._ensure_csrf()
            url = f"https://www.kuwo.cn/api/www/comment/commentList?rid={rid}&type=0&pn=1&rn=20"
            async with self.session.get(url, headers=self.headers) as resp:
                if resp.status >= 400:
                    return None, None, 0
                data = await resp.json()

            comments = data.get("data", {}).get("list", []) or data.get("data", {}).get("comments", [])
            comment_count = data.get("data", {}).get("total", 0) or data.get("data", {}).get("count", 0) or len(comments)
            if not comments:
                return None, None, 0

            def _build_comment(c: dict) -> Comment | None:
                content = c.get("content", "") or c.get("msg", "")
                if not content:
                    return None
                user = c.get("user", {}) or c
                return Comment(
                    author_name=user.get("name", "") or user.get("nickname", ""),
                    content=content,
                    author_avatar=user.get("avatar", "") or user.get("headpic", ""),
                    likes=c.get("likeNum", 0) or c.get("like", 0),
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

    @handle("kuwo.cn/yinyue", r"kuwo\.cn/yinyue/(\d+)\/?(?:\?.*)?$")
    async def _parse_song(self, searched: Match[str]):
        """单曲解析"""
        rid = searched.group(1)
        url = f"https://www.kuwo.cn/yinyue/{rid}"
        return await self._fetch_song_data(rid, url)

    @handle("kuwo.cn/playlist_detail", r"kuwo\.cn/playlist_detail/(\d+)\/?(?:\?.*)?$")
    async def _parse_playlist(self, searched: Match[str]):
        """歌单解析"""
        pid = searched.group(1)
        url = f"https://www.kuwo.cn/playlist_detail/{pid}"
        await self._ensure_csrf()

        api_url = f"https://www.kuwo.cn/api/www/playlist/playListInfo?pid={pid}&pn=1&rn=50"
        try:
            async with self.session.get(api_url, headers=self.headers) as resp:
                if resp.status >= 400:
                    return self.result(title="酷我歌单", text=f"歌单ID: {pid}", url=url)
                data = await resp.json()

            info = data.get("data", {})
            name = info.get("name", "酷我歌单")
            songs = info.get("musicList", [])
            total = info.get("total", len(songs))
            cover_url = info.get("img700", "") or info.get("img", "")
            creator = info.get("uname", "")
            description = info.get("info", "")

            lines = [f"🎵 {name} ({total}首)"]
            if creator:
                lines.append(f"创建者: {creator}")
            if description:
                lines.append(f"简介: {description[:200]}")
            lines.append("")

            for i, song in enumerate(songs[:50], 1):
                name_ = song.get("name", "未知歌曲")
                artist = song.get("artist", "未知歌手")
                lines.append(f"{i}. {artist} - {name_}")

            if total > 50:
                lines.append(f"\n... 共 {total} 首，仅显示前 50 首")

            contents = [TextContent("\n".join(lines))]
            if cover_url:
                contents.append(self.create_graphics_content(cover_url))

            return self.result(
                title=name,
                text=f"歌单共 {total} 首",
                contents=contents,
                url=url,
                extra={"playlist_id": pid},
            )
        except Exception:
            return self.result(title="酷我歌单", text=f"歌单ID: {pid}", url=url)

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
        artist_id = _tag("artistid")
        album = _tag("album")
        album_id = _tag("albumid")
        cover_url = _tag("pic")
        duration_str = _tag("duration")
        duration = (
            int(duration_str)
            if duration_str and duration_str.isdigit()
            else 0
        )
        artist_pic = _tag("artist_pic")
        avatar_url = artist_pic or cover_url

        # 尝试提取发布时间
        release_date_str = _tag("releaseDate") or _tag("publishTime")
        timestamp = None
        if release_date_str and release_date_str.isdigit():
            ts = int(release_date_str)
            # kuwo 可能返回毫秒时间戳
            timestamp = ts // 1000 if ts > 10**12 else ts

        # 2. 播放地址
        play_url = await self._get_play_url(rid)
        if not play_url:
            raise ValueError(
                "[KuWo] 无法获取播放地址，歌曲可能受版权保护"
            )

        author = self.create_author(artist, avatar_url, uid=artist_id or None)
        audio = self.create_audio_content(play_url, duration=duration)
        contents = [audio]

        # 封面图作为独立图片内容
        if cover_url:
            contents.append(self.create_graphics_content(cover_url))

        # 3. 并发获取：歌词、歌手详情、评论
        import asyncio
        lyrics_text, artist_info, (pinned_comment, hot_comment, comment_count) = await asyncio.gather(
            self._get_lyrics(rid),
            self._get_artist_info(artist_id) if artist_id else asyncio.sleep(0, result={}),
            self._get_comments(rid),
        )

        # 解析歌词为 LyricLine
        lyrics_lines = []
        if lyrics_text:
            for line in lyrics_text.strip().split("\n"):
                m = re_module.match(r"\[(\d+:\d+[\.:]\d+)\](.*)", line)
                if m:
                    lyrics_lines.append(LyricLine(time=m.group(1), text=m.group(2).strip()))

        # 更新作者信息（添加粉丝数、签名）
        follower_count = artist_info.get("follower_count") or None
        description = artist_info.get("description") or None
        if follower_count or description:
            author = self.create_author(
                artist, avatar_url, uid=artist_id or None,
                description=description, follower_count=follower_count,
            )

        # 4. 组装
        text_parts: list[str] = []
        if album:
            text_parts.append(f"专辑：{album}")

        if lyrics_text:
            text_parts.append("")
            text_parts.append("--- 歌词 ---")
            text_parts.extend(lyrics_text.strip().split("\n")[:30])

        # 统计数据
        stats = {}
        play_count_str = _tag("playcount") or _tag("playCount")
        if play_count_str and play_count_str.isdigit():
            stats["views"] = int(play_count_str)
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
                artist=artist,
                album=album,
                cover_url=cover_url,
                duration=duration,
                lyrics=lyrics_lines,
            ),
            stats=stats or None,
            extra={
                "uid": artist_id or "",
                "post_id": rid,
                "song_id": rid,
                "album_id": album_id or "",
                "artist_id": artist_id or "",
                "handle": f"歌手:{artist}" if artist else "",
            },
            pinned_comment=pinned_comment,
            hot_comment=hot_comment,
            page_type="song",
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

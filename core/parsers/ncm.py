from re import Match
from typing import ClassVar

from aiohttp import ClientError

from ..config import PluginConfig
from ..cookie import CookieJar
from ..data import Comment, ImageContent, LyricLine, MusicInfo, Platform, TextContent
from ..download import Downloader
from .base import BaseParser, handle


class NCMParser(BaseParser):
    """网易云音乐解析器"""

    platform: ClassVar[Platform] = Platform(
        name="ncm", display_name="网易云"
    )

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.headers.update({"Referer": "https://music.163.com"})
        self.mycfg = config.parser.ncm
        self.cookiejar = CookieJar(config, self.mycfg, domain="music.163.com")
        if self.cookiejar.cookies_str:
            self.headers["cookie"] = self.cookiejar.cookies_str


    @handle("163cn.tv", r"163cn\.tv/(?P<short_key>\w+)")
    async def _parse_short(self, searched: Match[str]):
        short_url = f"https://163cn.tv/{searched.group('short_key')}"
        # 让框架跟随 302 后再走通用解析
        return await self.parse_with_redirect(short_url)


    @handle("y.music.163.com", r"y\.music\.163\.com/m/song\?.*id=(?P<song_id>\d+)(?:&.*)?$")
    @handle("music.163.com/song", r"music\.163\.com/song\?.*id=(?P<song_id>\d+)(?:&.*)?$")
    @handle("music.163.com/#/song", r"music\.163\.com/#/song\?.*id=(?P<song_id>\d+)(?:&.*)?$")
    async def _parse_song(self, searched: Match[str]):
        song_id = searched.group("song_id")
        detail_url = (
            f"https://music.163.com/api/song/detail/?id={song_id}&ids=[{song_id}]"
        )
        play_url = f"https://music.163.com/api/song/enhance/player/url?ids=[{song_id}]&br=320000"

        # 1. 取歌曲元数据
        async with self.session.get(detail_url, headers=self.headers) as resp:
            if resp.status >= 400:
                raise ClientError(f"[NCM] 获取歌曲信息失败 HTTP {resp.status}")
            detail_json = await resp.json(content_type=None)

        song = detail_json.get("songs", [{}])[0]
        if not song:
            raise ValueError("[NCM] 未找到该歌曲")

        title = song.get("name", "")
        sub_title = song.get("alias", [""])[0]  # 别名
        album_data = song.get("album", {})
        album_name = album_data.get("name", "")
        album_id = str(album_data.get("id", ""))
        cover_url = album_data.get("picUrl", "") + "?param=640y640"
        duration_ms = song.get("duration", 0)

        # 作者信息
        ar_list = song.get("artists", [])
        author_name = " / ".join(ar.get("name", "") for ar in ar_list)
        author_avatar = ar_list[0].get("img1v1Url", "") if ar_list else ""
        author_uid = (
            str(ar_list[0].get("id")) if ar_list and ar_list[0].get("id") else None
        )

        # 尝试提取发布时间
        publish_time = album_data.get("publishTime")
        if publish_time and isinstance(publish_time, (int, float)):
            timestamp = int(publish_time / 1000)
        else:
            timestamp = None

        # 2. 并发获取：播放地址、歌词、歌手详情、评论
        import asyncio
        play_task = self._fetch_play_url(song_id, play_url)
        lyrics_task = self._fetch_lyrics(song_id)
        artist_task = self._get_artist_info(author_uid) if author_uid else asyncio.sleep(0, result={})
        comments_task = self._get_comments(song_id)
        audio_url, lyrics_text, artist_info, (pinned_comment, hot_comment, comment_count) = await asyncio.gather(
            play_task, lyrics_task, artist_task, comments_task
        )

        # 3. 组装结果
        follower_count = artist_info.get("follower_count") or None
        description = artist_info.get("description") or None
        author = self.create_author(
            author_name, author_avatar, uid=author_uid,
            description=description, follower_count=follower_count,
        )
        audio = self.create_video_content(
            audio_url, cover_url, duration=duration_ms // 1000
        )
        contents = [audio]

        # 封面图作为独立图片内容
        if cover_url:
            contents.append(self.create_graphics_content(cover_url))

        # 解析歌词为 LyricLine
        lyrics_lines = []
        if lyrics_text:
            import re as _re
            for line in lyrics_text.strip().split("\n"):
                m = _re.match(r"\[(\d+:\d+[\.:]\d+)\](.*)", line)
                if m:
                    lyrics_lines.append(LyricLine(time=m.group(1), text=m.group(2).strip()))

        # 构建展示文本
        text_parts = [f"专辑：{album_name}"]
        if lyrics_text:
            text_parts.append("")
            text_parts.append("--- 歌词 ---")
            text_parts.extend(lyrics_text.strip().split("\n")[:30])

        # 统计数据
        popularity = song.get("popularity", 0)
        stats = {}
        if popularity:
            stats["likes"] = popularity  # 热度映射为「赞」（标准 stats key）
        if comment_count:
            stats["comments"] = comment_count

        # 4. 返回
        return self.result(
            title=f"{title}{'（' + sub_title + '）' if sub_title else ''}",
            text="\n".join(text_parts),
            author=author,
            contents=contents,
            timestamp=timestamp,
            url=f"https://music.163.com/#/song?id={song_id}",
            music_info=MusicInfo(
                title=title,
                artist=author_name,
                album=album_name,
                cover_url=cover_url,
                duration=duration_ms // 1000,
                lyrics=lyrics_lines,
            ),
            stats=stats or None,
            extra={
                "uid": author_uid or "",
                "post_id": song_id,
                "song_id": song_id,
                "album_id": album_id,
                "artist_id": author_uid or "",
                "handle": f"歌手:{author_name}" if author_name else "",
            },
            pinned_comment=pinned_comment,
            hot_comment=hot_comment,
            page_type="song",
        )

    async def _get_artist_info(self, artist_id: str) -> dict:
        """获取歌手详情（粉丝数、签名等）"""
        try:
            # 尝试多个 API 端点获取歌手信息
            artist = {}
            for api_url in [
                f"https://music.163.com/api/artist/{artist_id}",
                f"https://music.163.com/api/artist/get?artistId={artist_id}",
            ]:
                async with self.session.get(api_url, headers=self.headers) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        artist = data.get("artist", {}) or data.get("data", {}).get("artist", {})
                        if artist:
                            break

            if not artist:
                return {}

            # 尝试获取粉丝数（可能在不同字段）
            follower_count = (
                artist.get("fansCount", 0)
                or artist.get("fans_count", 0)
                or artist.get("followCount", 0)
                or artist.get("musicSize", 0)  # 回退到歌曲数
            )

            return {
                "follower_count": follower_count,
                "description": artist.get("briefDesc", "") or artist.get("desc", ""),
                "avatar": artist.get("picUrl", "") or artist.get("img1v1Url", ""),
            }
        except Exception:
            return {}

    async def _get_comments(self, song_id: str) -> tuple[Comment | None, Comment | None, int]:
        """获取置顶评论、热评和评论总数（失败返回 (None, None, 0)）"""
        try:
            url = f"https://music.163.com/api/v1/resource/comments/R_SO_4_{song_id}"
            params = {"limit": 20, "offset": 0}
            async with self.session.get(url, params=params, headers=self.headers) as resp:
                if resp.status >= 400:
                    return None, None, 0
                data = await resp.json()

            comment_count = data.get("total", 0)

            def _build_comment(c: dict) -> Comment | None:
                user = c.get("user", {})
                content = c.get("content", "")
                if not content:
                    return None
                return Comment(
                    author_name=user.get("nickname", ""),
                    content=content,
                    author_avatar=user.get("avatarUrl", ""),
                    likes=c.get("likedCount", 0),
                    is_hot=True,
                )

            pinned_comment = None
            hot_comment = None

            # 先检查置顶评论
            for c in data.get("topComments", []):
                built = _build_comment(c)
                if built:
                    pinned_comment = built
                    break

            # 再检查热评
            for c in data.get("hotComments", []):
                built = _build_comment(c)
                if built:
                    if not pinned_comment:
                        pinned_comment = built
                    elif not hot_comment:
                        hot_comment = built
                        break

            return pinned_comment, hot_comment, comment_count
        except Exception:
            return None, None, 0

    async def _fetch_play_url(self, song_id: str, play_url: str) -> str:
        """获取播放地址"""
        try:
            async with self.session.get(play_url, headers=self.headers) as resp:
                if resp.status >= 400:
                    return ""
                play_json = await resp.json()
            play_info = play_json.get("data", [{}])[0]
            return play_info.get("url", "")
        except Exception:
            return ""

    async def _fetch_lyrics(self, song_id: str) -> str:
        """获取歌词（LRC 格式）"""
        lyrics_url = f"https://music.163.com/api/song/lyric?id={song_id}&lv=1"
        try:
            async with self.session.get(lyrics_url, headers=self.headers) as resp:
                if resp.status >= 400:
                    return ""
                data = await resp.json()
            lrc = data.get("lrc", {})
            return lrc.get("lyric", "")
        except Exception:
            return ""

    # 3. 直链 mp3 —— 直接下载
    @handle("music.126.net",r"https?://[^/]*music\.126\.net/.*\.mp3(?:\?.*)?$")
    async def _parse_direct_mp3(self, searched: Match[str]):
        url = searched.group(0)  # 整条 url
        audio = self.create_audio_content(url)
        return self.result(
            title="网易云音乐",
            text="直链音频",
            contents=[audio],
            url=url,
        )

    @handle(
        "music.163.com/song/media/outer/url",
        r"(https?://music\.163\.com/song/media/outer/url\?[^>\s]+)",
    )
    async def _parse_private_outer(self, searched: Match[str]):
        # 整条原始 URL 就是直链
        private_url = searched.group(0)
        print(private_url)
        audio = self.create_audio_content(private_url)
        return self.result(
            title="网易云音乐（私人直链）",
            text="直链音频",
            contents=[audio],
            url=private_url,
        )

    # 4. 歌单解析
    @handle("ncmplaylist", r"music\.163\.com/.*playlist/(\d+)\/?(?:\?.*)?$")
    async def _parse_playlist(self, searched: Match[str]):
        playlist_id = searched.group(1)
        playlist_url = f"https://music.163.com/api/playlist/detail?id={playlist_id}"

        async with self.session.get(playlist_url, headers=self.headers) as resp:
            if resp.status >= 400:
                raise ClientError(f"[NCM] 获取歌单信息失败 HTTP {resp.status}")
            data = await resp.json()

        playlist = data.get("result", {})
        if not playlist:
            raise ValueError("[NCM] 未找到该歌单")

        name = playlist.get("name", "未知歌单")
        tracks = playlist.get("tracks", [])
        track_count = playlist.get("trackCount", len(tracks))
        cover_url = playlist.get("coverImgUrl", "")
        creator = playlist.get("creator", {})
        creator_name = creator.get("nickname", "")
        play_count = playlist.get("playCount", 0)
        subscribed_count = playlist.get("subscribedCount", 0)
        comment_count = playlist.get("commentCount", 0)
        description = playlist.get("description", "")

        # 构建歌曲列表文本
        lines = [f"🎵 歌单: {name} ({track_count}首)"]
        if creator_name:
            lines.append(f"创建者: {creator_name}")
        if description:
            lines.append(f"简介: {description[:200]}")
        lines.append("")
        for i, track in enumerate(tracks[:50], 1):
            artists = " / ".join(
                ar.get("name", "") for ar in track.get("artists", [])
            )
            song_name = track.get("name", "未知歌曲")
            lines.append(f"{i}. {artists} - {song_name}")

        if len(tracks) > 50:
            lines.append(f"\n... 共 {track_count} 首，仅显示前 50 首")

        text_content = TextContent("\n".join(lines))
        contents = [text_content]

        # 封面图
        if cover_url:
            contents.append(self.create_graphics_content(cover_url))

        # 统计数据
        stats = {}
        if play_count:
            stats["plays"] = play_count
        if subscribed_count:
            stats["favorites"] = subscribed_count
        if comment_count:
            stats["comments"] = comment_count

        return self.result(
            title=name,
            text=f"歌单共 {track_count} 首",
            author=self.create_author(creator_name, creator.get("avatarUrl")) if creator_name else None,
            contents=contents,
            url=f"https://music.163.com/#/playlist?id={playlist_id}",
            stats=stats,
            extra={"playlist_id": playlist_id},
        )

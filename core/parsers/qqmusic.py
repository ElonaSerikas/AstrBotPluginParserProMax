"""QQ音乐解析器

支持的链接格式:
  - 歌曲:  y.qq.com/n/yqq/song/<songmid>.html
  - 歌曲:  i.y.qq.com/v8/playsong.html?songid=<id>
  - 专辑:  y.qq.com/n/yqq/album/<albummid>.html
  - 歌单:  y.qq.com/n/yqq/playlist/<id>.html
  - 歌手:  y.qq.com/n/yqq/singer/<singermid>.html
"""

import base64
import re as _re
import time as _time
from asyncio import TimeoutError, sleep
from re import Match
from typing import Any, ClassVar

from aiohttp import ClientError

from ..config import PluginConfig
from ..data import (
    AudioContent,
    Comment,
    ImageContent,
    LyricLine,
    MusicInfo,
    Platform,
    TextContent,
)
from ..download import Downloader
from .base import BaseParser, handle


class QQMusicParser(BaseParser):
    """QQ音乐解析器"""

    platform: ClassVar[Platform] = Platform(
        name="qqmusic", display_name="QQ音乐"
    )

    _MUSICU = "https://u.y.qq.com/cgi-bin/musicu.fcg"
    _LYRIC = "https://i.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg"
    _COVER = "https://y.gtimg.cn/music/photo_new/T002R300x300M000{}.jpg"
    _STREAM = "https://ws.stream.qqmusic.qq.com"
    _MAX_AUDIO_DOWNLOADS = 3

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.headers["Referer"] = "https://y.qq.com"
        self.headers["Origin"] = "https://y.qq.com"

    # ======================== helpers ========================

    async def _musicu(
        self, modules: dict[str, dict], retries: int = 2
    ) -> dict[str, Any]:
        """QQ 音乐 unified API POST 请求"""
        payload: dict[str, Any] = {
            "comm": {"ct": 24, "cv": 0},
            **modules,
        }
        for attempt in range(retries + 1):
            try:
                async with self.session.post(
                    self._MUSICU, json=payload, headers=self.headers
                ) as resp:
                    if resp.status >= 400:
                        raise ClientError(
                            f"[QQMusic] musicu.fcg HTTP {resp.status}"
                        )
                    return await resp.json()
            except (ClientError, TimeoutError) as e:
                if attempt < retries:
                    await sleep(1 + attempt)
                    continue
                raise  # caller handles final failure

    async def _song_play(self, songmid: str) -> str:
        """通过 songmid 获取歌曲播放地址"""
        for prefix in ("C400", "M500"):
            fn = f"{prefix}{songmid}"
            try:
                data = await self._musicu({
                    "url": {
                        "module": "music.pf_url_svr",
                        "method": "url",
                        "param": {
                            "songmid": [songmid],
                            "songtype": [0],
                            "filename": [fn],
                        },
                    }
                })
                purl = (
                    data.get("url", {})
                    .get("data", {})
                    .get("midurlinfo", [{}])[0]
                    .get("purl", "")
                )
                if purl:
                    return f"{self._STREAM}/{purl.lstrip('/')}"
            except Exception:
                continue
        return ""

    async def _song_detail(self, songmid: str) -> dict[str, Any]:
        """获取歌曲详情 track_info"""
        data = await self._musicu({
            "detail": {
                "module": "music.pf_song_detail_svr",
                "method": "get_song_detail_yqq",
                "param": {"song_mid": songmid, "song_type": 0},
            }
        })
        return data.get("detail", {}).get("data", {}).get("track_info", {})

    async def _get_lyrics(self, songmid: str) -> list[LyricLine]:
        """获取歌词（若无歌词返回空列表）"""
        params = {"songmid": songmid, "loginUin": "0", "format": "json"}
        try:
            async with self.session.get(
                self._LYRIC, params=params, headers=self.headers
            ) as resp:
                if resp.status >= 400:
                    return []
                data = await resp.json()
            raw = data.get("lyric", "")
            if not raw:
                return []
            text = base64.b64decode(raw).decode("utf-8")
        except Exception:
            return []
        return self._lrc_parse(text)

    @staticmethod
    def _lrc_parse(text: str) -> list[LyricLine]:
        """解析 LRC 格式歌词文本"""
        lines: list[LyricLine] = []
        for line in text.splitlines():
            m = _re.match(
                r"\[(\d{2}):(\d{2})(?:\.(\d{2,3}))?\](.*)", line.strip()
            )
            if m:
                t = int(m.group(1)) * 60 + int(m.group(2))
                frac = m.group(3)
                if frac:
                    t += int(frac) / (1000 if len(frac) == 3 else 100)
                lines.append(LyricLine(time=t, text=m.group(4).strip()))
        return lines

    async def _get_singer_detail(self, singermid: str) -> dict:
        """获取歌手详情（粉丝数、签名等）"""
        try:
            data = await self._musicu({
                "singer": {
                    "module": "music.pf_singer_detail_svr",
                    "method": "get_singer_detail",
                    "param": {
                        "singermid": singermid,
                        "begin": 0,
                        "num": 1,
                    },
                }
            })
            sd = data.get("singer", {}).get("data", {})
            si = sd.get("singer_info", {}) or sd
            return {
                "follower_count": si.get("fans", 0) or si.get("total_fans", 0),
                "description": si.get("desc", "") or si.get("brief_desc", ""),
                "avatar": si.get("pic", "") or si.get("avatar", ""),
            }
        except Exception:
            return {}

    async def _get_comments(self, songmid: str) -> tuple[Comment | None, Comment | None, int]:
        """获取置顶评论、热评和评论总数（失败返回 (None, None, 0)）"""
        try:
            data = await self._musicu({
                "comment": {
                    "module": "music.globalComment.CommentRead",
                    "method": "GetCommentList",
                    "param": {
                        "bizType": 1,
                        "bizId": songmid,
                        "commentId": "",
                        "pageNum": 0,
                        "pageSize": 20,
                        "orderBy": 1,  # 按热度排序
                    },
                }
            })
            comments_data = data.get("comment", {}).get("data", {})
            comments = comments_data.get("CommentList", []) or comments_data.get("commentList", [])
            comment_count = comments_data.get("commentCount", 0) or comments_data.get("total", 0) or len(comments)
            if not comments:
                return None, None, 0

            def _build_comment(c: dict) -> Comment | None:
                content = c.get("content", "") or c.get("msg", "")
                if not content:
                    return None
                user = c.get("userinfo", {}) or c.get("user", {})
                return Comment(
                    author_name=user.get("nick", "") or user.get("nickname", ""),
                    content=content,
                    author_avatar=user.get("avatar", "") or user.get("headurl", ""),
                    likes=c.get("praisenum", 0) or c.get("likeNum", 0),
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
            return None, None, 0

    @staticmethod
    def _song_track(track: dict[str, Any]) -> dict[str, Any]:
        """从 track_info 提取显示的曲目信息"""
        title = track.get("title", "")
        subtitle = track.get("subtitle", "")
        display = f"{title}（{subtitle}）" if subtitle else title
        album = track.get("album", {})
        album_name = album.get("name", "")
        album_mid = album.get("mid", "")
        cover_url = (
            f"https://y.gtimg.cn/music/photo_new/T002R300x300M000{album_mid}.jpg"
            if album_mid
            else ""
        )
        singers = track.get("singer", [])
        singer_names = " / ".join(s.get("name", "") for s in singers)
        duration = int(track.get("interval", 0) or 0)
        return {
            "title": title,
            "display": display,
            "singer_names": singer_names,
            "album_name": album_name,
            "album_mid": album_mid,
            "cover_url": cover_url,
            "duration": duration,
        }

    # ======================== song ========================

    @handle(
        "y.qq.com/n/yqq/song",
        r"y\.qq\.com/n/yqq/song/(?P<songmid>[A-Za-z0-9]+)\.html\/?(?:\?.*)?$",
    )
    @handle(
        "i.y.qq.com/v8/playsong.html",
        r"i\.y\.qq\.com/v8/playsong\.html\?.*songid=(?P<songid>\d+)(?:&.*)?$",
    )
    async def _parse_song(self, searched: Match[str]) -> ...:  # noqa: ANN401
        groups = searched.groupdict()
        songmid = groups.get("songmid")

        # resolve songid -> songmid if needed
        if not songmid and groups.get("songid"):
            sid = groups["songid"]
            try:
                data = await self._musicu({
                    "resolve": {
                        "module": "music.pf_song_detail_svr",
                        "method": "get_song_detail_yqq",
                        "param": {"song_id": int(sid), "song_type": 0},
                    }
                })
                track = data.get("resolve", {}).get("data", {}).get("track_info", {})
                songmid = track.get("mid", "")
            except Exception as e:
                raise ValueError(
                    f"[QQMusic] 无法从 songid={sid} 解析: {e}"
                ) from e

        if not songmid:
            raise ValueError("[QQMusic] 无法解析歌曲链接")

        track = await self._song_detail(songmid)
        if not track:
            raise ValueError(f"[QQMusic] 未找到歌曲: {songmid}")

        t = self._song_track(track)

        # 提取歌手 uid
        singers_raw = track.get("singer", [])
        singer_uid = (
            str(
                singers_raw[0].get("mid")
                or singers_raw[0].get("id")
                or ""
            )
            or None
            if singers_raw
            else None
        )

        # 提取发布时间
        timestamp = None
        time_public = track.get("time_public", "")
        if time_public and isinstance(time_public, str) and len(time_public) == 10:
            try:
                timestamp = int(
                    _time.mktime(_time.strptime(time_public, "%Y-%m-%d"))
                )
            except (ValueError, OverflowError):
                pass
        if timestamp is None:
            ts_raw = track.get("ts")
            if isinstance(ts_raw, (int, float)):
                timestamp = int(ts_raw)

        # 并发获取：播放地址、歌词、歌手详情、评论
        import asyncio
        audio_url, lyrics, singer_info, (pinned_comment, hot_comment, comment_count) = await asyncio.gather(
            self._song_play(songmid),
            self._get_lyrics(songmid),
            self._get_singer_detail(singer_uid) if singer_uid else asyncio.sleep(0, result={}),
            self._get_comments(songmid),
        )

        contents: list = []

        if audio_url:
            contents.append(
                self.create_audio_content(
                    audio_url, duration=float(t["duration"])
                )
            )
        if t["cover_url"]:
            contents.extend(self.create_image_contents([t["cover_url"]]))

        # 构建歌手信息
        follower_count = singer_info.get("follower_count") or None
        description = singer_info.get("description") or None
        author = self.create_author(
            t["singer_names"], uid=singer_uid,
            description=description, follower_count=follower_count,
        ) if t["singer_names"] else None

        info_parts = []
        if t["singer_names"]:
            info_parts.append(f"歌手：{t['singer_names']}")
        if t["album_name"]:
            info_parts.append(f"专辑：{t['album_name']}")
        if t["duration"]:
            m, s = divmod(t["duration"], 60)
            info_parts.append(f"时长：{m}:{s:02d}")

        # 统计数据
        stats = {}
        listen_count = track.get("listen_count", 0) or track.get("listenCount", 0)
        if listen_count:
            stats["views"] = listen_count
        if comment_count:
            stats["comments"] = comment_count

        return self.result(
            title=t["display"],
            text="\n".join(info_parts),
            author=author,
            contents=contents,
            timestamp=timestamp,
            url=f"https://y.qq.com/n/yqq/song/{songmid}.html",
            music_info=MusicInfo(
                title=t["title"],
                artist=t["singer_names"],
                album=t["album_name"],
                cover_url=t["cover_url"],
                duration=t["duration"],
                lyrics=lyrics,
            ),
            stats=stats or None,
            extra={
                "uid": singer_uid or "",
                "song_id": songmid,
                "album_id": track.get("album", {}).get("mid", ""),
                "singer_id": singer_uid or "",
                "handle": f"歌手:{t['singer_names']}" if t["singer_names"] else "",
                "post_id": songmid,
            },
            page_type="song",
            pinned_comment=pinned_comment,
            hot_comment=hot_comment,
        )

    # ======================== album ========================

    @handle(
        "y.qq.com/n/yqq/album",
        r"y\.qq\.com/n/yqq/album/(?P<albummid>[A-Za-z0-9]+)\.html\/?(?:\?.*)?$",
    )
    async def _parse_album(self, searched: Match[str]) -> ...:  # noqa: ANN401
        albummid = searched.group("albummid")

        data = await self._musicu({
            "album": {
                "module": "music.pf_album_detail_svr",
                "method": "get_album_detail",
                "param": {"album_mid": albummid},
            }
        })
        album_out = data.get("album", {}).get("data", {})
        info = album_out.get("album_info", {}) or album_out
        name = info.get("name", "")
        cover_mid = info.get("mid", "") or albummid
        cover_url = self._COVER.format(cover_mid)
        singers = info.get("singer", []) or info.get("singers", [])
        singer_names = " / ".join(s.get("name", "") for s in singers)
        singer_uid = (
            str(singers[0].get("mid") or singers[0].get("id") or "")
            or None
            if singers
            else None
        )

        # 提取专辑发布时间
        album_timestamp = None
        for key in ("time", "pub_date", "aDate", "timestamp"):
            raw = info.get(key)
            if isinstance(raw, (int, float)):
                album_timestamp = int(raw)
                if album_timestamp > 10**12:
                    album_timestamp //= 1000
                break
            if isinstance(raw, str) and len(raw) == 10:
                try:
                    album_timestamp = int(
                        _time.mktime(_time.strptime(raw, "%Y-%m-%d"))
                    )
                    break
                except (ValueError, OverflowError):
                    continue

        song_list = album_out.get("list", []) or info.get("list", [])
        total = len(song_list)

        info_text = (
            f"歌手：{singer_names}\n歌曲数：{total}"
            if singer_names
            else f"歌曲数：{total}"
        )

        contents: list = []
        # cover first
        contents.extend(self.create_image_contents([cover_url]))

        # download audio for first few tracks
        downloaded = 0
        for song in song_list:
            if downloaded >= self._MAX_AUDIO_DOWNLOADS:
                break
            song_mid = song.get("mid", "")
            if not song_mid:
                continue
            try:
                u = await self._song_play(song_mid)
                if u:
                    dur = int(song.get("interval", 0) or 0)
                    contents.append(
                        self.create_audio_content(u, duration=float(dur))
                    )
                    downloaded += 1
            except Exception:
                pass

        # text listing for all tracks
        for i, song in enumerate(song_list):
            ttl = song.get("title", "")
            ss = " / ".join(
                sg.get("name", "")
                for sg in (song.get("singer", []) or [])
            )
            label = f"{i + 1}. {ttl}"
            if ss:
                label += f" - {ss}"
            contents.append(TextContent(label))

        return self.result(
            title=name,
            text=info_text,
            author=self.create_author(singer_names, uid=singer_uid)
            if singer_names
            else None,
            contents=contents,
            timestamp=album_timestamp,
            url=f"https://y.qq.com/n/yqq/album/{albummid}.html",
            extra={
                "album_id": albummid,
                "singer_id": singer_uid or "",
                "post_id": albummid,
            },
            page_type="album",
        )

    # ======================== playlist ========================

    @handle(
        "y.qq.com/n/yqq/playlist",
        r"y\.qq\.com/n/yqq/playlist/(?P<pid>\d+)\.html\/?(?:\?.*)?$",
    )
    async def _parse_playlist(self, searched: Match[str]) -> ...:  # noqa: ANN401
        pid = searched.group("pid")

        data = await self._musicu({
            "pl": {
                "module": "music.pf_playlist_svr",
                "method": "get_playlist_detail",
                "param": {
                    "id": int(pid),
                    "dirid": 0,
                    "num": 30,
                    "offset": 0,
                },
            }
        })
        pl = data.get("pl", {}).get("data", {})
        info = pl.get("playlist_info", {}) or pl
        title = info.get("name", "") or pl.get("title", "")
        creator = info.get("creator", {}) or pl.get("creator_info", {})
        creator_name = creator.get("name", "") or creator.get("nick", "")
        creator_uid = str(
            creator.get("id") or creator.get("uin") or ""
        ) or None

        # 提取歌单创建时间
        playlist_timestamp = None
        for key in ("create_time", "timestamp", "modify_time"):
            raw = info.get(key) or pl.get(key)
            if isinstance(raw, (int, float)):
                playlist_timestamp = int(raw)
                if playlist_timestamp > 10**12:
                    playlist_timestamp //= 1000
                break

        song_list = (
            pl.get("song", []) or pl.get("list", []) or pl.get("musicList", [])
        )
        total = len(song_list)

        contents: list = []
        for i, song in enumerate(song_list[:50]):
            sname = song.get("title", "") or song.get("name", "")
            ssingers = " / ".join(
                s.get("name", "") for s in (song.get("singer", []) or [])
            )
            label = f"{i + 1}. {sname}"
            if ssingers:
                label += f" - {ssingers}"
            contents.append(TextContent(label))

        info_text = (
            f"创建者：{creator_name}\n歌曲数：{total}"
            if creator_name
            else f"歌曲数：{total}"
        )

        return self.result(
            title=title,
            text=info_text,
            author=self.create_author(creator_name, uid=creator_uid)
            if creator_name
            else None,
            contents=contents,
            timestamp=playlist_timestamp,
            url=f"https://y.qq.com/n/yqq/playlist/{pid}.html",
            extra={"playlist_id": pid, "post_id": pid},
            page_type="playlist",
        )

    # ======================== singer ========================

    @handle(
        "y.qq.com/n/yqq/singer",
        r"y\.qq\.com/n/yqq/singer/(?P<singermid>[A-Za-z0-9]+)\.html\/?(?:\?.*)?$",
    )
    async def _parse_singer(self, searched: Match[str]) -> ...:  # noqa: ANN401
        smid = searched.group("singermid")

        data = await self._musicu({
            "singer": {
                "module": "music.pf_singer_detail_svr",
                "method": "get_singer_detail",
                "param": {
                    "singermid": smid,
                    "begin": 0,
                    "num": 30,
                },
            }
        })
        sd = data.get("singer", {}).get("data", {})
        si = sd.get("singer_info", {}) or sd
        name = si.get("name", "")
        singer_uid = str(
            si.get("id") or si.get("mid") or ""
        ) or None

        song_list = (
            sd.get("list", [])
            or sd.get("songlist", [])
            or sd.get("songs", [])
        )
        total = sd.get("total", 0) or len(song_list)

        contents: list = []
        for i, song in enumerate(song_list[:50]):
            sname = song.get("title", "") or song.get("name", "")
            album_n = (song.get("album", {}) or {}).get("name", "")
            label = f"{i + 1}. {sname}"
            if album_n:
                label += f" （{album_n}）"
            contents.append(TextContent(label))

        return self.result(
            title=name or "QQ音乐歌手",
            text=f"歌曲数：{total}",
            author=self.create_author(name, uid=singer_uid)
            if name
            else None,
            contents=contents,
            url=f"https://y.qq.com/n/yqq/singer/{smid}.html",
            extra={"singer_id": smid, "post_id": smid},
            page_type="singer",
        )

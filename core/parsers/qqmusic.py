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
from asyncio import TimeoutError, sleep
from re import Match
from typing import Any, ClassVar

from aiohttp import ClientError

from ..config import PluginConfig
from ..data import (
    AudioContent,
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
        r"y\.qq\.com/n/yqq/song/(?P<songmid>[A-Za-z0-9]+)\.html",
    )
    @handle(
        "i.y.qq.com/v8/playsong.html",
        r"i\.y\.qq\.com/v8/playsong\.html\?.*songid=(?P<songid>\d+)",
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

        audio_url = await self._song_play(songmid)
        lyrics = await self._get_lyrics(songmid)

        contents: list = []

        if audio_url:
            contents.append(
                self.create_audio_content(
                    audio_url, duration=float(t["duration"])
                )
            )
        if t["cover_url"]:
            contents.extend(self.create_image_contents([t["cover_url"]]))

        info_parts = []
        if t["singer_names"]:
            info_parts.append(f"歌手：{t['singer_names']}")
        if t["album_name"]:
            info_parts.append(f"专辑：{t['album_name']}")
        if t["duration"]:
            m, s = divmod(t["duration"], 60)
            info_parts.append(f"时长：{m}:{s:02d}")

        return self.result(
            title=t["display"],
            text="\n".join(info_parts),
            author=self.create_author(t["singer_names"])
            if t["singer_names"]
            else None,
            contents=contents,
            url=f"https://y.qq.com/n/yqq/song/{songmid}.html",
            music_info=MusicInfo(
                title=t["title"],
                artist=t["singer_names"],
                album=t["album_name"],
                cover_url=t["cover_url"],
                duration=t["duration"],
                lyrics=lyrics,
            ),
        )

    # ======================== album ========================

    @handle(
        "y.qq.com/n/yqq/album",
        r"y\.qq\.com/n/yqq/album/(?P<albummid>[A-Za-z0-9]+)\.html",
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
            author=self.create_author(singer_names)
            if singer_names
            else None,
            contents=contents,
            url=f"https://y.qq.com/n/yqq/album/{albummid}.html",
        )

    # ======================== playlist ========================

    @handle(
        "y.qq.com/n/yqq/playlist",
        r"y\.qq\.com/n/yqq/playlist/(?P<pid>\d+)\.html",
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
            author=self.create_author(creator_name)
            if creator_name
            else None,
            contents=contents,
            url=f"https://y.qq.com/n/yqq/playlist/{pid}.html",
        )

    # ======================== singer ========================

    @handle(
        "y.qq.com/n/yqq/singer",
        r"y\.qq\.com/n/yqq/singer/(?P<singermid>[A-Za-z0-9]+)\.html",
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
            author=self.create_author(name) if name else None,
            contents=contents,
            url=f"https://y.qq.com/n/yqq/singer/{smid}.html",
        )

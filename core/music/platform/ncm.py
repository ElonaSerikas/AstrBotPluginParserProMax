import asyncio
from typing import ClassVar

from astrbot.api import logger

from ..config import PluginConfig
from ..model import Platform, Song
from .base import BaseMusicPlayer


class NetEaseMusic(BaseMusicPlayer):
    """
    网易云音乐（Web API）
    """

    platform: ClassVar[Platform] = Platform(
        name="netease",
        display_name="网易云音乐",
        keywords=["网易云", "网易点歌"],
    )

    def __init__(self, config: PluginConfig):
        super().__init__(config)

    async def fetch_songs(self, keyword: str, limit=5, extra=None) -> list[Song]:
        result = await self._request(
            url="http://music.163.com/api/search/get/web",
            method="POST",
            data={"s": keyword, "limit": limit, "type": 1, "offset": 0},
            cookies={"appver": "2.0.2"},
        )
        if (
            not isinstance(result, dict)
            or "result" not in result
            or "songs" not in result["result"]
        ):
            logger.error(f"返回了意料之外数据：{result}")
            return []

        songs = result["result"]["songs"][:limit]

        songs_list = [
            Song(
                id=s.get("id"),
                name=s.get("name"),
                artists="、".join(a["name"] for a in s["artists"]),
                duration=s.get("duration"),
            )
            for s in songs
        ]
        # 并行获取封面等额外信息
        songs_list = await asyncio.gather(
            *[self.fetch_extra(s) for s in songs_list]
        )
        return list(songs_list)

    async def fetch_hot_songs(self, limit: int = 10) -> list[Song]:
        """网易云热歌榜（3778678 = 云音乐热歌榜）"""
        result = await self._request(
            url="http://music.163.com/api/playlist/detail?id=3778678",
            method="POST",
            cookies={"appver": "2.0.2"},
        )
        if not isinstance(result, dict) or "result" not in result:
            logger.warning("获取热歌榜失败")
            return []
        tracks = result["result"].get("tracks", [])
        songs = []
        for s in tracks[:limit]:
            song = Song(
                id=str(s.get("id", "")),
                name=s.get("name", ""),
                artists="、".join(a.get("name", "") for a in s.get("artists", [])),
                duration=s.get("duration", 0),
                cover_url=s.get("album", {}).get("picUrl", ""),
            )
            songs.append(song)
        return songs

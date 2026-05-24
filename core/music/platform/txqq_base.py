from abc import ABC
from typing import ClassVar

from astrbot.api import logger

from ..config import PluginConfig
from ..model import Platform, Song
from .base import BaseMusicPlayer


class TXQQBaseMusic(BaseMusicPlayer, ABC):
    """通过 api.txqq.pro 实现的平台共享基类"""

    _API_URL = "https://music.txqq.pro/"
    api_type: ClassVar[str] = ""  # 子类必须定义

    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:146.0) Gecko/20100101 Firefox/146.0",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": "https://music.txqq.pro",
        "Referer": "https://music.txqq.pro",
    }

    async def fetch_songs(
        self, keyword: str, limit: int = 5, extra: str | None = None
    ) -> list[Song]:
        payload = {
            "input": keyword,
            "filter": "name",
            "type": self.api_type,
            "page": 1,
        }
        try:
            async with self.session.post(
                self._API_URL, data=payload, headers=self.HEADERS
            ) as resp:
                data = await resp.json()
        except Exception as e:
            logger.warning(f"{self.platform.display_name} 搜索失败: {e}")
            return []

        if not isinstance(data, dict) or "data" not in data:
            return []

        songs = []
        for s in data["data"]:
            songs.append(
                Song(
                    id=s.get("songid"),
                    name=s.get("title"),
                    artists=s.get("author"),
                    audio_url=s.get("url") or s.get("link"),
                    cover_url=s.get("pic"),
                    lyrics=s.get("lrc", ""),
                )
            )
        return songs[:limit]

import hashlib
import random
from re import Match
from typing import ClassVar
from urllib.parse import parse_qs, urlparse

from aiohttp import ClientError

from ..config import PluginConfig
from ..data import Platform
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

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    @handle("t1.kugou.com", r"t1\.kugou\.com/(?P<code>\w+)")
    async def _parse_short(self, searched: Match[str]):
        """短链接 t1.kugou.com/xxx -> 重定向到真实页面"""
        short_url = f"https://t1.kugou.com/{searched.group('code')}"
        return await self.parse_with_redirect(short_url)

    @handle("kugou.com/mixsong", r"kugou\.com/mixsong/(?P<encode_id>[^/\?#]+)")
    async def _parse_song(self, searched: Match[str]):
        """单曲解析（hash 从 URL 片段或查询参数中提取）"""
        full_url = searched.string
        hash_val = self._extract_hash(full_url)
        if not hash_val:
            raise ValueError("[KuGou] 无法从链接中提取 hash 参数")
        return await self._fetch_song_data(hash_val, full_url)

    @handle(
        "kugou.com/yy/special",
        r"kugou\.com/yy/special/single/(?P<special_id>\d+)\.html",
    )
    async def _parse_playlist(self, searched: Match[str]):
        """歌单解析（仅返回基础信息）"""
        special_id = searched.group("special_id")
        url = f"https://kugou.com/yy/special/single/{special_id}.html"
        return self.result(
            title="酷狗歌单",
            text=f"歌单ID: {special_id}",
            url=url,
        )

    @handle(
        "kugou.com/yy/album",
        r"kugou\.com/yy/album/single/(?P<album_id>\d+)\.html",
    )
    async def _parse_album(self, searched: Match[str]):
        """专辑解析（仅返回基础信息）"""
        album_id = searched.group("album_id")
        url = f"https://kugou.com/yy/album/single/{album_id}.html"
        return self.result(
            title="酷狗专辑",
            text=f"专辑ID: {album_id}",
            url=url,
        )

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
        album_name = song_data.get("album_name", "") or ""
        cover_url = song_data.get("img", "") or ""
        duration_ms = song_data.get("time_length", 0)
        duration = (
            duration_ms / 1000
            if isinstance(duration_ms, (int, float))
            else 0
        )
        lyrics_text = song_data.get("lyrics", "") or ""
        bitrate = song_data.get("bitrate", 0) or 0

        # 作者 & 音频
        author = self.create_author(author_name, cover_url)
        audio = self.create_audio_content(play_url, duration=duration)
        contents = [audio]

        # 展示文字
        text_parts: list[str] = []
        if album_name:
            text_parts.append(f"专辑：{album_name}")
        if bitrate:
            text_parts.append(f"音质：{bitrate}kbps")

        if lyrics_text.strip():
            lrc_lines = [
                line.strip()
                for line in lyrics_text.strip().split("\n")
                if line.strip()
            ]
            text_parts.append("")
            text_parts.append("--- 歌词 ---")
            text_parts.extend(lrc_lines[:30])

        return self.result(
            title=title,
            text="\n".join(text_parts) if text_parts else None,
            author=author,
            contents=contents,
            url=source_url,
        )

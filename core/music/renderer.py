"""Music card renderer using HtmlCardRenderer (B站粉风格)"""
import base64
import os
from io import BytesIO

from ..render_html.engine import HtmlCardRenderer
from ..render_html.models import RenderPayload
from ..render_html.constants import get_platform_color


# 平台 → 歌曲页 URL 模板
_MUSIC_URL_TEMPLATES = {
    "ncm": "https://music.163.com/song?id={}",
    "netease": "https://music.163.com/song?id={}",
    "qqmusic": "https://y.qq.com/n/ryqq/songDetail/{}",
    "kugou": "https://www.kugou.com/song/#hash={}",
    "kuwo": "https://www.kuwo.cn/play_detail/{}",
    "migu": "https://music.migu.cn/v3/music/song/{}",
    "baidu": "https://music.taihe.com/song/{}",
}


def _format_duration(duration) -> str:
    """Convert duration (int ms or str) to a display string like '3:45'."""
    if isinstance(duration, int) and duration > 0:
        mins, secs = divmod(duration // 1000, 60)
        return f"{mins}:{secs:02d}"
    if isinstance(duration, str) and duration:
        return duration
    return ""


def _split_lyrics(lyrics: str | None, max_lines: int = 50) -> str:
    """Convert lyrics (LRC text or list) to <br>-separated HTML."""
    if not lyrics:
        return ""
    if isinstance(lyrics, str):
        lines = [line.strip() for line in lyrics.split("\n") if line.strip()]
    else:
        lines = list(lyrics)
    return "<br>".join(lines[:max_lines])


def _make_music_url(song_id: str, platform: str = "") -> str:
    """根据平台和歌曲ID生成歌曲页链接"""
    p = platform.lower().strip()
    template = _MUSIC_URL_TEMPLATES.get(p, "")
    if template and song_id:
        return template.format(song_id)
    return ""


def _make_qr_base64(url: str) -> str:
    """生成二维码 base64 data URI"""
    if not url:
        return ""
    try:
        import qrcode
        qr = qrcode.make(url)
        buf = BytesIO()
        qr.save(buf, format="PNG")
        qr_b64 = base64.b64encode(buf.getvalue()).decode()
        return f"data:image/png;base64,{qr_b64}"
    except Exception:
        return ""


async def _read_image_bytes(file_path: str | None) -> bytes | None:
    """从渲染引擎返回的文件路径读取图片 bytes"""
    if not file_path:
        return None
    try:
        with open(file_path, "rb") as f:
            data = f.read()
        # 清理临时文件
        try:
            os.unlink(file_path)
        except OSError:
            pass
        return data
    except Exception:
        return None


class MusicRenderer:
    """Renders music cards, lists, and lyrics using HtmlCardRenderer"""

    def __init__(self, star, config=None):
        self.html_renderer = HtmlCardRenderer(star, config)

    async def draw_song_card(
        self,
        song_name: str,
        artist: str,
        duration: int | str,
        cover_url: str | None = None,
        platform: str = "",
        comments: list[dict] | None = None,
        song_id: str = "",
    ) -> bytes | None:
        """渲染单曲卡片，返回图片 bytes"""
        # 生成 QR 码
        music_url = _make_music_url(song_id, platform)
        qr = _make_qr_base64(music_url)

        # 平台配色
        color = get_platform_color(platform.lower() if platform else "")

        # 时长文本
        dur_str = _format_duration(duration)

        # 热评
        pinned = None
        if comments and isinstance(comments, list) and len(comments) > 0:
            top = comments[0]
            content = top.get("content", "") if isinstance(top, dict) else str(top)
            if content:
                pinned = {"content": content[:100], "author": "", "likes": ""}

        payload = RenderPayload(
            name=artist or "",
            title=song_name or "",
            timestamp=dur_str,
            image_urls=[cover_url] if cover_url else [],
            type="music",
            platform_display=platform,
            platform_color=color or "#fb7299",
            card_width="600px",
            uid=song_id,
            qrcode=qr,
            pinned_comment=pinned,
            url=music_url,
        )
        result = await self.html_renderer.render_card(payload, style="music_card")
        return await _read_image_bytes(result)

    async def draw_song_list(
        self,
        songs: list,
        platform: str = "",
        title: str = "",
    ) -> bytes | None:
        """渲染歌曲选择列表，返回图片 bytes"""
        # 解析歌曲数据为结构化列表
        song_items = []
        for item in songs[:20]:
            if isinstance(item, (list, tuple)) and len(item) >= 1:
                s = item[0]
            else:
                s = item
            song_items.append({
                "name": s.name if hasattr(s, "name") else str(s),
                "artists": s.artists if hasattr(s, "artists") and s.artists else "",
                "duration": _format_duration(s.duration) if hasattr(s, "duration") and s.duration else "",
            })

        color = get_platform_color(platform.lower() if platform else "")

        payload = RenderPayload(
            title=title or "搜索结果",
            platform_display=platform,
            platform_color=color or "#fb7299",
            card_width="600px",
            songs=song_items,
        )
        result = await self.html_renderer.render_card(payload, style="music_list")
        return await _read_image_bytes(result)

    async def draw_lyrics(
        self,
        lyrics: str | None,
        platform: str = "",
        song_name: str = "",
        artist: str = "",
        cover_url: str | None = None,
        duration: int | str = 0,
        song_id: str = "",
    ) -> bytes | None:
        """渲染歌词卡片，返回图片 bytes"""
        music_url = _make_music_url(song_id, platform)
        qr = _make_qr_base64(music_url)
        color = get_platform_color(platform.lower() if platform else "")
        dur_str = _format_duration(duration)
        lyrics_html = _split_lyrics(lyrics)

        payload = RenderPayload(
            name=artist or "",
            title=song_name or "",
            text=lyrics_html,
            timestamp=dur_str,
            image_urls=[cover_url] if cover_url else [],
            type="lyrics",
            platform_display=platform,
            platform_color=color or "#fb7299",
            card_width="600px",
            qrcode=qr,
            url=music_url,
        )
        result = await self.html_renderer.render_card(payload, style="lyrics")
        return await _read_image_bytes(result)

    @classmethod
    async def close_browser(cls):
        pass

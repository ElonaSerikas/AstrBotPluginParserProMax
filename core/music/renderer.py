"""Music card renderer using HtmlCardRenderer (B站粉风格)"""
from ..render_html.engine import HtmlCardRenderer
from ..render_html.models import RenderPayload


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
        return "暂无歌词"
    if isinstance(lyrics, str):
        lines = [line.strip() for line in lyrics.split("\n") if line.strip()]
    else:
        lines = list(lyrics)
    return "<br>".join(lines[:max_lines])


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
    ):
        """Render a single song card (signature matches sender.py calls)"""
        text_parts = []
        if platform:
            text_parts.append(f"平台: {platform}")
        dur_str = _format_duration(duration)
        if dur_str:
            text_parts.append(f"时长: {dur_str}")
        if comments and isinstance(comments, list) and len(comments) > 0:
            top = comments[0]
            content = top.get("content", "") if isinstance(top, dict) else str(top)
            if content:
                text_parts.append(f"热评: {content[:80]}")

        payload = RenderPayload(
            name=artist or "",
            title=song_name or "",
            text="<br>".join(text_parts) if text_parts else "",
            image_urls=[cover_url] if cover_url else [],
            type="music",
            platform_color="#fb7299",
            card_width="600px",
        )
        return await self.html_renderer.render_card(payload, style="music_card")

    async def draw_song_list(
        self,
        songs: list,
        platform: str = "",
        title: str = "",
    ):
        """Render a song selection list (signature matches sender.py calls)"""
        items = []
        for i, item in enumerate(songs[:20], 1):
            # songs can be list[Song] or list[tuple[Song, str]]
            if isinstance(item, (list, tuple)) and len(item) >= 1:
                s = item[0]
            else:
                s = item
            name = s.name if hasattr(s, "name") else str(s)
            items.append(f"{i}. {name}")
        payload = RenderPayload(
            title=title or "",
            text="<br>".join(items),
            type="music_list",
            platform_color="#fb7299",
            card_width="600px",
        )
        return await self.html_renderer.render_card(payload, style="music_list")

    async def draw_lyrics(
        self,
        lyrics: str | None,
        platform: str = "",
        song_name: str = "",
        artist: str = "",
        cover_url: str | None = None,
        duration: int | str = 0,
    ):
        """Render lyrics (signature matches sender.py calls)"""
        text_parts = []
        if platform:
            text_parts.append(f"平台: {platform}")
        dur_str = _format_duration(duration)
        if dur_str:
            text_parts.append(f"时长: {dur_str}")
        text_parts.append(_split_lyrics(lyrics))

        payload = RenderPayload(
            name=artist or "",
            title=song_name or "",
            text="<br>".join(text_parts),
            image_urls=[cover_url] if cover_url else [],
            type="lyrics",
            platform_color="#fb7299",
            card_width="600px",
        )
        return await self.html_renderer.render_card(payload, style="lyrics")

    @classmethod
    async def close_browser(cls):
        pass  # No standalone browser, using star.html_render

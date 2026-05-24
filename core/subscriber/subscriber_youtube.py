"""YouTube subscriber - polls channel videos via RSS"""
from typing import Optional
import aiohttp
import xml.etree.ElementTree as ET

from .base import BaseSubscriber, SubUpdate, SubUserInfo


class YouTubeSubscriber(BaseSubscriber):
    platform = "youtube"

    def __init__(self, session=None):
        self._session = session

    async def fetch_updates(self, uid: str) -> list[SubUpdate]:
        """Fetch latest videos via YouTube RSS"""
        # YouTube channel RSS: https://www.youtube.com/feeds/videos.xml?channel_id={uid}
        # But uid here is typically the channel ID
        updates = []
        rss_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={uid}"

        session = self._session or aiohttp.ClientSession()
        try:
            async with session.get(rss_url) as resp:
                if resp.status != 200:
                    return []
                text = await resp.text()

            root = ET.fromstring(text)
            ns = {"atom": "http://www.w3.org/2005/Atom"}

            for entry in root.findall("atom:entry", ns)[:5]:
                video_id = entry.find("atom:id", ns)
                title = entry.find("atom:title", ns)
                link = entry.find("atom:link", ns)
                published = entry.find("atom:published", ns)

                video_id_str = video_id.text.split(":")[-1] if video_id is not None else ""
                updates.append(SubUpdate(
                    id=video_id_str,
                    platform="youtube",
                    uid=uid,
                    type="video",
                    title=title.text if title is not None else "",
                    url=f"https://youtu.be/{video_id_str}",
                    timestamp=self._parse_rss_time(published.text if published is not None else ""),
                ))
        finally:
            if self._session is None:
                await session.close()

        return updates

    async def get_user_info(self, uid: str) -> Optional[SubUserInfo]:
        # YouTube RSS doesn't provide user info directly
        return SubUserInfo(platform="youtube", uid=uid, name=f"YouTuber {uid[:8]}")

    @staticmethod
    def _parse_rss_time(time_str: str) -> int:
        from datetime import datetime
        try:
            dt = datetime.strptime(time_str, "%Y-%m-%dT%H:%M:%S%z")
            return int(dt.timestamp())
        except (ValueError, IndexError):
            return 0

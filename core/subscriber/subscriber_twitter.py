"""Twitter/X subscriber - polls via Nitter RSS"""
from typing import Optional
import aiohttp
import xml.etree.ElementTree as ET

from .base import BaseSubscriber, SubUpdate, SubUserInfo


class TwitterSubscriber(BaseSubscriber):
    platform = "twitter"

    NITTER_INSTANCES = [
        "https://nitter.net",
        "https://nitter.privacydev.net",
        "https://nitter.1d4.us",
    ]

    def __init__(self, session=None):
        self._session = session

    async def fetch_updates(self, uid: str) -> list[SubUpdate]:
        updates = []
        session = self._session or aiohttp.ClientSession()

        try:
            for instance in self.NITTER_INSTANCES:
                url = f"{instance}/{uid}/rss"
                try:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                        if resp.status == 200:
                            text = await resp.text()
                            break
                except Exception:
                    continue
            else:
                return []  # All instances failed

            root = ET.fromstring(text)
            ns = {"atom": "http://www.w3.org/2005/Atom"}

            for entry in root.findall("atom:entry", ns)[:5]:
                title_el = entry.find("atom:title", ns)
                link_el = entry.find("atom:link", ns)
                published = entry.find("atom:published", ns)
                tweet_id = link_el.get("href", "").split("/")[-1] if link_el is not None else ""

                updates.append(SubUpdate(
                    id=tweet_id,
                    platform="twitter",
                    uid=uid,
                    type="text",
                    title=title_el.text[:100] if title_el is not None and title_el.text else "",
                    text=title_el.text if title_el is not None else "",
                    url=link_el.get("href", "") if link_el is not None else "",
                ))
        finally:
            if self._session is None:
                await session.close()

        return updates

    async def get_user_info(self, uid: str) -> Optional[SubUserInfo]:
        return SubUserInfo(platform="twitter", uid=uid, name=f"@{uid}")

"""Xiaohongshu (小红书) subscriber - polls user notes"""
from typing import Optional
from .base import BaseSubscriber, SubUpdate, SubUserInfo


class XHSSubscriber(BaseSubscriber):
    platform = "xhs"

    def __init__(self, session=None):
        self._session = session

    async def fetch_updates(self, uid: str) -> list[SubUpdate]:
        """Fetch latest notes from a 小红书 user"""
        # Try to fetch user feed - this is a stub that returns empty
        # Actual implementation would need to scrape or use API
        return []

    async def get_user_info(self, uid: str) -> Optional[SubUserInfo]:
        return None

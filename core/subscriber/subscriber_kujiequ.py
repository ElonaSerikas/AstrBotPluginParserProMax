"""Kujiequ (库街区) subscriber"""
from typing import Optional
import aiohttp
import hashlib
import time

from .base import BaseSubscriber, SubUpdate, SubUserInfo


class KujiequSubscriber(BaseSubscriber):
    platform = "kujiequ"

    def __init__(self, session=None):
        self._session = session

    async def fetch_updates(self, uid: str) -> list[SubUpdate]:
        # Stub - would need Kurobbs forum list API
        return []

    async def get_user_info(self, uid: str) -> Optional[SubUserInfo]:
        return None

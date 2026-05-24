from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class SubUpdate:
    """Unified subscription update data"""
    id: str
    platform: str
    uid: str
    type: str  # video/image/article/live
    title: str = ""
    text: str = ""
    image_urls: list[str] = field(default_factory=list)
    url: str = ""
    timestamp: int = 0


@dataclass
class SubUserInfo:
    platform: str
    uid: str
    name: str = ""
    avatar: str = ""


class BaseSubscriber(ABC):
    """Abstract base for all platform subscribers"""

    platform: str = ""

    @abstractmethod
    async def fetch_updates(self, uid: str) -> list[SubUpdate]:
        """Fetch latest updates for a user"""
        ...

    @abstractmethod
    async def get_user_info(self, uid: str) -> Optional[SubUserInfo]:
        """Get user display info"""
        ...

    def get_dedup_key(self, update: SubUpdate) -> str:
        return f"{self.platform}:{update.id}"

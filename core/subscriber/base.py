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
    image_count: int = 0       # 图片数量
    video_url: str = ""        # 视频 URL
    video_duration: str = ""   # 视频时长
    cover_url: str = ""        # 封面图


@dataclass
class SubUserInfo:
    platform: str
    uid: str
    name: str = ""
    avatar: str = ""
    handle: str = ""           # 平台专属账号名（如 @xxx）
    follower_count: int = 0    # 粉丝数/订阅量
    signature: str = ""        # 签名


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

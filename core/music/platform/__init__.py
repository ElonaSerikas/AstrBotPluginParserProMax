from .base import BaseMusicPlayer
from .ncm import NetEaseMusic
from .ncm_nodejs import NetEaseMusicNodeJS
from .qq import QQMusic
from .kugou import KuGouMusic
from .kuwo import KuWoMusic
from .baidu import BaiduMusic
from .migu import MiGuMusic

__all__ = [
    "NetEaseMusic",
    "NetEaseMusicNodeJS",
    "BaseMusicPlayer",
    "QQMusic",
    "KuGouMusic",
    "KuWoMusic",
    "BaiduMusic",
    "MiGuMusic",
]

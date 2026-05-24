from ..model import Platform
from .txqq_base import TXQQBaseMusic


class KuWoMusic(TXQQBaseMusic):
    api_type = "kuwo"
    platform = Platform(
        name="kuwo",
        display_name="酷我音乐",
        keywords=["酷我点歌"],
    )

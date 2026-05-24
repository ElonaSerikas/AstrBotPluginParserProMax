from ..model import Platform
from .txqq_base import TXQQBaseMusic


class KuGouMusic(TXQQBaseMusic):
    api_type = "kugou"
    platform = Platform(
        name="kugou",
        display_name="酷狗音乐",
        keywords=["酷狗点歌"],
    )

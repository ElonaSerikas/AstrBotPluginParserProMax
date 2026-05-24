from ..model import Platform
from .txqq_base import TXQQBaseMusic


class QQMusic(TXQQBaseMusic):
    api_type = "qq"
    platform = Platform(
        name="qq",
        display_name="QQ音乐",
        keywords=["QQ点歌"],
    )

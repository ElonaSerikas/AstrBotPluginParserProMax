from ..model import Platform
from .txqq_base import TXQQBaseMusic


class MiGuMusic(TXQQBaseMusic):
    api_type = "migu"
    platform = Platform(
        name="migu",
        display_name="咪咕音乐",
        keywords=["咪咕点歌"],
    )

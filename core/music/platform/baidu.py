from ..model import Platform
from .txqq_base import TXQQBaseMusic


class BaiduMusic(TXQQBaseMusic):
    api_type = "baidu"
    platform = Platform(
        name="baidu",
        display_name="百度音乐",
        keywords=["百度点歌"],
    )

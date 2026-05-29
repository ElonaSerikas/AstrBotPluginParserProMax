from __future__ import annotations

import json
from pathlib import Path
import zoneinfo
from collections.abc import Mapping, MutableMapping
from types import MappingProxyType, UnionType
from typing import Any, Union, get_args, get_origin, get_type_hints

from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.star.context import Context
from astrbot.core.star.star_tools import StarTools
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_path


class ConfigNode:
    """
    配置节点, 把 dict 变成强类型对象。

    规则：
    - schema 来自子类类型注解
    - 声明字段：读写，写回底层 dict
    - 未声明字段和下划线字段：仅挂载属性，不写回
    - 支持 ConfigNode 多层嵌套（lazy + cache）
    """

    _SCHEMA_CACHE: dict[type, dict[str, type]] = {}
    _FIELDS_CACHE: dict[type, set[str]] = {}

    @classmethod
    def _schema(cls) -> dict[str, type]:
        return cls._SCHEMA_CACHE.setdefault(cls, get_type_hints(cls))

    @classmethod
    def _fields(cls) -> set[str]:
        return cls._FIELDS_CACHE.setdefault(
            cls,
            {k for k in cls._schema() if not k.startswith("_")},
        )

    @staticmethod
    def _is_optional(tp: type) -> bool:
        if get_origin(tp) in (Union, UnionType):
            return type(None) in get_args(tp)
        return False

    def __init__(self, data: MutableMapping[str, Any]):
        object.__setattr__(self, "_data", data)
        object.__setattr__(self, "_children", {})
        for key, tp in self._schema().items():
            if key.startswith("_"):
                continue
            if key in data:
                continue
            if hasattr(self.__class__, key):
                continue
            if self._is_optional(tp):
                continue
            logger.warning(f"[config:{self.__class__.__name__}] 缺少字段: {key}")

    def __getattr__(self, key: str) -> Any:
        if key in self._fields():
            value = self._data.get(key)
            tp = self._schema().get(key)

            if isinstance(tp, type) and issubclass(tp, ConfigNode):
                children: dict[str, ConfigNode] = self.__dict__["_children"]
                if key not in children:
                    if not isinstance(value, MutableMapping):
                        raise TypeError(
                            f"[config:{self.__class__.__name__}] "
                            f"字段 {key} 期望 dict，实际是 {type(value).__name__}"
                        )
                    children[key] = tp(value)
                return children[key]

            return value

        if key in self.__dict__:
            return self.__dict__[key]

        raise AttributeError(key)

    def __setattr__(self, key: str, value: Any) -> None:
        if key in self._fields():
            self._data[key] = value
            return
        object.__setattr__(self, key, value)

    def raw_data(self) -> Mapping[str, Any]:
        """
        底层配置 dict 的只读视图
        """
        return MappingProxyType(self._data)

    def save_config(self) -> None:
        """
        保存配置到磁盘（仅允许在根节点调用）
        """
        if not isinstance(self._data, AstrBotConfig):
            raise RuntimeError(
                f"{self.__class__.__name__}.save_config() 只能在根配置节点上调用"
            )
        self._data.save_config()


class ConfigNodeContainer:
    """
    配置节点容器, 把 list 的 dict 变成 dict 的对象集合。

    - nodes: list[dict[str, Any]]
    - item_cls 用于包装 dict 成强类型节点
    - key_name 作为属性名访问, 默认为 "__template_key"
    """

    def __init__(
        self,
        nodes: list[dict[str, Any]],
        item_cls: type[ConfigNode],
        key_name="__template_key",
    ):
        self._nodes: dict[str, ConfigNode] = {}
        for node in nodes:
            key = node.get(key_name)
            if not key:
                logger.warning(f"[node] 缺少 {key_name}，已跳过")
                continue
            if key in self._nodes:
                logger.warning(f"[node] {key} 重复配置，已覆盖")
            self._nodes[key] = item_cls(node)

    def __getattr__(self, name: str) -> ConfigNode:
        if name in self._nodes:
            return self._nodes[name]
        raise AttributeError(name)

    def __iter__(self):
        return iter(self._nodes.values())

    def keys(self):
        return self._nodes.keys()

    def items(self):
        return self._nodes.items()


# ================ 插件自定义配置 ==================


class ParserItem(ConfigNode):
    __template_key: str
    enable: bool
    use_proxy: bool
    cookies: str | None
    show_body_text: bool | None
    video_send_mode: str | None
    video_codecs: str | None
    video_quality: str | None

    @property
    def name(self) -> str:
        return self._data.get("__template_key")


class ParserConfig(ConfigNodeContainer):
    acfun: ParserItem
    bilibili: ParserItem
    douyin: ParserItem
    facebook: ParserItem
    instagram: ParserItem
    kuaishou: ParserItem
    ncm: ParserItem
    nga: ParserItem
    telegram: ParserItem
    tiktok: ParserItem
    twitter: ParserItem
    weibo: ParserItem
    xiaoheihe: ParserItem
    zhihu: ParserItem
    xhs: ParserItem
    youtube: ParserItem
    lofter: ParserItem
    mihuashi: ParserItem
    cpp: ParserItem
    huajia: ParserItem
    kujiequ: ParserItem
    kugou: ParserItem
    kuwo: ParserItem
    pixiv: ParserItem
    qqmusic: ParserItem

    def __init__(self, nodes: list[dict[str, Any]]):
        super().__init__(nodes, item_cls=ParserItem)

    def platforms(self) -> list[str]:
        return list(self._nodes.keys())

    def enabled_platforms(self) -> list[str]:
        return [k for k, v in self._nodes.items() if getattr(v, "enable", True)]


class PluginConfig(ConfigNode):
    whitelist: list[str]
    blacklist: list[str]

    arbiter: bool
    debounce_interval: int

    source_max_size: int
    source_max_minute: int

    audio_to_file: bool
    single_heavy_render_card: bool
    forward_threshold: int

    show_download_fail_tip: bool
    debug_mode: bool
    download_timeout: int
    download_retry_times: int
    common_timeout: int

    proxy: str | None

    clean_cron: str

    use_html_render: bool
    card_width: int
    sessdata: str | None
    bangumi_token: str | None
    interval_secs: int
    task_gap_secs: int
    rai: bool
    node: bool
    enable_parse_miniapp: bool
    enable_parse_BV: bool
    dynamic_limit: int
    enable_comments: bool
    comment_limit: int
    max_video_size: int
    cookie_encrypt_key: str | None
    cookie_retention_days: int
    music_default_player: str
    music_send_mode: str
    music_playlist_limit: int
    music_nodejs_url: str
    real_song_limit: int | None
    timeout_recall: bool | None

    # Music subsystem fields (optional — set with defaults in __init__)
    music_send_modes: list[str] | None
    music_record_supported: list[str] | None
    music_file_supported: list[str] | None
    music_clear_cache: bool | None
    music_enable_lyrics: bool | None
    enc_params: str | None
    enc_sec_key: str | None

    parsers_template: list[dict[str, Any]]

    _plugin_name = "astrbot_plugin_parser"

    def __init__(self, config: AstrBotConfig, context: Context):
        super().__init__(config)
        self.context = context
        self.admins_id = self.context.get_config().get("admins_id", [])

        # ---------- 内置配置 ----------
        self.emoji_cdn = "https://cdn.jsdelivr.net/npm/emoji-datasource-facebook@14.0.0/img/facebook/64/"
        self.emoji_style = "FACEBOOK"  # 可选：APPLE、FACEBOOK、GOOGLE、TWITTER

        # ---------- 派生字段 ----------
        self.proxy = self.proxy or None
        self.max_duration = self.source_max_minute * 60
        self.max_size = self.source_max_size * 1024 * 1024

        tz = context.get_config().get("timezone")
        self.timezone = (
            zoneinfo.ZoneInfo(tz) if tz else zoneinfo.ZoneInfo("Asia/Shanghai")
        )

        # ---------- 路径 ----------
        self.data_dir = StarTools.get_data_dir(self._plugin_name)
        self.plugin_dir = Path(get_astrbot_plugin_path()) / self._plugin_name
        self.cache_dir = self.data_dir / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cookie_dir = self.data_dir / "cookies"
        self.cookie_dir.mkdir(parents=True, exist_ok=True)
        self.default_template_file = self.plugin_dir / "default_template.json"

        # ---------- Parser ----------
        if not self.parsers_template:
            self.parsers_template[:] = self.load_parser_template(
                self.default_template_file
            )
            self.save_config()

        self.parser = ParserConfig(self.parsers_template)

        # ---------- Music subsystem defaults ----------
        self.real_song_limit = self._data.get("real_song_limit", 10)
        self.timeout_recall = self._data.get("timeout_recall", False)
        self.music_send_modes = self._data.get("music_send_modes", ["card", "text"])
        self.music_record_supported = self._data.get(
            "music_record_supported", ["ncm"]
        )
        self.music_file_supported = self._data.get(
            "music_file_supported", ["ncm", "netease"]
        )
        self.music_clear_cache = self._data.get("music_clear_cache", True)
        self.music_enable_lyrics = self._data.get("music_enable_lyrics", True)
        self.enc_params = self._data.get("enc_params", "")
        self.enc_sec_key = self._data.get("enc_sec_key", "")

    @staticmethod
    def load_parser_template(file: Path) -> list[dict[str, Any]]:
        try:
            with file.open(encoding="utf-8-sig") as f:
                template = json.loads(f.read())
                logger.info(f"[parser] 加载模板成功: {file}")
                return template
        except Exception as e:
            logger.error(f"[parser] 加载模板失败: {e}")
            return []

    def add_blacklist(self, umo: str):
        if umo not in self.blacklist:
            self.blacklist.append(umo)
            self.save_config()

    def remove_blacklist(self, umo: str):
        if umo in self.blacklist:
            self.blacklist.remove(umo)
            self.save_config()

    # =====================================================================
    # Property aliases for music subsystem backward compatibility
    # (maps old music-plugin field names to the parser's config fields)
    # =====================================================================

    @property
    def default_player_name(self) -> str:
        return self.music_default_player

    @default_player_name.setter
    def default_player_name(self, value: str) -> None:
        self.music_default_player = value

    @property
    def timeout(self) -> int:
        return self.common_timeout

    @timeout.setter
    def timeout(self, value: int) -> None:
        self.common_timeout = value

    @property
    def real_send_modes(self) -> list[str] | None:
        raw = self.music_send_modes
        if isinstance(raw, str):
            return [m.split("(", 1)[0].strip() for m in raw.split(",") if m.strip()]
        return raw

    @real_send_modes.setter
    def real_send_modes(self, value: list[str] | None) -> None:
        self.music_send_modes = value

    @property
    def http_proxy(self) -> str | None:
        return self.proxy

    @http_proxy.setter
    def http_proxy(self, value: str | None) -> None:
        self.proxy = value

    @property
    def nodejs_base_url(self) -> str:
        return self.music_nodejs_url

    @nodejs_base_url.setter
    def nodejs_base_url(self, value: str) -> None:
        self.music_nodejs_url = value

    @property
    def record_supported(self) -> list[str] | None:
        raw = self.music_record_supported
        if isinstance(raw, str):
            return [m.strip() for m in raw.split(",") if m.strip()]
        return raw

    @record_supported.setter
    def record_supported(self, value: list[str] | None) -> None:
        self.music_record_supported = value

    @property
    def file_supported(self) -> list[str] | None:
        raw = self.music_file_supported
        if isinstance(raw, str):
            return [m.strip() for m in raw.split(",") if m.strip()]
        return raw

    @file_supported.setter
    def file_supported(self, value: list[str] | None) -> None:
        self.music_file_supported = value

    @property
    def clear_cache(self) -> bool | None:
        return self.music_clear_cache

    @clear_cache.setter
    def clear_cache(self, value: bool | None) -> None:
        self.music_clear_cache = value

    @property
    def enable_lyrics(self) -> bool | None:
        return self.music_enable_lyrics

    @enable_lyrics.setter
    def enable_lyrics(self, value: bool | None) -> None:
        self.music_enable_lyrics = value

    @property
    def playlist_limit(self) -> int:
        return self.music_playlist_limit

    @playlist_limit.setter
    def playlist_limit(self, value: int) -> None:
        self.music_playlist_limit = value

    @property
    def playlist_dir(self) -> Path:
        return self.data_dir / "music_playlists"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "music_playlists" / "playlist.db"

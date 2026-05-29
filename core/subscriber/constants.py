import os
from typing import Dict

CURRENT_DIR = os.path.dirname(__file__)
ASSETS_DIR = os.path.join(CURRENT_DIR, "..", "resources")


def _asset_path(*parts: str) -> str:
    return os.path.join(ASSETS_DIR, *parts)


LOGO_PATH = _asset_path("astrbot_logo.png")
BANNER_PATH = _asset_path("banner.png")

# Re-export shared constants from render_html.constants (single source of truth)
from ..render_html.constants import (  # noqa: E402
    BV,
    CARD_TEMPLATES,
    DEFAULT_TEMPLATE,
    LIVE_ATALL_OPTION,
    MAX_ATTEMPTS,
    RECENT_DYNAMIC_CACHE,
    RECONNECT_SILENT_PADDING_SECS,
    RECONNECT_SILENT_THRESHOLD_SECS,
    RETRY_DELAY,
    VALID_FILTER_TYPES,
    VALID_SUB_OPTIONS,
    get_template_names,
    get_template_path,
)

DATA_PATH = "data/astrbot_plugin_parser.json"
DEFAULT_CFG = {
    "bili_sub_list": {},  # sub_user -> [{"uid": "uid", "last": "last_dynamic_id", ...}]
    "sub_list": {},       # 多平台订阅：sub_user -> [{"uid": "uid", "platform": "xxx", ...}]
    "credential": None,
    "last_success_sub_notify_ts": 0,
}

from pathlib import Path
from typing import Dict

# 模板根目录
TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
BILIBILI_TEMPLATES_DIR = TEMPLATES_DIR / "bilibili"
RESOURCES_DIR = Path(__file__).parent.parent / "resources"

BV = r"(?:\?.*)?(?:https?:\/\/)?(?:www\.)?(?:bilibili\.com\/video\/(BV[a-zA-Z0-9]+)|b23\.tv\/([a-zA-Z0-9]+))\/?(?:\?.*)?|BV[a-zA-Z0-9]+"
VALID_FILTER_TYPES = {
    "forward", "lottery", "video", "article", "draw", "live", "forward_lottery",
}
LIVE_ATALL_OPTION = "live_atall"
VALID_SUB_OPTIONS = {LIVE_ATALL_OPTION}

# ==================== 模板注册表 ====================
CARD_TEMPLATES: Dict[str, dict] = {
    "universal_card": {
        "name": "通用卡片",
        "description": "1440px 全平台通用卡片",
        "file": "universal_card.html",
        "path": str(TEMPLATES_DIR / "universal_card.html"),
    },
    "universal_push": {
        "name": "推送窄卡",
        "description": "640px 订阅推送窄卡片",
        "file": "universal_push.html",
        "path": str(TEMPLATES_DIR / "universal_push.html"),
    },
    "help": {
        "name": "帮助页面",
        "description": "800px 使用帮助卡片",
        "file": "help.html",
        "path": str(TEMPLATES_DIR / "help.html"),
    },
    "template_1": {
        "name": "经典风格",
        "description": "原版设计",
        "file": "template_1.html",
        "path": str(BILIBILI_TEMPLATES_DIR / "template_1.html"),
    },
    "template_2": {
        "name": "B站粉风格",
        "description": "B站风格设计（默认）",
        "file": "template_2.html",
        "path": str(BILIBILI_TEMPLATES_DIR / "template_2.html"),
    },
    "simple": {
        "name": "简约风格",
        "description": "简洁现代的设计",
        "file": "template_simple.html",
        "path": str(BILIBILI_TEMPLATES_DIR / "template_simple.html"),
    },
    "music_card": {
        "name": "歌曲卡片",
        "description": "单曲详情卡片（封面+热评+歌词预览）",
        "file": "music_card.html",
        "path": str(TEMPLATES_DIR / "music_card.html"),
    },
    "music_list": {
        "name": "歌曲列表",
        "description": "搜索结果/歌单列表卡片",
        "file": "music_list.html",
        "path": str(TEMPLATES_DIR / "music_list.html"),
    },
    "lyrics": {
        "name": "歌词卡片",
        "description": "歌词展示卡片（封面+歌词）",
        "file": "lyrics.html",
        "path": str(TEMPLATES_DIR / "lyrics.html"),
    },
}

DEFAULT_TEMPLATE = "universal_card"

PLATFORM_COLORS = {
    "bilibili": "#fb7299",
    "douyin": "#fe2c55",
    "weibo": "#ff8200",
    "xhs": "#ff2442",
    "youtube": "#ff0000",
    "twitter": "#1da1f2",
    "pixiv": "#0096fa",
    "qqmusic": "#00c853",
    "kugou": "#ff5722",
    "kuwo": "#ff6f00",
    "acfun": "#fd4c5d",
    "instagram": "#e4405f",
    "telegram": "#0088cc",
    "tiktok": "#25f4ee",
    "ncm": "#c20c0c",
    "kuaishou": "#ff4800",
    "xiaoheihe": "#3a7bd5",
    "zhihu": "#0084ff",
    "nga": "#ff6b35",
    "lofter": "#39c5bb",
    "mihuashi": "#ff6fa0",
    "huajia": "#fb7299",
    "cpp": "#734c9e",
    "kujiequ": "#fe8b2c",
}


def get_template_path(style: str) -> str:
    template = CARD_TEMPLATES.get(style, CARD_TEMPLATES[DEFAULT_TEMPLATE])
    return template["path"]


def get_template_names() -> list:
    return list(CARD_TEMPLATES.keys())


def get_platform_color(platform: str) -> str:
    return PLATFORM_COLORS.get(platform, "#666666")


MAX_ATTEMPTS = 3
RETRY_DELAY = 2
RECENT_DYNAMIC_CACHE = 4
RECONNECT_SILENT_THRESHOLD_SECS = 21600
RECONNECT_SILENT_PADDING_SECS = 60

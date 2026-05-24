"""
帮助系统 - 生成纯文本和图片两种模式的帮助指引。
"""

from typing import Optional

from astrbot.api import logger


# 全平台列表
PLATFORM_HELP = {
    "bilibili": {
        "name": "B站",
        "types": "视频/专栏/动态/直播/收藏夹",
        "features": "解析链接 / 订阅UP主 / 番剧推荐 / 热榜",
    },
    "douyin": {"name": "抖音", "types": "视频/图文", "features": "解析链接"},
    "weibo": {"name": "微博", "types": "视频/图文/文章", "features": "解析链接"},
    "xhs": {"name": "小红书", "types": "笔记/视频", "features": "解析链接 / 订阅博主"},
    "zhihu": {"name": "知乎", "types": "回答/文章/想法", "features": "解析链接"},
    "kuaishou": {"name": "快手", "types": "视频", "features": "解析链接"},
    "acfun": {"name": "A站", "types": "视频", "features": "解析链接"},
    "youtube": {"name": "YouTube", "types": "视频/频道", "features": "解析链接 / 订阅"},
    "tiktok": {"name": "TikTok", "types": "视频", "features": "解析链接"},
    "twitter": {"name": "Twitter/X", "types": "推文/图片", "features": "解析链接 / 订阅"},
    "instagram": {"name": "Instagram", "types": "帖子/图文", "features": "解析链接"},
    "pixiv": {"name": "Pixiv", "types": "插画/漫画", "features": "解析链接"},
    "ncm": {"name": "网易云音乐", "types": "歌曲/歌单", "features": "解析链接 / 点歌"},
    "qqmusic": {"name": "QQ音乐", "types": "歌曲/歌单", "features": "解析链接 / 点歌"},
    "kugou": {"name": "酷狗音乐", "types": "歌曲", "features": "解析链接 / 点歌"},
    "kuwo": {"name": "酷我音乐", "types": "歌曲", "features": "解析链接 / 点歌"},
    "lofter": {"name": "LOFTER", "types": "文章/图片", "features": "解析链接"},
    "mihuashi": {"name": "米画师", "types": "作品", "features": "解析链接"},
    "cpp": {"name": "CPP", "types": "同人作品", "features": "解析链接"},
    "kujiequ": {"name": "库街区", "types": "帖子/动态", "features": "解析链接 / 订阅"},
    "xiaoheihe": {"name": "小黑盒", "types": "帖子/视频", "features": "解析链接"},
    "nga": {"name": "NGA", "types": "帖子", "features": "解析链接"},
}


class HelpSystem:
    """生成帮助指引"""

    PLATFORM_HELP = PLATFORM_HELP

    @staticmethod
    def build_text_help(platform: str = "", render_mode: str = "HTML") -> str:
        """构建纯文本帮助"""
        if platform:
            info = PLATFORM_HELP.get(platform)
            if not info:
                return f"未知平台: {platform}"
            return (
                f"\U0001f4d6 {info['name']} 解析器\n"
                f"━━━━━━━━━━━━━━\n"
                f"支持类型: {info['types']}\n"
                f"功能: {info['features']}\n"
                f"\n使用方法: 直接发送链接即可自动解析"
            )

        lines = [
            "\U0001f31f 万能解析器 v2.0 使用帮助",
            "━" * 30,
            "",
            "【基础命令】",
            "  /help           - 显示本帮助",
            "  /help <平台>    - 查看特定平台详情",
            "  /开启解析       - 开启当前会话解析",
            "  /关闭解析       - 关闭当前会话解析",
            "",
            "【B站命令】",
            "  bili_sub <UID>  - 订阅UP主动态",
            "  bili_sub_list   - 查看订阅列表",
            "  bili_sub_del    - 删除订阅",
            "  bili_card_style - 切换卡片样式",
            "  bili_login      - B站扫码登录",
            "  bili_logout     - B站登出",
            "  bili_sub_test   - 测试订阅推送",
            "",
            "【点歌命令】",
            "  点歌 <歌名>     - 搜索并播放歌曲",
            "  查歌词 <歌名>   - 搜索歌词",
            "  歌单收藏 <歌名> - 收藏歌曲",
            "  歌单列表        - 查看歌单",
            "  歌单点歌 <序号> - 从歌单播放",
            "  全部点歌 <歌名> - 全平台搜索",
            "  热歌榜          - 查看热门歌曲",
            "",
            "【解析功能】",
            "  直接发送链接即可自动解析",
            f"  支持 {len(PLATFORM_HELP)} 个平台",
            "",
            "【渲染模式】",
            f"  当前: {render_mode}",
            "  可在插件配置中切换 (use_html_render)",
            "",
            "【订阅功能】",
            "  支持多平台博主动态订阅",
            "  配置: interval_secs 控制检测周期",
            "",
            f"\U0001f4a1 发送 /help <平台名> 查看详情",
        ]
        return "\n".join(lines)

    @staticmethod
    async def render_help_image(html_renderer, platform: str = "") -> Optional[str]:
        """渲染图片版帮助（使用B站粉风格）"""
        from .render_html.models import RenderPayload

        if platform:
            info = PLATFORM_HELP.get(platform)
            if not info:
                return None
            text = (
                f"<b>{info['name']}</b><br>"
                f"支持类型: {info['types']}<br>"
                f"功能: {info['features']}"
            )
            payload = RenderPayload(
                name=info['name'],
                title="使用帮助",
                text=text,
                type=platform,
                platform_color="#fb7299",
                card_width="800px",
            )
        else:
            platforms_li = "".join(
                f"<br>• {v['name']}: {v['types']}"
                for v in PLATFORM_HELP.values()
            )
            text = (
                "万能解析器 v2.0<br><br>"
                "<b>命令:</b><br>"
                "/help, /开启解析, /关闭解析<br>"
                "bili_sub, bili_sub_list, bili_sub_del<br>"
                "点歌, 查歌词, 歌单列表, 歌单点歌<br><br>"
                "<b>支持平台:</b>" + platforms_li
            )
            payload = RenderPayload(
                name="万能解析器",
                title="使用帮助",
                text=text,
                type="help",
                platform_color="#fb7299",
                card_width="800px",
            )

        return await html_renderer.render_card(payload, style="help")

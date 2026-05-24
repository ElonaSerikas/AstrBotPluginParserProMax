# main.py - Unified entry point for astrbot_plugin_parser
# Merges: original parser + astrbot_plugin_bilibili + astrbot_plugin_music

import asyncio
import json
import os
import pathlib
import re
import tempfile
import time
import traceback
from typing import List, Tuple

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.core import AstrBotConfig
from astrbot.core.message.components import At, Image, Json
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)
from astrbot.core.star.filter.command import GreedyStr
from astrbot.core.utils.session_waiter import (
    SessionController,
    session_waiter,
)

from bilibili_api import login_v2

# ---- Parser existing core modules ----
from .core.arbiter import ArbiterContext, EmojiLikeArbiter
from .core.clean import CacheCleaner
from .core.config import PluginConfig
from .core.debounce import Debouncer
from .core.download import Downloader
from .core.parsers import BaseParser, BilibiliParser
from .core.render import Renderer
from .core.sender import MessageSender
from .core.help import HelpSystem
from .core.utils import extract_json_url

# ---- B站 subscriber modules (ported from astrbot_plugin_bilibili) ----
from .core.subscriber.bili_client import BiliClient
from .core.subscriber.data_manager import DataManager
from .core.subscriber.listener import DynamicListener
from .core.subscriber.dispatcher import SubscriptionNotificationDispatcher
from .core.subscriber.subscription_service import SubscriptionService

# ---- B站 rendering data models & constants ----
from .core.render_html.models import RenderPayload, SubscriptionRecord
from .core.render_html.constants import (
    CARD_TEMPLATES,
    DEFAULT_TEMPLATE,
    LIVE_ATALL_OPTION,
    RECENT_DYNAMIC_CACHE,
    RECONNECT_SILENT_PADDING_SECS,
    RECONNECT_SILENT_THRESHOLD_SECS,
    VALID_FILTER_TYPES,
    VALID_SUB_OPTIONS,
    get_template_names,
)
from .core.subscriber.utils import create_qrcode, image_to_base64, is_valid_umo
from .core.subscriber.renderer import Renderer as BiliCardRenderer

# ---- B站 LLM tools (ported from astrbot_plugin_bilibili) ----
from .core.tools.bgm_daily import BgmDailyTool
from .core.tools.bgm_subject import (
    BgmAdvancedSubjectSearchTool,
    BgmRecommendHotSubjectsTool,
)
from .core.tools.bili_hot_video import BiliSearchHotVideosTool
from .core.tools.bili_user_dynamics import BiliUserDynamicsTool

# ---- Music modules (ported from astrbot_plugin_music) ----
from .core.music.model import Song
from .core.music.sender import MusicSender
from .core.music.playlist import Playlist
from .core.music.config import PluginConfig as MusicPluginConfig
from .core.music.downloader import Downloader as MusicDownloader
from .core.music.platform import BaseMusicPlayer
from .core.music.renderer import MusicRenderer
from .core.music.utils import parse_user_input


_PLUGIN_ROOT = pathlib.Path(__file__).parent.resolve()
LOGO_PATH = str(_PLUGIN_ROOT / "logo.png")


class ParserPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context

        # ===== Parser subsystems =====
        self.cfg = PluginConfig(config, context=context)
        self._raw_config = config  # direct AstrBotConfig for bilibili config ops
        self.renderer = Renderer(self.cfg)
        self.downloader = Downloader(self.cfg)
        self.debouncer = Debouncer(self.cfg)
        self.arbiter = EmojiLikeArbiter()
        self.sender = MessageSender(self.cfg, self.renderer)
        self.cleaner = CacheCleaner(self.cfg)
        self.parser_map: dict[str, BaseParser] = {}
        self.key_pattern_list: list[tuple[str, re.Pattern[str]]] = []
        self.help_system = HelpSystem()

        # ===== B站 subsystems =====
        self.rai = self._raw_config.get("rai", True)
        # B站 parser config (parse_miniapp moved to template)
        try:
            bili_cfg = self.cfg.parser.bilibili
            self.enable_parse_miniapp = getattr(bili_cfg, "parse_miniapp", True)
        except (AttributeError, KeyError):
            self.enable_parse_miniapp = True
        self.bili_style = self._raw_config.get(
            "renderer_template", DEFAULT_TEMPLATE
        )

        self.proxy = (self._raw_config.get("proxy", "") or "").strip()
        self.bangumi_token = (self._raw_config.get("bangumi_token", "") or "").strip()
        self.bili_data_manager = DataManager(
            recent_dynamic_cache=self._raw_config.get(
                "recent_dynamic_cache", RECENT_DYNAMIC_CACHE
            )
        )
        self.bili_renderer = BiliCardRenderer(
            star_instance=self, rai=self.rai, style=self.bili_style
        )
        self._last_bili_notify_write_ts = (
            self.bili_data_manager.get_last_success_sub_notify_ts()
        )
        self.bili_notification_dispatcher = SubscriptionNotificationDispatcher(
            context=self.context,
            on_sent=self._on_bili_notification_sent,
        )

        saved_credential = self.bili_data_manager.get_credential()
        if saved_credential:
            self.bili_client = BiliClient(
                credential_dict=saved_credential, proxy=self.proxy
            )
        else:
            self.bili_client = BiliClient(
                sessdata=self._raw_config.get("sessdata"), proxy=self.proxy
            )

        self.bili_dynamic_listener = DynamicListener(
            context=self.context,
            data_manager=self.bili_data_manager,
            bili_client=self.bili_client,
            renderer=self.bili_renderer,
            dispatcher=self.bili_notification_dispatcher,
            cfg=self._raw_config,
        )
        self.bili_subscription_service = SubscriptionService(
            data_manager=self.bili_data_manager,
            bili_client=self.bili_client,
            parse_dynamics=self.bili_dynamic_listener._parse_and_filter_dynamics,
        )

        # Register B站 LLM tools
        bili_llm_tools = (
            BgmAdvancedSubjectSearchTool(token=self.bangumi_token),
            BgmRecommendHotSubjectsTool(token=self.bangumi_token),
            BgmDailyTool(token=self.bangumi_token),
            BiliSearchHotVideosTool(bili_client=self.bili_client),
            BiliUserDynamicsTool(
                bili_client=self.bili_client,
                parse_dynamics=self.bili_dynamic_listener._parse_and_filter_dynamics,
            ),
        )
        self.context.add_llm_tools(*bili_llm_tools)

        self._configure_bili_reconnect_silent()
        self._bili_dynamic_listener_task: asyncio.Task | None = None
        self._start_bili_tasks()

        # ===== Music subsystems =====
        self.music_cfg = MusicPluginConfig(config, context)
        self.music_players: list[BaseMusicPlayer] = []
        self.music_keywords: list[str] = []
        self.music_downloader: MusicDownloader | None = None
        self.music_renderer: MusicRenderer | None = None
        self.music_sender: MusicSender | None = None
        self.playlist: Playlist | None = None

    # =====================================================================
    # initialize / terminate
    # =====================================================================

    async def initialize(self):
        """加载、重载插件时触发"""
        # ---- Parser init ----
        await asyncio.to_thread(Renderer.load_resources)
        self._register_parser()

        # ---- Music init ----
        self._register_music_player()
        self.music_downloader = MusicDownloader(self.music_cfg)
        await self.music_downloader.initialize()
        self.music_renderer = MusicRenderer(self, self.music_cfg)
        self.music_sender = MusicSender(
            self.music_cfg, self.music_renderer, self.music_downloader
        )
        self.playlist = Playlist(self.music_cfg)
        await self.playlist.initialize()

    async def terminate(self):
        """插件卸载时触发"""
        # ---- Parser cleanup ----
        await self.downloader.close()
        unique_parsers = set(self.parser_map.values())
        for parser in unique_parsers:
            await parser.close_session()
        await self.cleaner.stop()

        # ---- B站 cleanup ----
        if (
            hasattr(self, "_bili_dynamic_listener_task")
            and self._bili_dynamic_listener_task
            and not self._bili_dynamic_listener_task.done()
        ):
            self._bili_dynamic_listener_task.cancel()
            try:
                await self._bili_dynamic_listener_task
            except asyncio.CancelledError:
                logger.info(
                    "B站 dynamic_listener task cancelled during terminate."
                )
            except Exception as e:
                logger.error(
                    f"Error cancelling B站 dynamic_listener task: {e}"
                )

        # ---- Music cleanup ----
        if self.music_downloader:
            await self.music_downloader.close()
        await MusicRenderer.close_browser()
        if self.music_players:
            for player in self.music_players:
                await player.close()
        if self.playlist:
            await self.playlist.close()

    # =====================================================================
    # Parser helpers
    # =====================================================================

    def _register_parser(self):
        """注册解析器（以 parser.enable 为唯一启用来源）"""
        all_subclass = BaseParser.get_all_subclass()
        enabled_platforms = set(self.cfg.parser.enabled_platforms())

        enabled_classes: list[type[BaseParser]] = []
        enabled_names: list[str] = []
        for cls in all_subclass:
            platform_name = cls.platform.name

            if platform_name not in enabled_platforms:
                logger.debug(f"[parser] 平台未启用或未配置: {platform_name}")
                continue

            enabled_classes.append(cls)
            enabled_names.append(platform_name)

            parser = cls(self.cfg, self.downloader)

            for keyword, _ in cls._key_patterns:
                self.parser_map[keyword] = parser

        logger.debug(f"启用平台: {'、'.join(enabled_names) if enabled_names else '无'}")

        patterns: list[tuple[str, re.Pattern[str]]] = []
        for cls in enabled_classes:
            for kw, pat in cls._key_patterns:
                patterns.append((kw, re.compile(pat) if isinstance(pat, str) else pat))

        patterns.sort(key=lambda x: -len(x[0]))
        self.key_pattern_list = patterns

        logger.debug(
            f"[parser] 关键词-正则对已生成: {[kw for kw, _ in patterns]}"
        )

    def _get_parser_by_type(self, parser_type):
        for parser in self.parser_map.values():
            if isinstance(parser, parser_type):
                return parser
        raise ValueError(f"未找到类型为 {parser_type} 的 parser 实例")

    # =====================================================================
    # B站 helpers
    # =====================================================================

    def _start_bili_tasks(self):
        """启动或重启 B站 后台任务。"""
        if (
            self._bili_dynamic_listener_task
            and not self._bili_dynamic_listener_task.done()
        ):
            self._bili_dynamic_listener_task.cancel()

        self._bili_dynamic_listener_task = asyncio.create_task(
            self.bili_dynamic_listener.start()
        )

    def _compute_bili_reconnect_silent_duration(self) -> int:
        uid_count = len(self.bili_dynamic_listener._build_uid_targets())
        interval_secs = max(float(self._raw_config.get("interval_secs")), 0.0)
        task_gap_secs = max(float(self._raw_config.get("task_gap_secs")), 0.0)
        duration = (
            interval_secs
            + task_gap_secs * uid_count
            + RECONNECT_SILENT_PADDING_SECS
        )
        return max(int(duration), 1)

    def _configure_bili_reconnect_silent(self) -> None:
        if not bool(self._raw_config.get("reconnect_silent", False)):
            self.bili_notification_dispatcher.set_silent_until_ts(0)
            return

        last_success_ts = self.bili_data_manager.get_last_success_sub_notify_ts()
        if last_success_ts <= 0:
            logger.info("B站 重连静默未触发：缺少历史推送成功时间。")
            return

        now_ts = int(time.time())
        idle_secs = now_ts - last_success_ts
        if idle_secs <= RECONNECT_SILENT_THRESHOLD_SECS:
            logger.info(
                f"B站 重连静默未触发：距上次成功推送仅 {idle_secs} 秒"
                f"（阈值 {RECONNECT_SILENT_THRESHOLD_SECS} 秒）。"
            )
            return

        silent_duration = self._compute_bili_reconnect_silent_duration()
        silent_until_ts = now_ts + silent_duration
        self.bili_notification_dispatcher.set_silent_until_ts(silent_until_ts)
        logger.warning(
            f"检测到长时间未成功推送 B站 订阅通知（{idle_secs} 秒），"
            f"进入静默模式 {silent_duration} 秒。"
        )

    async def _on_bili_notification_sent(self, _notification: object) -> None:
        now_ts = int(time.time())
        if now_ts == self._last_bili_notify_write_ts:
            return
        self._last_bili_notify_write_ts = now_ts
        await self.bili_data_manager.set_last_success_sub_notify_ts(now_ts)

    @staticmethod
    def _parse_bili_sub_args(
        input_text: GreedyStr,
    ) -> tuple[List[str], List[str], bool]:
        args = input_text.strip().split(" ") if input_text.strip() else []
        filter_types: List[str] = []
        filter_regex: List[str] = []
        live_atall = False

        for arg in args:
            if arg in VALID_SUB_OPTIONS:
                if arg == LIVE_ATALL_OPTION:
                    live_atall = True
                continue
            if arg in VALID_FILTER_TYPES:
                filter_types.append(arg)
            else:
                filter_regex.append(arg)

        return filter_types, filter_regex, live_atall

    @staticmethod
    def _build_bili_filter_desc(
        filter_types: List[str], filter_regex: List[str], live_atall: bool
    ) -> str:
        filter_desc = ""
        if filter_types:
            filter_desc += f"<br>过滤类型: {', '.join(filter_types)}"
        if filter_regex:
            filter_desc += f"<br>过滤正则: {filter_regex}"
        filter_desc += (
            f"<br>直播开播@全体: {'开启' if live_atall else '关闭'}"
        )
        return filter_desc

    @staticmethod
    def _build_bili_subscription_payload(
        uid: int,
        name: str,
        sex: str,
        avatar: str,
        mid: int,
        filter_desc: str,
    ) -> RenderPayload:
        link = f"https://space.bilibili.com/{mid}"
        return RenderPayload(
            uid=str(uid),
            name="AstrBot",
            avatar=image_to_base64(LOGO_PATH),
            text=f"📣 订阅成功！<br>UP 主: {name} | 性别: {sex}{filter_desc}",
            image_urls=[avatar] if avatar else [],
            url=link,
            qrcode=create_qrcode(link),
        )

    async def _send_bili_subscription_result(
        self, event: AstrMessageEvent, payload: RenderPayload, avatar: str
    ):
        if self.rai:
            img_path = await self.bili_renderer.render_dynamic(payload)
            if img_path:
                await event.send(
                    event.chain_result([Image.fromFile(img_path)])
                    .message(payload.url)
                )
                return
            msg = "渲染图片失败了 (´;ω;`)"
            text = "\n".join(filter(None, payload.text.split("<br>")))
            chain = f"{msg}\n{text}"
            if avatar:
                await event.send(
                    event.chain_result([Image.fromURL(avatar)])
                    .message(chain)
                )
            else:
                await event.send(event.plain_result(chain))
            return
        lines = [payload.text]
        if avatar:
            lines.append(avatar)
        await event.send(event.plain_result("\n".join(lines)))

    async def _apply_bili_subscription(
        self,
        sub_user: str,
        uid_int: int,
        filter_types: List[str],
        filter_regex: List[str],
        live_atall: bool,
    ) -> Tuple[bool, str]:
        result = await self.bili_subscription_service.add_or_update(
            sub_user, uid_int, filter_types, filter_regex, live_atall
        )
        if result.updated:
            option_desc = "开启" if live_atall else "关闭"
            return True, f"该动态已订阅，已更新过滤条件。直播@全体: {option_desc}"
        return False, ""

    # =====================================================================
    # Music helpers
    # =====================================================================

    def _register_music_player(self):
        """注册音乐播放器"""
        all_subclass = BaseMusicPlayer.get_all_subclass()
        for _cls in all_subclass:
            player = _cls(self.music_cfg)
            self.music_players.append(player)
            self.music_keywords.extend(player.platform.keywords)
        logger.debug(f"已注册音乐触发词：{self.music_keywords}")

    def get_music_player(
        self,
        name: str | None = None,
        word: str | None = None,
        default: bool = False,
    ) -> BaseMusicPlayer | None:
        if default:
            word = self.music_cfg.default_player_name
        for player in self.music_players:
            if name:
                name_ = name.strip().lower()
                p = player.platform
                if p.display_name.lower() == name_ or p.name.lower() == name_:
                    return player
            elif word:
                word_ = word.strip().lower()
                for keyword in player.platform.keywords:
                    if keyword.lower() in word_:
                        return player
        return None

    # =====================================================================
    # Event handlers (ALL)
    # =====================================================================

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """消息的统一入口 — 解析各类平台链接"""
        umo = event.unified_msg_origin

        if self.cfg.whitelist and umo not in self.cfg.whitelist:
            return
        if self.cfg.blacklist and umo in self.cfg.blacklist:
            return

        chain = event.get_messages()
        if not chain:
            return

        seg1 = chain[0]
        text = event.message_str

        if isinstance(seg1, Json):
            text = extract_json_url(seg1.data)
            logger.debug(f"解析Json组件: {text}")

        if not text:
            return

        self_id = event.get_self_id()

        if isinstance(seg1, At) and str(seg1.qq) != self_id:
            return

        keyword: str = ""
        searched: re.Match[str] | None = None
        for kw, pat in self.key_pattern_list:
            if kw not in text:
                continue
            if m := pat.search(text):
                keyword, searched = kw, m
                break
        if searched is None:
            return
        logger.debug(f"匹配结果: {keyword}, {searched}")

        if isinstance(event, AiocqhttpMessageEvent) and not event.is_private_chat():
            raw = event.message_obj.raw_message
            if not isinstance(raw, dict):
                logger.warning(f"Unexpected raw_message type: {type(raw)}")
                return
            is_win = await self.arbiter.compete(
                bot=event.bot,
                ctx=ArbiterContext(
                    message_id=int(raw["message_id"]),
                    msg_time=int(raw["time"]),
                    self_id=int(raw["self_id"]),
                ),
            )
            if not is_win:
                logger.debug("Bot在仲裁中输了, 跳过解析")
                return
            logger.debug("Bot在仲裁中胜出, 准备解析...")
            logger.debug("已发送表情回应")

        link = searched.group(0)
        if self.debouncer.hit_link(umo, link):
            logger.warning(
                f"[链接防抖] 链接 {link} 在防抖时间内，跳过解析"
            )
            return

        # Send processing indicator for private chats
        if isinstance(event, AiocqhttpMessageEvent) and event.is_private_chat():
            await event.send(event.plain_result("🔍 正在解析..."))

        parse_res = await self.parser_map[keyword].parse(keyword, searched)

        resource_id = parse_res.get_resource_id()
        if self.debouncer.hit_resource(umo, resource_id):
            logger.warning(
                f"[资源防抖] 资源 {resource_id} 在防抖时间内，跳过发送"
            )
            return

        await self.sender.send_parse_result(event, parse_res)

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_search_song(self, event: AstrMessageEvent):
        """监听点歌命令"""
        if not event.is_at_or_wake_command:
            return
        cmd, _, arg = event.message_str.partition(" ")
        if not arg:
            return
        player = self.get_music_player(word=cmd)
        if "点歌" == cmd:
            player = self.get_music_player(default=True)
        if not player:
            return
        args = arg.split()
        index: int = int(args[-1]) if args[-1].isdigit() else 0
        song_name = arg.removesuffix(str(index))
        if not song_name:
            yield event.plain_result("未指定歌名")
            return

        logger.debug(
            f"正在通过{player.platform.display_name}搜索歌曲：{song_name}"
        )
        songs = await player.fetch_songs(
            keyword=song_name,
            limit=self.music_cfg.real_song_limit,
            extra=cmd,
        )
        if not songs:
            yield event.plain_result(f"搜索【{song_name}】无结果")
            return

        if len(songs) == 1:
            index = 1

        if index and index <= len(songs):
            selected_song = songs[int(index) - 1]
            await self.music_sender.send_song(event, player, selected_song)

        else:
            title = f"【{player.platform.display_name}】"
            asyncio.create_task(
                self.music_sender.send_song_selection(
                    event=event,
                    songs=songs,
                    title=title,
                    platform_name=player.platform.name,
                )
            )

            yield event.plain_result(
                f"找到 {len(songs)} 首，回复数字选歌：\n"
                f"1 → 第1首 | 2+语音 → 第2首语音模式\n"
                f"也可输入「点歌 <歌名>」搜其他"
            )

            @session_waiter(timeout=self.music_cfg.timeout)
            async def empty_mention_waiter(
                controller: SessionController, event: AstrMessageEvent
            ):
                arg = event.message_str.strip()
                arg_lower = arg.lower()
                for kw in self.music_keywords:
                    if kw in arg_lower:
                        parts = arg.split()
                        idx = (
                            int(parts[-1])
                            if parts and parts[-1].isdigit()
                            else 0
                        )
                        if idx and 1 <= idx <= len(songs):
                            selected_song = songs[idx - 1]
                            await self.music_sender.send_song(
                                event, player, selected_song
                            )
                        else:
                            await event.send(
                                event.plain_result("请输入有效序号")
                            )
                        controller.stop()
                        return
                index, modes, error = parse_user_input(arg)
                if error:
                    await event.send(event.plain_result(error))
                    return
                if index == 0:
                    return
                if index < 1 or index > len(songs):
                    controller.stop()
                    return
                selected_song = songs[index - 1]
                await self.music_sender.send_song(
                    event, player, selected_song, modes=modes
                )
                controller.stop()

            try:
                await empty_mention_waiter(event)
            except TimeoutError:
                yield event.plain_result("点歌超时！")
            except Exception as e:
                logger.error(traceback.format_exc())
                logger.error("点歌发生错误" + str(e))

        event.stop_event()

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def parse_miniapp(self, event: AstrMessageEvent):
        """解析QQ小程序JSON中的B站链接"""
        if not self.enable_parse_miniapp:
            return
        for msg_element in event.message_obj.message:
            if (
                hasattr(msg_element, "type")
                and msg_element.type == "Json"
                and hasattr(msg_element, "data")
            ):
                json_string = msg_element.data

                try:
                    if isinstance(json_string, dict):
                        parsed_data = json_string
                    else:
                        parsed_data = json.loads(json_string)
                    meta = parsed_data.get("meta", {})
                    detail_1 = meta.get("detail_1", {})
                    title = detail_1.get("title")
                    qqdocurl = detail_1.get("qqdocurl")
                    desc = detail_1.get("desc")

                    if title == "哔哩哔哩" and qqdocurl:
                        if "https://b23.tv" in qqdocurl:
                            qqdocurl = await self.bili_client.b23_to_bv(
                                qqdocurl
                            )
                        ret = f"标题: {desc}\n链接: {qqdocurl}"
                        await event.send(event.plain_result(ret))

                    news = meta.get("news", {})
                    tag = news.get("tag", "")
                    jumpurl = news.get("jumpUrl", "")
                    news_title = news.get("title", "")
                    if tag == "哔哩哔哩" and jumpurl:
                        if "https://b23.tv" in jumpurl:
                            jumpurl = await self.bili_client.b23_to_bv(
                                jumpurl
                            )
                        ret = f"标题: {news_title}\n链接: {jumpurl}"
                        await event.send(event.plain_result(ret))
                except json.JSONDecodeError:
                    logger.error(
                        f"Failed to decode JSON string: {json_string}"
                    )
                except Exception as e:
                    logger.error(
                        f"An error occurred during JSON processing: {e}"
                    )

    # =====================================================================
    # Help commands
    # =====================================================================

    @filter.command("help")
    async def help_command(self, event: AstrMessageEvent, platform: str = ""):
        """显示使用帮助"""
        render_mode = "HTML" if self.cfg.use_html_render else "PIL/文本"
        html_renderer = getattr(self.renderer, "html_renderer", None)

        if self.cfg.use_html_render and html_renderer:
            img_path = await self.help_system.render_help_image(
                html_renderer, platform
            )
            if img_path:
                yield event.chain_result([Image.fromFileSystem(img_path)])
                return

        text = self.help_system.build_text_help(platform, render_mode)
        yield event.plain_result(text)

    @filter.command("帮助")
    async def help_command_cn(self, event: AstrMessageEvent, platform: str = ""):
        """显示使用帮助"""
        render_mode = "HTML" if self.cfg.use_html_render else "PIL/文本"
        html_renderer = getattr(self.renderer, "html_renderer", None)

        if self.cfg.use_html_render and html_renderer:
            img_path = await self.help_system.render_help_image(
                html_renderer, platform
            )
            if img_path:
                yield event.chain_result([Image.fromFileSystem(img_path)])
                return

        text = self.help_system.build_text_help(platform, render_mode)
        yield event.plain_result(text)

    # =====================================================================
    # Parser commands
    # =====================================================================

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("开启解析")
    async def open_parser(self, event: AstrMessageEvent):
        """开启当前会话的解析"""
        umo = event.unified_msg_origin
        self.cfg.remove_blacklist(umo)
        yield event.plain_result("当前会话的解析已开启")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("关闭解析")
    async def close_parser(self, event: AstrMessageEvent):
        """关闭当前会话的解析"""
        umo = event.unified_msg_origin
        self.cfg.add_blacklist(umo)
        yield event.plain_result("当前会话的解析已关闭")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("登录B站", alias={"blogin", "登录b站"})
    async def login_bilibili(self, event: AstrMessageEvent):
        """扫码登录B站 — 使用 BilibiliParser 的 QR 码登录"""
        parser: BilibiliParser = self._get_parser_by_type(BilibiliParser)
        qrcode = await parser.login.login_with_qrcode()
        yield event.chain_result([Image.fromBytes(qrcode)])
        async for msg in parser.login.check_qr_state():
            yield event.plain_result(msg)

    # =====================================================================
    # B站 commands
    # =====================================================================

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili_login")
    async def bili_login(self, event: AstrMessageEvent):
        """扫码登录 Bilibili — 使用 bilibili_api 标准登录（支持凭据持久化）"""
        if event.get_group_id():
            yield event.plain_result(
                "仅支持管理员在私聊中使用 '/bili_login' 指令。"
            )
            return

        login_obj = login_v2.QrCodeLogin()
        await login_obj.generate_qrcode()

        qr_path = os.path.join(tempfile.gettempdir(), "qrcode.png")

        yield event.chain_result(
            [Image.fromFile(qr_path)]
        ).message("请使用 Bilibili App 扫描下方二维码登录：")

        try:
            while True:
                state = await login_obj.check_state()
                if state == login_v2.QrCodeLoginEvents.DONE:
                    credential = login_obj.get_credential()
                    self.bili_client.credential = credential
                    cred_dict = self.bili_client.get_credential_dict()
                    if cred_dict is not None:
                        await self.bili_data_manager.set_credential(
                            cred_dict
                        )
                        self._start_bili_tasks()
                        yield event.plain_result("✅ 登录成功！")
                    else:
                        yield event.plain_result(
                            "❌ 登录失败：无法获取凭据。"
                        )
                    break
                elif state == login_v2.QrCodeLoginEvents.TIMEOUT:
                    yield event.plain_result(
                        "❌ 登录超时，请重新执行 /bili_login。"
                    )
                    break

                await asyncio.sleep(2)
        except Exception as e:
            logger.error(f"登录过程中发生错误: {e}")
            yield event.plain_result(f"❌ 登录失败: {str(e)}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili_logout")
    async def bili_logout(self, event: AstrMessageEvent):
        """登出 Bilibili，清除凭据。"""
        self.bili_client.credential = None
        await self.bili_data_manager.clear_credential()
        self.bili_client = BiliClient(
            sessdata=self._raw_config.get("sessdata"), proxy=self.proxy
        )
        self.bili_dynamic_listener.bili_client = self.bili_client
        self._start_bili_tasks()
        yield event.plain_result("✅ 已登出 Bilibili，凭据已清除。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili_card_style", alias={"卡片样式"})
    async def switch_bili_style(
        self, event: AstrMessageEvent, style: str | None = None
    ):
        """切换 B站 动态卡片样式。不带参数查看可用样式列表。"""
        available = get_template_names()

        if not style:
            lines = ["📋 可用的卡片样式："]
            for tid in available:
                info = CARD_TEMPLATES[tid]
                current = " ← 当前" if tid == self.bili_style else ""
                lines.append(f"  • {tid}: {info['name']}{current}")
                lines.append(f"    {info['description']}")
            lines.append("\n使用 /卡片样式 <样式名> 切换")
            yield event.plain_result("\n".join(lines))
            return

        if style not in available:
            yield event.plain_result(
                f"样式 '{style}' 不存在。可用样式：{', '.join(available)}"
            )
            return

        self.bili_style = style
        self.bili_renderer.style = style
        self._raw_config["renderer_template"] = style
        self._raw_config.save_config()

        info = CARD_TEMPLATES[style]
        yield event.plain_result(
            f"✅ 已切换样式为：{info['name']} ({style})"
        )

    @filter.command("bili_sub", alias={"订阅动态"})
    async def bili_dynamic_sub(
        self, event: AstrMessageEvent, uid: str, input: GreedyStr
    ):
        """订阅 UP 主动态"""
        filter_types, filter_regex, live_atall = self._parse_bili_sub_args(
            input
        )

        sub_user = event.unified_msg_origin
        if not uid.isdigit():
            yield event.plain_result("UID 格式错误")
            return
        uid_int = int(uid)

        updated, update_msg = await self._apply_bili_subscription(
            sub_user, uid_int, filter_types, filter_regex, live_atall
        )
        if updated:
            yield event.plain_result(update_msg)
            return

        try:
            usr_info, msg = await self.bili_client.get_user_info(int(uid))
        except Exception as e:
            logger.error(f"获取用户信息失败: {e}")
            yield event.plain_result(
                "订阅成功，但获取 UP 主信息失败。"
            )
            return
        if not usr_info:
            yield event.plain_result(
                f"订阅成功，但获取 UP 主信息失败: {msg}"
            )
            return

        filter_desc = self._build_bili_filter_desc(
            filter_types, filter_regex, live_atall
        )
        payload = self._build_bili_subscription_payload(
            uid_int,
            str(usr_info.get("name", "Unknown")),
            str(usr_info.get("sex", "保密")),
            str(usr_info.get("face", "")),
            int(usr_info.get("mid", uid_int)),
            filter_desc,
        )
        await self._send_bili_subscription_result(
            event, payload, str(usr_info.get("face", ""))
        )

    @filter.command("bili_sub_list", alias={"订阅列表"})
    async def bili_sub_list(self, event: AstrMessageEvent):
        """查看 B站 动态监控列表"""
        sub_user = event.unified_msg_origin
        ret = "订阅列表：\n"
        subs = self.bili_data_manager.get_subscriptions_by_user(sub_user)

        if not subs:
            yield event.plain_result("无订阅")
            return

        for idx, uid_sub_data in enumerate(subs):
            uid = uid_sub_data.uid
            info, _ = await self.bili_client.get_user_info(int(uid))
            if not info:
                ret += f"{idx + 1}. {uid} - 无法获取 UP 主信息\n"
            else:
                name = info["name"]
                ret += f"{idx + 1}. {uid} - {name}\n"
            filters = []
            if uid_sub_data.filter_types:
                filters.append(
                    f"过滤类型: {', '.join(uid_sub_data.filter_types)}"
                )
            if uid_sub_data.filter_regex:
                filters.append(
                    f"过滤正则: {uid_sub_data.filter_regex}"
                )
            if uid_sub_data.live_atall:
                filters.append("直播@全体: live_atall")
            if filters:
                ret += f"   {'｜'.join(filters)}\n"
        yield event.plain_result(ret)

    @filter.command("bili_sub_del", alias={"订阅删除"})
    async def bili_sub_del(self, event: AstrMessageEvent, uid: str):
        """删除 B站 动态监控"""
        sub_user = event.unified_msg_origin
        if not uid or not uid.isdigit():
            yield event.plain_result(
                "参数错误，请提供正确的UID。"
            )
            return

        uid2del = int(uid)

        if await self.bili_data_manager.remove_subscription(
            sub_user, uid2del
        ):
            yield event.plain_result("删除成功")
        else:
            yield event.plain_result("未找到指定的订阅")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili_global_del", alias={"全局删除"})
    async def bili_global_sub_del(
        self, event: AstrMessageEvent, umo: str = ""
    ):
        """管理员指令。通过 UMO 删除某一个群聊或者私聊的所有订阅。"""
        if not is_valid_umo(umo):
            yield event.plain_result(
                "通过 UMO 删除某一个群聊或者私聊的所有订阅。"
                "使用 /sid 指令查看当前会话的 UMO 或参考 WebUI-自定义规则。"
            )
            return

        msg = await self.bili_data_manager.remove_all_for_user(umo)
        yield event.plain_result(msg)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili_global_sub", alias={"全局订阅"})
    async def bili_global_sub_add(
        self,
        event: AstrMessageEvent,
        umo: str,
        uid: str,
        input: GreedyStr,
    ):
        """管理员指令。通过 UMO + UID 添加订阅。"""
        if not is_valid_umo(umo) or not uid.isdigit():
            yield event.plain_result(
                "请提供正确的UMO与UID。"
                "使用 /sid 指令查看当前会话的 UMO 或参考 WebUI-自定义规则。"
            )
            return
        filter_types, filter_regex, live_atall = self._parse_bili_sub_args(
            input
        )
        uid_int = int(uid)

        updated, update_msg = await self._apply_bili_subscription(
            umo, uid_int, filter_types, filter_regex, live_atall
        )
        if updated:
            yield event.plain_result(update_msg)
            return
        yield event.plain_result(
            f"订阅完成，已为{umo}添加订阅{uid_int}，详情见日志。"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili_global_list", alias={"全局列表"})
    async def bili_global_list(self, event: AstrMessageEvent):
        """管理员指令。查看所有订阅者"""
        ret = "订阅会话列表：\n"
        all_subs = self.bili_data_manager.get_all_subscriptions()
        if not all_subs:
            yield event.plain_result("没有任何会话订阅过。")
            return

        for sub_user in all_subs:
            ret += f"- {sub_user}\n"
            for sub in all_subs[sub_user]:
                uid = sub.uid
                ret += f"  - {uid}\n"
        yield event.plain_result(ret)

    @filter.command("bili_sub_test", alias={"订阅测试"})
    async def bili_sub_test(self, event: AstrMessageEvent, uid: str):
        """测试 B站 订阅功能。"""
        sub_user = event.unified_msg_origin
        try:
            uid_int = int(uid)
        except (TypeError, ValueError):
            yield event.plain_result("UID 必须是数字。")
            return

        dyn = await self.bili_client.get_latest_dynamics(uid_int)
        if not dyn:
            yield event.plain_result(
                "未获取到动态数据，请稍后重试。"
            )
            return

        result_list = self.bili_dynamic_listener._parse_and_filter_dynamics(
            dyn,
            SubscriptionRecord(uid=uid_int),
        )

        render_data: RenderPayload | None = None
        for result in result_list or []:
            if result.has_payload():
                render_data = result.payload
                break

        if not render_data:
            yield event.plain_result(
                "没有可用于测试推送的动态"
                "（可能没有新动态、都被过滤掉，或动态类型暂不支持）。"
            )
            return

        await self.bili_dynamic_listener._handle_new_dynamic(
            sub_user, render_data, None
        )
        event.stop_event()

    # =====================================================================
    # Music commands
    # =====================================================================

    @filter.command("查歌词")
    async def query_lyrics(
        self, event: AstrMessageEvent, song_name: str
    ):
        """查歌词 <搜索词>"""
        player = self.get_music_player(default=True)
        if not player:
            yield event.plain_result("无可用播放器")
            return
        songs = await player.fetch_songs(keyword=song_name, limit=1)
        if not songs:
            yield event.plain_result("没找到相关歌曲")
            return
        await self.music_sender.send_lyrics(event, player, songs[0])

    @filter.command("歌单收藏")
    async def collect_song(
        self, event: AstrMessageEvent, song_name: str
    ):
        """歌单收藏 <歌名>"""
        user_id = event.get_sender_id()
        player = self.get_music_player(default=True)
        if not player:
            yield event.plain_result("无可用播放器")
            return

        songs = await player.fetch_songs(keyword=song_name, limit=1)
        if not songs:
            yield event.plain_result(f"搜索【{song_name}】无结果")
            return

        song = songs[0]
        platform = player.platform.name

        success = await self.playlist.add_song(user_id, song, platform)
        if success:
            yield event.plain_result(
                f"已收藏【{song.name}_{song.artists}】"
            )
        else:
            yield event.plain_result(
                f"【{song.name}】已在你的歌单中"
            )

    @filter.command("歌单取藏")
    async def uncollect_song(
        self, event: AstrMessageEvent, song_name: str
    ):
        """歌单取藏 <歌名>"""
        user_id = event.get_sender_id()
        player = self.get_music_player(default=True)
        if not player:
            yield event.plain_result("无可用播放器")
            return

        songs = await player.fetch_songs(keyword=song_name, limit=1)
        if not songs:
            yield event.plain_result(f"搜索【{song_name}】无结果")
            return

        song = songs[0]
        platform = player.platform.name

        success = await self.playlist.remove_song(
            user_id, song.id, platform
        )
        if success:
            yield event.plain_result(
                f"已取消收藏【{song.name}_{song.artists}】"
            )
        else:
            yield event.plain_result(
                f"【{song.name}】不在你的歌单中"
            )

    @filter.command("歌单列表")
    async def view_playlist(self, event: AstrMessageEvent):
        """查看歌单"""
        user_id = event.get_sender_id()
        user_name = event.get_sender_name()

        if await self.playlist.is_empty(user_id):
            yield event.plain_result(
                "你的歌单是空的，使用「收藏 <歌名>」来添加歌曲"
            )
            return

        songs_with_platform = await self.playlist.get_songs(user_id)
        if not songs_with_platform:
            yield event.plain_result("获取歌单失败")
            return

        playlist_text = f"【{user_name}的歌单】\n"
        for i, (song, platform) in enumerate(songs_with_platform, 1):
            playlist_text += f"{i}. {song.name} - {song.artists}\n"

        yield event.plain_result(playlist_text.strip())

    @filter.command("歌单点歌")
    async def play_from_playlist(
        self, event: AstrMessageEvent, index: str
    ):
        """歌单点歌 <序号>"""
        user_id = event.get_sender_id()

        if not index.isdigit():
            yield event.plain_result("请输入有效的序号")
            return

        idx = int(index)
        if idx < 1:
            yield event.plain_result("序号必须大于0")
            return

        songs_with_platform = await self.playlist.get_songs(user_id)
        if not songs_with_platform:
            yield event.plain_result("你的歌单是空的")
            return

        if idx > len(songs_with_platform):
            yield event.plain_result(
                f"序号超出范围，你的歌单只有{len(songs_with_platform)}首歌"
            )
            return

        song, platform_name = songs_with_platform[idx - 1]

        player = self.get_music_player(name=platform_name)
        if not player:
            player = self.get_music_player(default=True)

        if not player:
            yield event.plain_result("无可用播放器")
            return

        await self.music_sender.send_song(event, player, song)

    @filter.command("全部点歌")
    async def on_multi_search(self, event: AstrMessageEvent):
        """全部点歌 <歌名> - 全平台同步搜索"""
        cmd, _, arg = event.message_str.partition(" ")
        if not arg:
            yield event.plain_result("用法：全部点歌 <歌名>")
            return

        song_name = arg.strip()
        tasks = [
            player.fetch_songs(keyword=song_name, limit=2)
            for player in self.music_players
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_songs_with_platform = []
        for player, songs in zip(self.music_players, results):
            if isinstance(songs, list) and songs:
                for s in songs[:2]:
                    all_songs_with_platform.append(
                        (s, player.platform.name)
                    )

        if not all_songs_with_platform:
            yield event.plain_result(
                f"全平台搜索【{song_name}】均无结果"
            )
            return

        await self.music_sender.send_song_selection(
            event,
            all_songs_with_platform,
            "全平台搜索结果",
            platform_name=all_songs_with_platform[0][1],
        )
        event.stop_event()

    @filter.command("热歌榜")
    async def show_hot_songs(self, event: AstrMessageEvent):
        """查看热门歌曲"""
        player = self.get_music_player(default=True)
        if not player:
            yield event.plain_result("无可用播放器")
            return
        songs = await player.fetch_hot_songs(limit=10)
        if not songs:
            yield event.plain_result("获取热歌榜失败")
            return
        await self.music_sender.send_song_selection(
            event,
            songs,
            "热门歌曲",
            platform_name=player.platform.name,
        )

    # =====================================================================
    # LLM tool
    # =====================================================================

    @filter.llm_tool()
    async def play_song_by_name(
        self, event: AstrMessageEvent, song_name: str
    ):
        """
        当用户想听歌时，根据歌名（可含歌手）搜索并播放音乐。
        Args:
            song_name(string): 歌曲名称或包含歌手的关键词
        """
        player = self.get_music_player(default=True)
        if not player:
            return "无可用播放器"
        songs = await player.fetch_songs(keyword=song_name, limit=1)
        if not songs:
            return "没找到相关歌曲"
        await self.music_sender.send_song(event, player, songs[0])

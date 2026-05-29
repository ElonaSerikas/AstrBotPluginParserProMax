from itertools import chain
from pathlib import Path

from astrbot.api import logger
from astrbot.core.message.components import (
    BaseMessageComponent,
    File,
    Image,
    Node,
    Nodes,
    Plain,
    Record,
    Video,
)
from astrbot.core.platform.astr_message_event import AstrMessageEvent

from .config import PluginConfig
from .data import (
    AudioContent,
    DynamicContent,
    FileContent,
    GraphicsContent,
    ImageContent,
    ParseResult,
    SendGroup,
    TextContent,
    VideoContent,
)
from .exception import (
    DownloadException,
    DownloadLimitException,
    SizeLimitException,
    ZeroSizeException,
)
from .render import Renderer


class MessageSender:
    """
    消息发送器

    职责：
    - 根据解析结果（ParseResult）规划发送策略
    - 控制是否渲染卡片、是否强制合并转发
    - 将不同类型的内容转换为 AstrBot 消息组件并发送

    重要原则：
    - 不在此处做解析
    - 不在此处决定"内容是什么"
    - 只负责"怎么发"
    """

    def __init__(self, config: PluginConfig, renderer: Renderer):
        self.cfg = config
        self.renderer = renderer

    def _to_file_uri(self, path: Path) -> str:
        if not path.is_absolute():
            path = path.resolve()
        posix_path = path.as_posix()
        if posix_path.startswith("/"):
            return f"file:////{posix_path.lstrip('/')}"
        return path.as_uri()

    @staticmethod
    def _iter_contents(result: ParseResult):
        return chain(result.contents, result.repost.contents if result.repost else ())

    def _build_send_plan(
        self,
        result: ParseResult,
        contents: list | tuple | None = None,
        *,
        force_merge_override: bool | None = None,
        render_card_override: bool | None = None,
    ) -> dict:
        """
        根据解析结果生成发送计划（plan）

        plan 只做"策略决策"，不做任何 IO 或发送动作。
        后续发送流程严格按 plan 执行，避免逻辑分散。
        """
        light, heavy = [], []

        # 合并主内容 + 转发内容，统一参与发送策略计算
        # 过滤掉二维码图片（仅用于卡片渲染，不单独发送）
        iterable = contents if contents is not None else self._iter_contents(result)
        for cont in iterable:
            if isinstance(cont, ImageContent) and cont.is_qr:
                continue
            match cont:
                case ImageContent() | GraphicsContent() | TextContent():
                    light.append(cont)
                case VideoContent() | AudioContent() | FileContent() | DynamicContent():
                    heavy.append(cont)
                case _:
                    light.append(cont)

        # 有任意内容（图片/视频/文字等）时均允许渲染卡片
        has_content = bool(light) or bool(heavy) or bool(result.text) or bool(result.title)
        render_card = has_content and self.cfg.single_heavy_render_card
        if render_card_override is not None:
            render_card = render_card_override
        # 实际消息段数量（卡片也算一个段）
        seg_count = len(light) + len(heavy) + (1 if render_card else 0)

        # 达到阈值后，强制合并转发，避免刷屏
        force_merge = seg_count >= self.cfg.forward_threshold
        if force_merge_override is not None:
            force_merge = force_merge_override

        # Cards are always rendered with all content data.
        # If there are 3+ images (light media), forward them separately.
        # Text-only messages should never be forwarded.
        if render_card:
            force_merge = len(light) >= 3
        elif contents is not None and all(
            isinstance(c, TextContent) for c in contents
        ):
            force_merge = False

        return {
            "light": light,
            "heavy": heavy,
            "render_card": render_card,
            # 卡片始终单独发送（不在合并转发中内联）
            "preview_card": render_card,
            "force_merge": force_merge,
        }

    # self_id → 显示名映射（从 angel_heart per_bot_configs 同步）
    _BOT_NAME_MAP: dict[str, str] = {
        "3958874605": "守岸人",
        "3670290043": "薇尔莉特",
        "3828485060": "八千代",
    }

    @staticmethod
    def _resolve_bot_name(event: AstrMessageEvent) -> str:
        """从 event.self_id 解析 Bot 显示名，未匹配返回空字符串"""
        try:
            self_id = str(event.get_self_id())
            return MessageSender._BOT_NAME_MAP.get(self_id, "")
        except Exception:
            return ""

    @staticmethod
    def _build_header_prefix(result: ParseResult, bot_name: str = "") -> str:
        """构建解析结果的前缀消息，格式: 「Bot名·平台解析」标题 — @作者"""
        platform_name = result.platform.display_name if result.platform else "链接"
        if bot_name:
            parts = [f"「{bot_name}·{platform_name}解析」"]
        else:
            parts = [f"「{platform_name}解析」"]
        if result.title:
            parts.append(result.title)
        if result.author and result.author.name:
            parts.append(f"— @{result.author.name}")
        return " ".join(parts) if len(parts) > 1 else parts[0]

    async def _send_preview_card(
        self,
        event: AstrMessageEvent,
        result: ParseResult,
        plan: dict,
    ):
        """
        发送预览卡片（附带文字信息，减少消息段数避免 NapCat 超时）
        """
        if not plan["preview_card"]:
            return

        if image_path := await self.renderer.render_card(result):
            try:
                # 将前缀与卡片图片合并为一条消息，减少发送次数
                bot_name = self._resolve_bot_name(event)
                prefix = self._build_header_prefix(result, bot_name=bot_name)
                parts = [Plain(prefix), Image(self._to_file_uri(image_path))]
                await event.send(event.chain_result(parts))
            finally:
                # 清理渲染产生的临时文件
                try:
                    image_path.unlink(missing_ok=True)
                except OSError:
                    pass

    async def _build_segments(
        self,
        result: ParseResult,
        plan: dict,
    ) -> list[BaseMessageComponent]:
        """
        根据发送计划构建消息段列表

        这里负责：
        - 下载媒体
        - 转换为 AstrBot 消息组件
        - 合并连续文本段，控制总段数 ≤ 3
        """
        segs: list[BaseMessageComponent] = []
        text_parts: list[str] = []  # 暂存文本，最后合并

        # 轻媒体处理
        for cont in plan["light"]:
            if isinstance(cont, TextContent):
                if cont.text:
                    text_parts.append(cont.text)
                continue

            try:
                path: Path = await cont.get_path()
            except DownloadLimitException:
                continue
            except (DownloadException, ZeroSizeException):
                if self.cfg.show_download_fail_tip:
                    text_parts.append("此项媒体下载失败")
                continue

            match cont:
                case ImageContent():
                    segs.append(Image(self._to_file_uri(path)))
                case GraphicsContent() as g:
                    segs.append(Image(self._to_file_uri(path)))
                    if g.text:
                        text_parts.append(g.text)
                    if g.alt:
                        text_parts.append(g.alt)

        # 重媒体处理
        for cont in plan["heavy"]:
            try:
                path: Path = await cont.get_path()
            except SizeLimitException:
                text_parts.append("此项媒体超过大小限制")
                continue
            except DownloadException:
                if self.cfg.show_download_fail_tip:
                    text_parts.append("此项媒体下载失败")
                continue

            match cont:
                case VideoContent() | DynamicContent():
                    segs.append(Video(self._to_file_uri(path)))
                case AudioContent():
                    segs.append(
                        File(name=path.name, file=self._to_file_uri(path))
                        if self.cfg.audio_to_file
                        else Record(self._to_file_uri(path))
                    )
                case FileContent():
                    segs.append(File(name=path.name, file=self._to_file_uri(path)))

        # 将所有文本合并为单条 Plain（减少分段）
        if text_parts:
            merged_text = "\n".join(text_parts)
            if merged_text.strip():
                segs.insert(0, Plain(merged_text))

        return segs

    def _merge_segments_if_needed(
        self,
        event: AstrMessageEvent,
        segs: list[BaseMessageComponent],
        force_merge: bool,
    ) -> list[BaseMessageComponent]:
        """
        根据策略决定是否将消息段合并为转发节点

        合并后的消息结构：
        - 每个原始消息段成为一个 Node
        - 统一使用机器人自身身份
        """
        if not force_merge or not segs:
            return segs

        nodes = Nodes([])
        self_id = event.get_self_id()

        for seg in segs:
            nodes.nodes.append(Node(uin=self_id, name="解析器", content=[seg]))

        return [nodes]

    async def _build_text_fallback(self, result: ParseResult) -> list[BaseMessageComponent]:
        lines: list[str] = []
        if result.header:
            lines.append(result.header)
        if result.text:
            lines.append(result.text)
        elif result.extra.get("info"):
            lines.append(str(result.extra["info"]))

        # 补充发布时间和来源链接
        if result.timestamp:
            from datetime import datetime, timezone, timedelta
            try:
                dt = datetime.fromtimestamp(result.timestamp, tz=timezone(timedelta(hours=8)))
                lines.append(f"发布时间: {dt.strftime('%Y-%m-%d %H:%M')}")
            except Exception:
                pass
        if result.url:
            lines.append(f"来源: {result.url}")

        segs: list[BaseMessageComponent] = []
        text = "\n".join(line for line in lines if line).strip()
        if text:
            segs.append(Plain(text))

        # Also include cover/images when falling back to text mode
        for cont in chain(
            result.contents, result.repost.contents if result.repost else ()
        ):
            if isinstance(cont, ImageContent) and cont.is_qr:
                continue
            if isinstance(cont, ImageContent):
                try:
                    path = await cont.get_path()
                    segs.append(Image(self._to_file_uri(path)))
                except Exception:
                    pass
            elif isinstance(cont, VideoContent):
                try:
                    cover = await cont.get_cover_path()
                    if cover:
                        segs.append(Image(self._to_file_uri(cover)))
                except Exception:
                    pass

        return segs

    def _resolve_groups(self, result: ParseResult) -> list[SendGroup]:
        if result.send_groups:
            return result.send_groups
        return [SendGroup(contents=list(MessageSender._iter_contents(result)))]

    async def _send_group(
        self,
        event: AstrMessageEvent,
        result: ParseResult,
        group: SendGroup,
    ) -> bool:
        plan = self._build_send_plan(
            result,
            group.contents,
            force_merge_override=group.force_merge,
            render_card_override=group.render_card,
        )

        # 先单独发送渲染卡片（如果有）
        if plan["render_card"]:
            await self._send_preview_card(event, result, plan)

        # 构建消息段（合并转发时不再包含卡片，只包含原始媒体）
        segs = await self._build_segments(result, plan)
        segs = self._merge_segments_if_needed(event, segs, plan["force_merge"])

        if not segs:
            return False

        try:
            await event.send(event.chain_result(segs))
            return True
        except Exception as e:
            seg_meta = self._collect_seg_meta(segs)
            logger.error(f"发送解析结果失败： error={e}, segments={seg_meta}")
            return False

    @staticmethod
    def _collect_seg_meta(segs: list[BaseMessageComponent]) -> list[dict[str, str]]:
        """提取消息段元信息，用于失败日志定位。"""
        meta: list[dict[str, str]] = []

        for seg in segs:
            item = {"type": seg.__class__.__name__}
            for attr in ("file", "path", "url"):
                value = getattr(seg, attr, None)
                if value:
                    item["media"] = str(value)
                    break
            meta.append(item)

        return meta

    async def send_parse_result(
        self,
        event: AstrMessageEvent,
        result: ParseResult,
    ):
        """
        发送解析结果的统一入口

        执行顺序固定：
        1. 构建发送计划
        2. 发送预览卡片（如有）
        3. 构建消息段
        4. 必要时合并转发
        5. 最终发送
        """
        groups = self._resolve_groups(result)

        sent = False
        for group in groups:
            sent = await self._send_group(event, result, group) or sent

        if not sent:
            segs = await self._build_text_fallback(result)
            if not segs:
                logger.warning("发送结果为空，不执行发送")
                return

            try:
                await event.send(event.chain_result(segs))
            except Exception as e:
                seg_meta = self._collect_seg_meta(segs)
                logger.error(f"发送解析结果失败： error={e}, segments={seg_meta}")
            return

"""Generic dynamic listener - polls non-Bilibili platforms for subscription updates"""

import asyncio
import random
import time
import traceback
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

from astrbot.api import logger
from astrbot.api.message_components import Image, Plain

from ..parsers.anti_ban import get_anti_ban
from ..render_html.models import RenderPayload, SubscriptionRecord
from .base import BaseSubscriber, SubUpdate
from .constants import BANNER_PATH, LOGO_PATH
from .data_manager import DataManager
from .dispatcher import SubscriptionNotificationDispatcher, SubscriptionNotification
from .renderer import Renderer
from .utils import create_qrcode, image_to_base64

# 基础轮询间隔（秒）
BASE_POLL_INTERVAL = 300  # 5 分钟
JITTER_MAX = 120  # ±2 分钟

# 支持的多平台列表（非 Bilibili）
MULTI_PLATFORMS = {"xhs", "kujiequ", "weibo", "telegram", "twitter", "youtube"}


class GenericDynamicListener:
    """
    多平台订阅监听器。
    轮询非 Bilibili 平台的订阅更新，渲染为 HTML 卡片后推送。
    """

    def __init__(
        self,
        context: Any,
        data_manager: DataManager,
        renderer: Renderer,
        dispatcher: SubscriptionNotificationDispatcher,
        subscribers: Dict[str, BaseSubscriber],
        cfg: dict,
    ):
        self.context = context
        self.data_manager = data_manager
        self.renderer = renderer
        self.dispatcher = dispatcher
        self.subscribers = subscribers  # platform_name -> subscriber instance
        self.rai = cfg.get("rai", True)
        self.node = cfg.get("node", False)
        self.render_cache: OrderedDict[str, list] = OrderedDict()
        self.render_cache_limit = int(cfg.get("render_cache_limit", 32))
        self._anti_ban = get_anti_ban()

    async def start(self):
        """启动后台监听循环。"""
        logger.info("[通用订阅] 多平台订阅监听器启动")
        while True:
            try:
                await self._poll_cycle()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[通用订阅] 轮询异常: {e}\n{traceback.format_exc()}")
            # 基础间隔 + 大抖动（±2 分钟）
            jitter = random.uniform(-JITTER_MAX, JITTER_MAX)
            wait = max(60, BASE_POLL_INTERVAL + jitter)
            await asyncio.sleep(wait)

    async def _poll_cycle(self):
        """单次轮询周期：遍历所有多平台订阅。"""
        all_subs = self.data_manager.get_all_subscriptions()
        if not all_subs:
            return

        # 构建 (platform, uid) -> [(sub_user, sub_data)] 映射
        platform_uid_targets: Dict[Tuple[str, str], List[Tuple[str, SubscriptionRecord]]] = {}

        for sub_user, sub_list in all_subs.items():
            for sub_data in sub_list or []:
                platform = sub_data.platform
                if platform not in MULTI_PLATFORMS:
                    continue
                if platform not in self.subscribers:
                    continue
                key = (platform, sub_data.uid)
                platform_uid_targets.setdefault(key, []).append((sub_user, sub_data))

        if not platform_uid_targets:
            return

        # 按平台分组，每个平台内的请求间隔控制
        for (platform, uid), targets in platform_uid_targets.items():
            subscriber = self.subscribers.get(platform)
            if not subscriber:
                continue

            try:
                updates = await subscriber.fetch_updates(uid)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[通用订阅] 获取 {platform} uid={uid} 更新失败: {e}")
                continue

            if not updates:
                continue

            # 对每个订阅者分发更新
            for sub_user, sub_data in targets:
                await self._process_updates(sub_user, sub_data, updates)

            # 平台内请求间隔
            await asyncio.sleep(random.uniform(3, 8))

    async def _process_updates(
        self,
        sub_user: str,
        sub_data: SubscriptionRecord,
        updates: List[SubUpdate],
    ):
        """处理单个订阅者的更新列表。"""
        last_id = sub_data.last
        recent_ids = set(sub_data.recent_ids)
        known_ids = {x for x in ([last_id] + list(recent_ids)) if x}

        new_updates = []
        for update in updates:
            if update.id in known_ids:
                break
            new_updates.append(update)

        if not new_updates:
            return

        # 从旧到新发送
        for update in reversed(new_updates):
            payload = self._sub_update_to_payload(update, sub_data)
            if payload:
                await self._handle_new_update(sub_user, payload, update.id)
            # 更新记录
            sub_data.record_dynamic(update.id, self.data_manager.recent_dynamic_cache)

        await self.data_manager.save()

    def _sub_update_to_payload(
        self, update: SubUpdate, sub_data: SubscriptionRecord
    ) -> Optional[RenderPayload]:
        """将 SubUpdate 转换为 RenderPayload 用于渲染。"""
        from ..render_html.constants import PLATFORM_COLORS

        platform_display = {
            "xhs": "小红书",
            "kujiequ": "库街区",
            "weibo": "微博",
            "telegram": "Telegram",
            "twitter": "Twitter",
            "youtube": "YouTube",
        }

        # 获取用户信息（用于头像和名称）
        # 用户信息在 subscriber.get_user_info() 中获取，这里用基础信息
        name = ""
        avatar = ""
        handle = ""
        follower_count = ""
        signature = ""

        platform_color = PLATFORM_COLORS.get(update.platform, "#999999")
        display = platform_display.get(update.platform, update.platform)

        # 构建正文
        text_parts = []
        if update.text:
            text_parts.append(update.text)

        # 统计信息
        stats = {}

        # 时间戳
        timestamp = ""
        if update.timestamp:
            from datetime import datetime
            try:
                dt = datetime.fromtimestamp(update.timestamp)
                timestamp = dt.strftime("%Y-%m-%d %H:%M")
            except Exception:
                pass

        return RenderPayload(
            name=name or f"{display}用户",
            avatar=avatar,
            title=update.title,
            text="<br>".join(text_parts) if text_parts else "",
            image_urls=update.image_urls[:9],
            url=update.url,
            type=update.type,
            platform_display=display,
            platform_color=platform_color,
            uid=update.uid,
            handle=handle,
            follower_count=follower_count,
            signature=signature,
            stats=stats,
            timestamp=timestamp,
            banner=image_to_base64(BANNER_PATH),
            qrcode=create_qrcode(update.url) if update.url else "",
        )

    def _cache_render(self, update_id: str, chain_parts: list):
        """缓存渲染结果。"""
        if not update_id:
            return
        self.render_cache[update_id] = chain_parts
        while len(self.render_cache) > self.render_cache_limit:
            self.render_cache.popitem(last=False)

    async def _handle_new_update(
        self,
        sub_user: str,
        payload: RenderPayload,
        update_id: str,
    ):
        """处理并发送新的更新通知。"""
        # 检查缓存
        cached = self.render_cache.get(update_id)
        if cached:
            try:
                await self._send_notification(sub_user, cached, update_id)
            except Exception as e:
                logger.error(f"[通用订阅] 发送缓存更新失败: {e}")
            return

        # 渲染图片
        if self.rai:
            img_path = await self.renderer.render_dynamic(payload)
            if img_path:
                ls = [Image.fromFileSystem(img_path)]
                if payload.url:
                    ls.append(Plain(f"\n{payload.url}"))
                try:
                    await self._send_notification(sub_user, ls, update_id)
                    logger.info(f"[通用订阅] 推送完成(图片): sub_user={sub_user} id={update_id}")
                except Exception as e:
                    logger.error(f"[通用订阅] 推送失败: {e}")
                finally:
                    self._cache_render(update_id, ls)
                return

        # 降级纯文本
        ls = self._compose_plain_push(payload)
        try:
            await self._send_notification(sub_user, ls, update_id)
            logger.info(f"[通用订阅] 推送完成(文本): sub_user={sub_user} id={update_id}")
        except Exception as e:
            logger.error(f"[通用订阅] 推送失败: {e}")
        finally:
            self._cache_render(update_id, ls)

    async def _send_notification(
        self, sub_user: str, chain_parts: list, update_id: str
    ):
        """发送订阅通知。"""
        notification = SubscriptionNotification(
            sub_user=sub_user,
            chain_parts=chain_parts,
            send_node=self.node,
            category="dynamic",
            dyn_id=update_id,
        )
        await self.dispatcher.publish(notification)

    def _compose_plain_push(self, payload: RenderPayload) -> list:
        """纯文本模式下的消息链（≤3 段）。"""
        chain = []
        text_lines = []

        platform_display = payload.platform_display or "订阅"
        name = payload.name or "未知作者"
        text_lines.append(f"📣 [{platform_display}] {name}")

        if payload.title:
            text_lines.append(f"标题: {payload.title}")

        body = (payload.text or "").strip()
        if body:
            # 清理 HTML 标签
            import re
            body = re.sub(r"<[^>]+>", "", body).strip()
            text_lines.append(body)

        if payload.url:
            text_lines.append(payload.url)

        merged = "\n".join(filter(None, text_lines))
        if merged.strip():
            chain.append(Plain(merged))

        # 图片最多 2 张
        pics = [pic for pic in payload.image_urls if pic][:2]
        for pic in pics:
            chain.append(Image.fromURL(pic))

        return chain

    async def refresh_user_info(self, platform: str, uid: str) -> Optional[dict]:
        """获取用户信息（用于丰富订阅卡片）。"""
        subscriber = self.subscribers.get(platform)
        if not subscriber:
            return None
        try:
            info = await subscriber.get_user_info(uid)
            if info:
                return {
                    "name": info.name,
                    "avatar": info.avatar,
                    "handle": info.handle,
                    "follower_count": info.follower_count,
                    "signature": info.signature,
                }
        except Exception as e:
            logger.debug(f"[通用订阅] 获取用户信息失败 {platform}/{uid}: {e}")
        return None

"""Pixiv 插画/漫画/用户解析器

使用 pixivpy-async 库通过 OAuth refresh token 登录 Pixiv，
解析作品链接和用户主页链接。
"""

from __future__ import annotations

import re
from re import Match
from typing import Any, ClassVar

from astrbot.api import logger

from ..config import PluginConfig
from ..data import Platform, ParseResult
from ..download import Downloader
from ..exception import ParseException
from .base import BaseParser, handle


class PixivParser(BaseParser):
    """Pixiv 插画/漫画/用户解析器"""

    platform: ClassVar[Platform] = Platform(name="pixiv", display_name="Pixiv")

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)
        # Pixiv CDN 要求 Referer 头
        self.headers.update({"Referer": "https://www.pixiv.net/"})
        self.mycfg = config.parser.pixiv
        self._pixiv_client: Any = None
        self._pixiv_api: Any = None
        self._refresh_token: str = self.cfg.raw_data().get("pixiv_refresh_token") or ""

    # ------------------------------------------------------------------
    # pixivpy-async 的延迟初始化
    # ------------------------------------------------------------------

    @property
    def pixiv_api(self) -> Any:
        """延迟初始化 PixivAPI，避免未启用时额外导入"""
        if self._pixiv_api is None:
            from pixivpy_async import PixivAPI, PixivClient

            self._pixiv_client = PixivClient(proxy=self.proxy)
            self._pixiv_api = PixivAPI(client=self._pixiv_client)
        return self._pixiv_api

    async def _ensure_login(self) -> None:
        """通过 OAuth refresh token 登录（必要时）"""
        if not self._refresh_token:
            raise ParseException(
                "[Pixiv] 未配置 OAuth refresh token，"
                "请在 astrbot 插件配置中设置 pixiv_refresh_token"
            )
        api = self.pixiv_api
        if not api.access_token:
            try:
                await api.login(refresh_token=self._refresh_token)
                logger.info("[Pixiv] OAuth 登录成功")
            except Exception as e:
                raise ParseException(f"[Pixiv] OAuth 登录失败: {e}")

    async def close_session(self) -> None:
        """关闭 Pixiv 客户端以及框架的 aiohttp 会话"""
        if self._pixiv_client is not None:
            try:
                await self._pixiv_client.close()
            except Exception:
                pass
            self._pixiv_client = None
            self._pixiv_api = None
        await super().close_session()

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    @staticmethod
    def _strip_html(text: str) -> str:
        """去除 HTML 标签"""
        return re.sub(r"<[^>]+>", "", text).strip()

    @staticmethod
    def _parse_timestamp(iso_str: str) -> int | None:
        """将 Pixiv ISO-8601 时间戳转为 Unix 时间戳（秒）

        输入格式示例: "2024-01-15T12:34:56+09:00"
        """
        try:
            from datetime import datetime

            # 去掉 T 分隔符
            clean = iso_str.replace("T", " ")
            # 去掉时区部分 (如 +09:00) 或 Z
            if "+" in clean:
                clean = clean.split("+")[0]
            if "Z" in clean:
                clean = clean.replace("Z", "").strip()
            clean = clean.strip()
            # 保留前 19 字符: "2024-01-15 12:34:56"
            dt = datetime.strptime(clean[:19], "%Y-%m-%d %H:%M:%S")
            return int(dt.timestamp())
        except Exception:
            return None

    @staticmethod
    def _get_img_url(obj: Any, *keys: str) -> str | None:
        """从 pixivpy 模型对象中安全提取图片 URL。

        同时支持属性访问和字典访问两种方式。
        """
        if obj is None:
            return None
        for key in keys:
            try:
                if hasattr(obj, key):
                    val = getattr(obj, key)
                    if isinstance(val, str):
                        return val
                    obj = val
                elif isinstance(obj, dict):
                    val = obj.get(key)
                    if isinstance(val, str):
                        return val
                    obj = val
                else:
                    return None
            except Exception:
                return None
        return None

    @staticmethod
    def _collect_tags(illust: Any) -> list[str]:
        """从 illust 对象中提取标签名列表"""
        tags: list[str] = []
        try:
            raw_tags = illust.tags
            if raw_tags is None:
                return tags
            for tag in raw_tags:
                if hasattr(tag, "name"):
                    tags.append(tag.name)
                elif isinstance(tag, dict):
                    tags.append(tag.get("name", ""))
        except Exception:
            pass
        return tags

    # ------------------------------------------------------------------
    # URL 处理器
    # ------------------------------------------------------------------

    @handle("pixiv", r"pixiv\.net/artworks/(\d+)\/?(?:\?.*)?$")
    async def _parse_artwork(self, matched: Match[str]) -> ParseResult:
        """解析插画/漫画作品页 https://www.pixiv.net/artworks/{id}"""
        illust_id = matched.group(1)
        pixiv_headers = {"Referer": "https://www.pixiv.net/"}

        await self._ensure_login()

        # 1. 获取作品详情
        try:
            detail = await self.pixiv_api.illust_detail(illust_id)
        except Exception as e:
            raise ParseException(f"[Pixiv] 获取插画 {illust_id} 信息失败: {e}")

        illust = getattr(detail, "illust", None)
        if illust is None:
            raise ParseException(f"[Pixiv] 未找到插画: {illust_id}")

        # 2. 基本信息
        title: str = getattr(illust, "title") or ""
        raw_desc: str = getattr(illust, "caption") or ""
        description = self._strip_html(raw_desc)

        # 3. 作者
        user = getattr(illust, "user", None)
        author_name: str = getattr(user, "name") or "" if user else ""
        avatar_url: str | None = None
        if user and hasattr(user, "profile_image_urls"):
            avatar_url = self._get_img_url(user.profile_image_urls, "medium")
        author_desc = getattr(user, "comment", None) or None if user else None
        author = self.create_author(author_name, avatar_url, description=author_desc)
        if user:
            author.uid = str(getattr(user, "id", ""))

        # 4. 时间
        create_date: str = getattr(illust, "create_date") or ""
        timestamp = self._parse_timestamp(create_date) if create_date else None

        # 5. 标签
        tags = self._collect_tags(illust)
        tag_text = " ".join(f"#{t}" for t in tags[:10])

        # 6. 统计数据
        stats: dict[str, Any] = {
            "views": getattr(illust, "total_view", 0),
            "likes": getattr(illust, "total_bookmarks", 0),
        }

        # 7. 提取所有图片 URL（支持多页）
        image_urls: list[str] = []
        meta_pages = getattr(illust, "meta_pages", None)
        meta_single = getattr(illust, "meta_single_page", None)

        if meta_pages:
            for page in meta_pages:
                url = self._get_img_url(page, "image_urls", "original")
                if url:
                    image_urls.append(url)
        elif meta_single:
            url = self._get_img_url(meta_single, "original_image_url")
            if url:
                image_urls.append(url)

        if not image_urls:
            raise ParseException(f"[Pixiv] 无法获取插画 {illust_id} 的图片 URL")

        # 8. 创建内容
        contents = self.create_image_contents(image_urls, headers=pixiv_headers)
        page_suffix = f"({len(image_urls)}P)" if len(image_urls) > 1 else ""

        # 9. 构建文本
        text_parts: list[str] = []
        if description:
            text_parts.append(description[:300])
        if tag_text:
            text_parts.append(tag_text)
        text = "\n".join(text_parts) if text_parts else None

        return self.result(
            title=f"{title} {page_suffix}".strip(),
            text=text,
            author=author,
            contents=contents,
            timestamp=timestamp,
            url=f"https://www.pixiv.net/artworks/{illust_id}",
            stats=stats,
            extra={"uid": str(getattr(user, "id", "") or ""), "post_id": str(illust_id), "handle": f"@{author_name}"} if author_name else {"uid": str(getattr(user, "id", "") or ""), "post_id": str(illust_id)},
        )

    @handle("pixiv.net/users", r"pixiv\.net/users/(\d+)\/?(?:\?.*)?$")
    async def _parse_user(self, matched: Match[str]) -> ParseResult:
        """解析用户主页 https://www.pixiv.net/users/{id}"""
        user_id = matched.group(1)

        await self._ensure_login()

        # 1. 获取用户详情
        try:
            user_detail = await self.pixiv_api.user_detail(user_id)
        except Exception as e:
            raise ParseException(f"[Pixiv] 获取用户 {user_id} 信息失败: {e}")

        user = getattr(user_detail, "user", None)
        if user is None:
            raise ParseException(f"[Pixiv] 未找到用户: {user_id}")

        profile = getattr(user_detail, "profile", None)

        # 2. 提取用户信息
        name: str = getattr(user, "name") or ""
        account: str = getattr(user, "account") or ""
        avatar_url: str | None = None
        if hasattr(user, "profile_image_urls"):
            avatar_url = self._get_img_url(user.profile_image_urls, "medium")
        comment: str = getattr(user, "comment") or ""
        description = self._strip_html(comment) if comment else None

        follower_count = 0
        if profile:
            follower_count = getattr(profile, "total_follower", 0) or 0

        # 3. 构建作者
        author = self.create_author(name, avatar_url, description=description)
        author.uid = str(user_id)
        author.follower_count = follower_count

        # 4. 获取用户最新插画列表
        illusts: list[Any] = []
        try:
            illusts_resp = await self.pixiv_api.user_illusts(user_id)
            illusts = getattr(illusts_resp, "illusts", []) or []
        except Exception as e:
            logger.warning(f"[Pixiv] 获取用户 {user_id} 插画列表失败: {e}")

        # 限制展示数量
        illusts = illusts[:6]

        pixiv_headers = {"Referer": "https://www.pixiv.net/"}
        contents: list = []
        for illust in illusts:
            img_url = self._get_img_url(illust, "image_urls", "medium")
            if img_url:
                img_contents = self.create_image_contents(
                    [img_url], headers=pixiv_headers
                )
                contents.extend(img_contents)

        return self.result(
            title=name,
            text=f"@{account}" if account else None,
            author=author,
            contents=contents if contents else None,
            url=f"https://www.pixiv.net/users/{user_id}",
        )

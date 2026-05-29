"""反封禁管理器 — 统一控制请求频率、UA 轮换、抖动、退避、代理决策"""

import asyncio
import random
import time
from typing import ClassVar

from astrbot.api import logger


class AntiBanManager:
    """统一请求管理：频率控制 + UA 轮换 + 抖动 + 指数退避 + 代理决策"""

    # 需要代理的平台（国外）
    PROXY_REQUIRED: ClassVar[set[str]] = {
        "youtube", "twitter", "instagram", "facebook", "telegram", "pixiv",
    }

    # 不需要代理的平台（国内）
    NO_PROXY: ClassVar[set[str]] = {
        "bilibili", "xhs", "weibo", "kujiequ", "douyin", "kuaishou",
        "ncm", "qqmusic", "kugou", "kuwo", "acfun", "nga", "zhihu",
        "lofter", "mihuashi", "cpp", "huajia", "xiaoheihe",
    }

    # 现代浏览器 UA 池
    USER_AGENTS: ClassVar[list[str]] = [
        # Chrome Windows
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        # Chrome Mac
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        # Firefox
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.5; rv:126.0) Gecko/20100101 Firefox/126.0",
        # Safari
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
        # Edge
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0",
        # Mobile
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
        "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Mobile Safari/537.36",
    ]

    def __init__(self):
        self._last_request: dict[str, float] = {}    # domain → timestamp
        self._backoff_until: dict[str, float] = {}   # domain → timestamp
        self._fail_counts: dict[str, int] = {}        # domain → consecutive failures

    def random_ua(self) -> str:
        """随机 User-Agent"""
        return random.choice(self.USER_AGENTS)

    def get_headers(self, domain: str = "", base_headers: dict | None = None) -> dict:
        """获取带随机 UA 的请求头"""
        headers = {
            "User-Agent": self.random_ua(),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
        }
        if base_headers:
            headers.update(base_headers)
            # 确保 UA 被随机化
            headers["User-Agent"] = self.random_ua()
        return headers

    def get_proxy(self, platform_name: str, configured_proxy: str) -> str | None:
        """根据平台决定是否走代理

        Args:
            platform_name: 平台名称（如 "youtube", "bilibili"）
            configured_proxy: 配置中的代理地址

        Returns:
            代理地址或 None
        """
        if platform_name in self.PROXY_REQUIRED:
            return configured_proxy or None
        return None  # 国内平台不走代理

    async def wait_if_needed(self, domain: str, min_interval: float = 5.0):
        """请求前等待：最小间隔 + 大抖动（±50%）+ 指数退避检查

        Args:
            domain: 请求域名或平台名
            min_interval: 最小间隔秒数
        """
        now = time.monotonic()

        # 指数退避检查
        backoff = self._backoff_until.get(domain, 0)
        if now < backoff:
            wait = backoff - now
            logger.debug(f"[AntiBan] {domain} 退避等待 {wait:.1f}s")
            await asyncio.sleep(wait)

        # 最小间隔 + 抖动（±50%）
        last = self._last_request.get(domain, 0)
        elapsed = time.monotonic() - last
        jitter_interval = min_interval * random.uniform(0.5, 1.5)
        if elapsed < jitter_interval:
            wait = jitter_interval - elapsed
            await asyncio.sleep(wait)

        self._last_request[domain] = time.monotonic()

    def record_success(self, domain: str):
        """记录成功请求，重置退避和失败计数"""
        self._backoff_until.pop(domain, None)
        self._fail_counts[domain] = 0

    def record_failure(self, domain: str, is_rate_limit: bool = False, retry_after: int = 0):
        """记录失败请求，设置指数退避

        Args:
            domain: 请求域名或平台名
            is_rate_limit: 是否为 429 限流响应
            retry_after: 服务器建议的等待秒数
        """
        fails = self._fail_counts.get(domain, 0) + 1
        self._fail_counts[domain] = fails

        if is_rate_limit or retry_after > 0:
            # 限流：使用服务器建议值或指数退避
            wait = retry_after if retry_after > 0 else min(60 * (2 ** (fails - 1)), 3600)
        else:
            # 普通失败：指数退避
            wait = min(30 * (2 ** (fails - 1)), 1800)

        self._backoff_until[domain] = time.monotonic() + wait
        logger.warning(f"[AntiBan] {domain} 失败(#{fails})，退避 {wait}s")

    def should_skip(self, domain: str) -> bool:
        """检查是否应该跳过本次请求（连续失败过多）"""
        fails = self._fail_counts.get(domain, 0)
        if fails >= 5:
            backoff = self._backoff_until.get(domain, 0)
            if time.monotonic() < backoff:
                return True
        return False


# 全局单例
_anti_ban: AntiBanManager | None = None


def get_anti_ban() -> AntiBanManager:
    """获取全局 AntiBanManager 单例"""
    global _anti_ban
    if _anti_ban is None:
        _anti_ban = AntiBanManager()
    return _anti_ban

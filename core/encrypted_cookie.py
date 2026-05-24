"""
加密 Cookie 存储。

使用 Fernet (AES-128) 加密持久化 Cookie，
支持长期保留和自动过期清理。
"""

import base64
import hashlib
import json
import time
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet
from astrbot.api import logger

from .cookie import CookieJar
from .config import PluginConfig


class EncryptedCookieManager:
    """
    加密 Cookie 管理器。

    特性：
    - AES-128 加密存储 (Fernet)
    - 自动过期清理 (configurable retention_days)
    - 兼容现有 CookieJar 接口
    """

    def __init__(self, config: PluginConfig):
        self.cfg = config
        self._cipher: Optional[Fernet] = None
        self._init_cipher()

    def _init_cipher(self):
        """从配置初始化加密密钥"""
        key_str = self.cfg.cookie_encrypt_key
        if key_str:
            key = base64.urlsafe_b64encode(
                hashlib.sha256(key_str.encode()).digest()
            )
            self._cipher = Fernet(key)
        else:
            logger.warning("[Cookie] 未设置加密密钥，Cookie将以明文存储")

    def _encrypt(self, data: str) -> str:
        if self._cipher:
            return self._cipher.encrypt(data.encode()).decode()
        return data

    def _decrypt(self, data: str) -> str:
        if self._cipher:
            try:
                return self._cipher.decrypt(data.encode()).decode()
            except Exception:
                logger.warning("[Cookie] 解密失败，返回原始数据")
                return data
        return data

    def get_storage_path(self, platform: str) -> Path:
        """获取平台 Cookie 存储路径"""
        cookie_dir = self.cfg.cookie_dir
        cookie_dir.mkdir(parents=True, exist_ok=True)
        return cookie_dir / f"{platform}_encrypted.json"

    def save_cookies(self, platform: str, cookie_dict: dict) -> None:
        """保存加密后的 Cookie"""
        payload = {
            "platform": platform,
            "cookies": cookie_dict,
            "saved_at": int(time.time()),
            "expires_at": int(time.time()) + self.cfg.cookie_retention_days * 86400,
        }
        encrypted = self._encrypt(json.dumps(payload, ensure_ascii=False))
        path = self.get_storage_path(platform)
        path.write_text(encrypted, encoding="utf-8")
        logger.info(f"[Cookie] {platform} Cookie已保存到 {path}")

    def load_cookies(self, platform: str) -> Optional[dict]:
        """加载并解密 Cookie，自动检查过期"""
        path = self.get_storage_path(platform)
        if not path.exists():
            return None

        try:
            encrypted = path.read_text(encoding="utf-8")
            decrypted = self._decrypt(encrypted)
            payload = json.loads(decrypted)

            # 检查过期
            if payload.get("expires_at", 0) < int(time.time()):
                logger.info(f"[Cookie] {platform} Cookie已过期，自动清理")
                path.unlink(missing_ok=True)
                return None

            return payload.get("cookies")
        except Exception as e:
            logger.warning(f"[Cookie] 加载 {platform} Cookie失败: {e}")
            return None

    def clear_cookies(self, platform: str) -> None:
        """清除平台 Cookie"""
        path = self.get_storage_path(platform)
        if path.exists():
            path.unlink()
            logger.info(f"[Cookie] {platform} Cookie已清除")

    def apply_to_jar(self, platform: str, jar: CookieJar) -> bool:
        """将加密存储的 Cookie 应用到 CookieJar"""
        cookies = self.load_cookies(platform)
        if not cookies:
            return False
        for name, value in cookies.items():
            jar.set(name, value, domain=jar.domain)
        return True

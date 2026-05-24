"""Tests for encrypted cookie storage"""

import importlib
import json
import sys
import time
import types
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def config():
    """Mock plugin config"""
    return SimpleNamespace(
        cookie_encrypt_key="test-key-12345",
        cookie_retention_days=30,
        cookie_dir=Path("/tmp/test_cookies"),
    )


def _setup_mocks(monkeypatch):
    """Mock astrbot and core.config so that cookie.py can be imported without astrbot.core."""
    logger = SimpleNamespace(
        debug=lambda *a, **kw: None,
        info=lambda *a, **kw: None,
        warning=lambda *a, **kw: None,
    )

    astrbot_pkg = types.ModuleType("astrbot")
    astrbot_pkg.__path__ = []
    api_module = types.ModuleType("astrbot.api")
    api_module.logger = logger
    monkeypatch.setitem(sys.modules, "astrbot", astrbot_pkg)
    monkeypatch.setitem(sys.modules, "astrbot.api", api_module)

    # Mock core.config so that cookie.py's `from .config import ParserItem, PluginConfig`
    # resolves without triggering the real config.py (which needs astrbot.core).
    config_module = types.ModuleType("core.config")
    config_module.ParserItem = object
    config_module.PluginConfig = object
    monkeypatch.setitem(sys.modules, "core.config", config_module)


@pytest.fixture
def manager(config, monkeypatch):
    """Lazy-import and create EncryptedCookieManager"""
    _setup_mocks(monkeypatch)
    monkeypatch.delitem(sys.modules, "core.encrypted_cookie", raising=False)

    from core.encrypted_cookie import EncryptedCookieManager
    return EncryptedCookieManager(config)


class TestEncryptedCookieManager:
    def test_encrypt_decrypt_roundtrip(self, manager):
        data = "my_secret_cookie_value"
        encrypted = manager._encrypt(data)
        assert encrypted != data
        decrypted = manager._decrypt(encrypted)
        assert decrypted == data

    def test_save_and_load(self, manager, tmp_path):
        manager.cfg.cookie_dir = tmp_path
        platform = "bilibili"
        cookies = {"sessdata": "abc123", "bili_jct": "def456"}

        manager.save_cookies(platform, cookies)
        loaded = manager.load_cookies(platform)

        assert loaded is not None
        assert loaded["sessdata"] == "abc123"
        assert loaded["bili_jct"] == "def456"

    def test_expired_cookie_returns_none(self, manager, tmp_path):
        manager.cfg.cookie_dir = tmp_path
        manager.cfg.cookie_retention_days = -1  # expires immediately (past)

        manager.save_cookies("test", {"key": "val"})
        loaded = manager.load_cookies("test")
        assert loaded is None  # expired

    def test_clear_cookies(self, manager, tmp_path):
        manager.cfg.cookie_dir = tmp_path
        manager.save_cookies("test", {"key": "val"})
        assert manager.load_cookies("test") is not None

        manager.clear_cookies("test")
        assert manager.load_cookies("test") is None

    def test_without_encryption_key(self, config, monkeypatch):
        config.cookie_encrypt_key = ""
        _setup_mocks(monkeypatch)
        monkeypatch.delitem(sys.modules, "core.encrypted_cookie", raising=False)

        from core.encrypted_cookie import EncryptedCookieManager
        mgr = EncryptedCookieManager(config)
        assert mgr._encrypt("test") == "test"  # plaintext passthrough
        assert mgr._decrypt("test") == "test"

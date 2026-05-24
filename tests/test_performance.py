"""Tests for performance optimizations (Phase 13)"""

import pytest
from pathlib import Path


class TestLazyTemplateLoading:
    """engine.py should load templates lazily, not at startup"""

    def test_init_does_not_load_templates(self):
        """HtmlCardRenderer should not load templates at __init__"""
        import sys
        import types
        from types import SimpleNamespace
        from importlib import reload

        # Mock astrbot
        if "astrbot" not in sys.modules:
            astrbot = types.ModuleType("astrbot")
            astrbot.api = types.ModuleType("astrbot.api")
            astrbot.api.logger = SimpleNamespace(
                info=lambda *a, **kw: None, debug=lambda *a, **kw: None,
                warning=lambda *a, **kw: None, error=lambda *a, **kw: None,
            )
            sys.modules["astrbot"] = astrbot
            sys.modules["astrbot.api"] = astrbot.api

        from core.render_html.engine import HtmlCardRenderer

        # After init, _templates should be empty (lazy loading)
        renderer = HtmlCardRenderer(star=None)
        assert len(renderer._templates) == 0


class TestBgmClientSession:
    """bgm_client.py should reuse sessions"""

    def test_persistent_session_attribute(self):
        """BangumiApiClient should have _session attribute"""
        import sys, types
        from types import SimpleNamespace

        if "astrbot" not in sys.modules:
            astrbot = types.ModuleType("astrbot")
            astrbot.api = types.ModuleType("astrbot.api")
            astrbot.api.logger = SimpleNamespace(info=lambda *a, **kw: None)
            sys.modules["astrbot"] = astrbot
            sys.modules["astrbot.api"] = astrbot.api

        from core.subscriber.bgm_client import BangumiApiClient

        client = BangumiApiClient(token="", user_agent="test/1.0")
        assert hasattr(client, "_session")


class TestPlaylistWALMode:
    """playlist.py should set WAL mode"""

    def test_wal_pragma_in_code(self):
        """SQLite WAL pragma should appear in playlist source"""
        source_path = Path(__file__).parent.parent / "core" / "music" / "playlist.py"
        source = source_path.read_text(encoding="utf-8")
        assert "WAL" in source or "wal" in source
        assert "busy_timeout" in source

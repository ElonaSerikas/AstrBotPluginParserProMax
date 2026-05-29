"""Tests for ParseResult ↔ RenderPayload bridge"""

import importlib
import sys
import types
from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def mock_astrbot(monkeypatch):
    """Mock astrbot modules for bridge imports"""
    logger = SimpleNamespace(
        info=lambda *a, **kw: None,
        debug=lambda *a, **kw: None,
        warning=lambda *a, **kw: None,
        error=lambda *a, **kw: None,
    )
    astrbot_pkg = types.ModuleType("astrbot")
    astrbot_pkg.__path__ = []
    api_module = types.ModuleType("astrbot.api")
    api_module.logger = logger
    monkeypatch.setitem(sys.modules, "astrbot", astrbot_pkg)
    monkeypatch.setitem(sys.modules, "astrbot.api", api_module)


@pytest.fixture
def bridge():
    """Lazy-import bridge after astrbot mock"""
    return importlib.import_module("core.render_html.bridge")


@pytest.fixture
def models():
    """Lazy-import render_html models"""
    return importlib.import_module("core.render_html.models")


@pytest.fixture
def data():
    """Lazy-import core data"""
    return importlib.import_module("core.data")


class TestParseResultToRenderPayload:
    def test_basic_fields_are_mapped(self, bridge, data, models):
        result = data.ParseResult(
            platform=data.Platform(name="bilibili", display_name="哔哩哔哩"),
            author=data.Author(name="测试UP主", description="测试签名", uid="12345"),
            title="测试视频标题",
            text="测试正文内容",
            url="https://b23.tv/test",
        )
        payload = bridge.parse_result_to_render_payload(result)
        assert payload.name == "测试UP主"
        assert payload.title == "测试视频标题"
        assert payload.text == "测试正文内容"
        assert payload.url == "https://b23.tv/test"

    def test_image_urls_extracted(self, bridge, data):
        result = data.ParseResult(
            platform=data.Platform(name="pixiv", display_name="Pixiv"),
            contents=[data.ImageContent("https://i.pximg.net/img1.png", source_url="https://i.pximg.net/img1.png")],
        )
        payload = bridge.parse_result_to_render_payload(result)
        assert len(payload.image_urls) == 1

    def test_author_without_avatar(self, bridge, data):
        result = data.ParseResult(
            platform=data.Platform(name="bilibili", display_name="哔哩哔哩"),
            author=data.Author(name="无头像UP主"),
        )
        payload = bridge.parse_result_to_render_payload(result)
        assert payload.name == "无头像UP主"
        assert payload.avatar == ""

    def test_empty_result(self, bridge, data):
        result = data.ParseResult(platform=data.Platform(name="test", display_name="Test"))
        payload = bridge.parse_result_to_render_payload(result)
        assert payload.name == ""

    def test_repost_is_mapped_to_forward(self, bridge, data):
        result = data.ParseResult(
            platform=data.Platform(name="bilibili", display_name="哔哩哔哩"),
            author=data.Author(name="原PO主"),
            title="转发视频",
            repost=data.ParseResult(
                platform=data.Platform(name="bilibili", display_name="哔哩哔哩"),
                author=data.Author(name="被转发UP"),
                title="被转发视频",
                url="https://b23.tv/original",
            ),
        )
        payload = bridge.parse_result_to_render_payload(result)
        assert payload.forward is not None
        assert payload.forward.name == "被转发UP"

    def test_stats_are_formatted(self, bridge, data):
        result = data.ParseResult(
            platform=data.Platform(name="bilibili", display_name="哔哩哔哩"),
            stats={"views": 123456, "likes": 7890, "danmaku": 1234},
        )
        payload = bridge.parse_result_to_render_payload(result)
        assert payload.stats["views"] == "123456"
        assert payload.stats["likes"] == "7890"

    def test_pinned_comment_is_mapped(self, bridge, data):
        result = data.ParseResult(
            platform=data.Platform(name="bilibili", display_name="哔哩哔哩"),
            pinned_comment=data.Comment(author_name="热评用户", content="置顶评论", likes=999, timestamp=1700000000),
        )
        payload = bridge.parse_result_to_render_payload(result)
        assert payload.pinned_comment is not None

    def test_comments_list_truncated_to_10(self, bridge, data):
        comments = [data.Comment(author_name=f"用户{i}", content=f"评论{i}") for i in range(15)]
        result = data.ParseResult(
            platform=data.Platform(name="bilibili", display_name="哔哩哔哩"),
            comments=comments,
        )
        payload = bridge.parse_result_to_render_payload(result)
        assert len(payload.comments) == 10

    def test_platform_color_is_set(self, bridge, data):
        result = data.ParseResult(platform=data.Platform(name="bilibili", display_name="哔哩哔哩"))
        payload = bridge.parse_result_to_render_payload(result)
        assert payload.platform_color == "#fb7299"

    def test_sanitize_removes_script_tags(self, bridge, data):
        result = data.ParseResult(
            platform=data.Platform(name="test", display_name="Test"),
            text='<script>alert("xss")</script>正常内容',
        )
        payload = bridge.parse_result_to_render_payload(result)
        assert "script" not in payload.text
        assert "正常内容" in payload.text

    def test_sanitize_removes_event_handlers(self, bridge, data):
        result = data.ParseResult(
            platform=data.Platform(name="test", display_name="Test"),
            text='<img src=x onerror=alert(1)>',
        )
        payload = bridge.parse_result_to_render_payload(result)
        assert "onerror" not in payload.text

    def test_sanitize_allows_safe_html(self, bridge, data):
        result = data.ParseResult(
            platform=data.Platform(name="test", display_name="Test"),
            text='<br><b>粗体</b><a href="https://example.com">链接</a>',
        )
        payload = bridge.parse_result_to_render_payload(result)
        assert "<b>" in payload.text
        assert "<a" in payload.text


class TestRenderPayloadToParseResult:
    def test_basic_fields_are_mapped(self, bridge, data, models):
        payload = models.RenderPayload(name="测试UP", title="测试标题", text="测试正文", url="https://b23.tv/test")
        result = bridge.render_payload_to_parse_result(payload)
        assert result.author is not None
        assert result.author.name == "测试UP"
        assert result.title == "测试标题"

    def test_image_urls_mapped(self, bridge, data, models):
        payload = models.RenderPayload(image_urls=["https://example.com/img1.png"])
        result = bridge.render_payload_to_parse_result(payload)
        assert len(result.contents) == 1

    def test_forward_is_mapped_to_repost(self, bridge, data, models):
        payload = models.RenderPayload(
            name="主UP",
            forward=models.ForwardPayload(name="被转发UP", title="转发标题"),
        )
        result = bridge.render_payload_to_parse_result(payload)
        assert result.repost is not None
        assert result.repost.author is not None
        assert result.repost.author.name == "被转发UP"

"""Tests for AI summary system"""

import importlib
import sys
import types
from types import SimpleNamespace

import pytest


@pytest.fixture
def ai_summary(monkeypatch):
    """Mock astrbot and import ai_summary module"""
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

    return importlib.import_module("core.ai_summary")


class TestStripHtmlTags:
    def test_removes_simple_tags(self, ai_summary):
        assert ai_summary.strip_html_tags("<p>hello</p>") == "hello"

    def test_removes_nested_tags(self, ai_summary):
        assert ai_summary.strip_html_tags("<div><p>test</p></div>") == "test"

    def test_handles_empty_string(self, ai_summary):
        assert ai_summary.strip_html_tags("") == ""

    def test_handles_plain_text(self, ai_summary):
        assert ai_summary.strip_html_tags("just text") == "just text"

    def test_removes_attributes(self, ai_summary):
        result = ai_summary.strip_html_tags(
            '<a href="https://example.com">link</a>'
        )
        assert result == "link"


class TestBuildSummaryPrompt:
    def test_contains_platform_name(self, ai_summary):
        prompt = ai_summary.build_summary_prompt(
            platform="bilibili", title="测试视频"
        )
        assert "bilibili" in prompt

    def test_includes_title(self, ai_summary):
        prompt = ai_summary.build_summary_prompt(
            platform="bilibili", title="我的测试视频"
        )
        assert "测试视频" in prompt

    def test_includes_author(self, ai_summary):
        prompt = ai_summary.build_summary_prompt(
            platform="bilibili", author="UP主名", title="视频"
        )
        assert "UP主名" in prompt

    def test_includes_text_content(self, ai_summary):
        prompt = ai_summary.build_summary_prompt(
            platform="bilibili", text="这是正文内容"
        )
        assert "这是正文内容" in prompt

    def test_strips_html_from_text(self, ai_summary):
        prompt = ai_summary.build_summary_prompt(
            platform="bilibili", text="<p>HTML内容</p><br>第二行"
        )
        assert "HTML内容" in prompt
        assert "<p>" not in prompt
        assert "<br>" not in prompt

    def test_includes_page_type(self, ai_summary):
        prompt = ai_summary.build_summary_prompt(
            platform="bilibili", title="测试", page_type="video"
        )
        assert "video" in prompt

    def test_includes_stats(self, ai_summary):
        prompt = ai_summary.build_summary_prompt(
            platform="bilibili",
            stats={"views": 10000, "likes": 500},
        )
        assert "views" in prompt
        assert "likes" in prompt

    def test_has_output_format_instructions(self, ai_summary):
        prompt = ai_summary.build_summary_prompt(
            platform="bilibili", title="测试"
        )
        assert "📌" in prompt or "一句话" in prompt

    def test_truncates_long_text(self, ai_summary):
        long_text = "A" * 1000
        prompt = ai_summary.build_summary_prompt(
            platform="bilibili", text=long_text
        )
        assert len(prompt) < 800

    def test_empty_input_produces_valid_prompt(self, ai_summary):
        prompt = ai_summary.build_summary_prompt(platform="bilibili")
        assert len(prompt) > 10


class TestBuildTextSummary:
    def test_includes_summary_text(self, ai_summary):
        result = ai_summary.build_text_summary("这是一个AI摘要")
        assert "AI 内容概况" in result
        assert "这是一个AI摘要" in result

    def test_includes_url(self, ai_summary):
        result = ai_summary.build_text_summary(
            "摘要", url="https://b23.tv/test"
        )
        assert "https://b23.tv/test" in result

    def test_includes_stats(self, ai_summary):
        result = ai_summary.build_text_summary(
            "摘要",
            stats={"views": "1.2万", "likes": "500"},
        )
        assert "1.2万" in result
        assert "500" in result

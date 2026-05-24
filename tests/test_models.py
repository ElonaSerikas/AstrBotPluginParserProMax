"""Tests for RenderPayload and models"""

import pytest
from core.render_html.models import RenderPayload, ForwardPayload


class TestRenderPayload:
    def test_default_values(self):
        p = RenderPayload()
        assert p.name == ""
        assert p.avatar == ""
        assert p.text == ""
        assert p.image_urls == []
        assert p.forward is None

    def test_to_dict_roundtrip(self):
        p = RenderPayload(
            name="测试UP",
            title="测试标题",
            text="测试正文",
            url="https://b23.tv/test",
            image_urls=["https://example.com/img.png"],
            platform_color="#fb7299",
        )
        d = p.to_dict()
        assert d["name"] == "测试UP"
        assert d["title"] == "测试标题"
        assert d["image_urls"] == ["https://example.com/img.png"]

        p2 = RenderPayload.from_dict(d)
        assert p2.name == "测试UP"
        assert p2.title == "测试标题"
        assert p2.image_urls == ["https://example.com/img.png"]

    def test_from_dict_none(self):
        p = RenderPayload.from_dict(None)
        assert p.name == ""

    def test_to_template_context(self):
        p = RenderPayload(name="test", title="hello")
        ctx = p.to_template_context()
        assert ctx["name"] == "test"
        assert ctx["title"] == "hello"

    def test_forward_roundtrip(self):
        f = ForwardPayload(name="被转发UP", title="转发标题")
        p = RenderPayload(name="主UP", forward=f)
        d = p.to_dict()
        assert "forward" in d
        assert d["forward"]["name"] == "被转发UP"

        p2 = RenderPayload.from_dict(d)
        assert p2.forward is not None
        assert p2.forward.name == "被转发UP"

    def test_to_forward_payload(self):
        p = RenderPayload(name="UP主", title="标题", text="内容")
        f = p.to_forward_payload()
        assert isinstance(f, ForwardPayload)
        assert f.name == "UP主"
        assert f.title == "标题"


class TestForwardPayload:
    def test_default_values(self):
        f = ForwardPayload()
        assert f.name == ""
        assert f.image_urls == []

    def test_to_dict_roundtrip(self):
        f = ForwardPayload(
            name="转发者",
            text="转发内容",
            url="https://b23.tv/original",
        )
        d = f.to_dict()
        assert d["name"] == "转发者"

        f2 = ForwardPayload.from_dict(d)
        assert f2.name == "转发者"
        assert f2.text == "转发内容"

    def test_from_dict_none(self):
        f = ForwardPayload.from_dict(None)
        assert f.name == ""


class TestDynamicParseResult:
    def test_deliver(self):
        from core.render_html.models import DynamicParseResult
        payload = RenderPayload(name="test")
        r = DynamicParseResult.deliver(payload, "dyn_123")
        assert r.has_payload() is True
        assert r.dyn_id == "dyn_123"
        assert r.skipped is False

    def test_skip(self):
        from core.render_html.models import DynamicParseResult
        r = DynamicParseResult.skip("dyn_456", "filtered by type")
        assert r.has_payload() is False
        assert r.skipped is True
        assert r.reason == "filtered by type"

    def test_empty(self):
        from core.render_html.models import DynamicParseResult
        r = DynamicParseResult.empty()
        assert r.has_payload() is False
        assert r.skipped is False
        assert r.dyn_id is None

"""Tests for cross-platform dedup"""
from core.subscriber.dedup import CrossPlatformDedup
from core.subscriber.base import SubUpdate


class TestCrossPlatformDedup:
    def test_exact_duplicate_detected(self):
        dedup = CrossPlatformDedup(cache_size=100, ttl=3600)
        update = SubUpdate(id="1", platform="bilibili", uid="123", type="video", title="测试视频", text="内容")

        assert dedup.is_duplicate(update) is False  # first time
        assert dedup.is_duplicate(update) is True   # duplicate

    def test_same_content_different_platform(self):
        dedup = CrossPlatformDedup(cache_size=100, ttl=3600)

        bilibili = SubUpdate(id="1", platform="bilibili", uid="123", type="video", title="相同内容", text="正文")
        xhs = SubUpdate(id="2", platform="xhs", uid="456", type="note", title="相同内容", text="正文")

        assert dedup.is_duplicate(bilibili) is False
        assert dedup.is_duplicate(xhs) is True  # same content hash

    def test_different_content_not_duplicate(self):
        dedup = CrossPlatformDedup(cache_size=100, ttl=3600)

        a = SubUpdate(id="1", platform="bilibili", uid="123", type="video", title="视频A", text="内容A")
        b = SubUpdate(id="2", platform="bilibili", uid="123", type="video", title="视频B", text="内容B")

        assert dedup.is_duplicate(a) is False
        assert dedup.is_duplicate(b) is False

    def test_empty_title_and_text(self):
        dedup = CrossPlatformDedup(cache_size=100, ttl=3600)
        update = SubUpdate(id="1", platform="bilibili", uid="123", type="live")

        assert dedup.is_duplicate(update) is False  # should not crash

    def test_cache_eviction(self):
        dedup = CrossPlatformDedup(cache_size=2, ttl=3600)

        for i in range(3):
            u = SubUpdate(id=str(i), platform="test", uid="1", type="text", title=f"Title{i}", text=f"Body{i}")
            dedup.is_duplicate(u)

        # First one should be evicted
        first = SubUpdate(id="0", platform="test", uid="1", type="text", title="Title0", text="Body0")
        assert dedup.is_duplicate(first) is False  # evicted, not duplicate

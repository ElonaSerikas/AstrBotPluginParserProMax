"""Cross-platform content deduplication"""
import hashlib
import time
from collections import OrderedDict

from .base import SubUpdate


class CrossPlatformDedup:
    """
    Detect duplicate content across platforms.

    Some creators post the same content on multiple platforms
    (e.g., B站 + Xiaohongshu). This deduplicates by content fingerprint.
    """

    def __init__(self, cache_size: int = 1000, ttl: int = 86400):
        self._cache: OrderedDict[str, float] = OrderedDict()
        self._maxsize = cache_size
        self._ttl = ttl

    def _compute_hash(self, update: SubUpdate) -> str:
        content = f"{update.title}|{update.text}"
        return hashlib.sha256(content.encode()).hexdigest()[:16]

    def is_duplicate(self, update: SubUpdate) -> bool:
        """Check if content is duplicate across platforms"""
        now = time.time()
        h = self._compute_hash(update)

        # Clean expired entries
        while self._cache and next(iter(self._cache.values())) < now - self._ttl:
            self._cache.popitem(last=False)

        if h in self._cache:
            return True

        self._cache[h] = now
        # Enforce max size
        while len(self._cache) > self._maxsize:
            self._cache.popitem(last=False)

        return False

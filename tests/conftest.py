"""Global test configuration - mock astrbot for all tests"""
import importlib
import sys
import types
from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True, scope="session")
def mock_astrbot():
    """Mock astrbot modules for testing"""
    if "astrbot" in sys.modules:
        return

    astrbot = types.ModuleType("astrbot")
    astrbot.api = types.ModuleType("astrbot.api")
    astrbot.api.logger = SimpleNamespace(
        info=lambda *a, **kw: None,
        debug=lambda *a, **kw: None,
        warning=lambda *a, **kw: None,
        error=lambda *a, **kw: None,
    )
    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = astrbot.api

from __future__ import annotations

import pytest

from wheel_screener.config import Settings


@pytest.fixture
def settings() -> Settings:
    """Default settings (no real keys) for tests that need a config object."""
    return Settings()

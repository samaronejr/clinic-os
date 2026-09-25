"""Real Redis on an owned, random loopback port; never the host default."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import pytest
from ops.testing.realtime_stack import redis_server

if TYPE_CHECKING:
    from collections.abc import Iterator

    from pytest_django.fixtures import SettingsWrapper


@pytest.fixture(scope="session")
def redis_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    supplied = os.environ.get("REALTIME_REDIS_URL", "")
    if supplied:
        parsed = urlsplit(supplied)
        assert parsed.hostname == "127.0.0.1"
        assert parsed.port not in (None, 6379)
        yield supplied
        return
    with redis_server(Path.cwd(), tmp_path_factory.mktemp("realtime-redis")) as url:
        yield url


@pytest.fixture
def real_redis(redis_url: str, settings: SettingsWrapper) -> None:
    settings.REALTIME_REDIS_URL = redis_url
    settings.REALTIME_ENABLED = True

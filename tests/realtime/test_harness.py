"""Redis readiness must not make the existing prefork handoff multithreaded."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ops.testing import realtime_stack
from redis import Redis

if TYPE_CHECKING:
    import subprocess
    from threading import Thread
    from typing import IO

    import pytest


def test_redis_readiness_reaps_its_observer_before_yielding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    readers: list[Thread] = []
    ready = realtime_stack._ready

    def observe(process: subprocess.Popen[str], marker: str, log: IO[str]) -> Thread:
        thread = ready(process, marker, log)
        readers.append(thread)
        return thread

    monkeypatch.setattr(realtime_stack, "_ready", observe)
    with realtime_stack.redis_server(Path.cwd(), tmp_path) as url:
        with Redis.from_url(url) as client:
            assert client.ping()
        assert len(readers) == 1
        assert not readers[0].is_alive()

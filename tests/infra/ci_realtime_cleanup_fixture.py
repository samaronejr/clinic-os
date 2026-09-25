"""Real Redis resource boundary used inside the actual CI daemon supervisor."""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch
from uuid import uuid4

from ops.testing import realtime_stack
from ops.testing.isolation_docker_metadata import run_docker_command

if TYPE_CHECKING:
    from collections.abc import Iterator


def assert_absent(volumes: list[str]) -> None:
    remaining = []
    for volume in volumes:
        found = run_docker_command(
            ("volume", "ls", "-q", "--filter", "name=^" + volume + "$")
        )
        if found.strip():
            remaining.append(volume)
            # A red/mutation run still removes only resources observed mounted
            # on the unique container this fixture itself created.
            run_docker_command(("volume", "rm", volume))
    assert not remaining, "realtime lease left unclaimed Docker volumes"


@contextmanager
def redis_cleanup_lease(root: Path) -> Iterator[None]:
    identity = uuid4()
    volumes = []
    try:
        with (
            patch.object(realtime_stack, "uuid4", return_value=identity),
            realtime_stack.redis_server(Path.cwd(), root),
        ):
            mounts = json.loads(
                run_docker_command(
                    (
                        "inspect",
                        "--format",
                        "{{json .Mounts}}",
                        "clinic-realtime-test-" + identity.hex,
                    )
                )
            )
            volumes = [mount["Name"] for mount in mounts if mount["Type"] == "volume"]
            yield
    finally:
        assert_absent(volumes)

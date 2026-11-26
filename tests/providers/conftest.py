"""Fixtures for provider lifecycle tests."""

from __future__ import annotations

import socket
from typing import TYPE_CHECKING

import pytest
from ops.release import activation

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


@pytest.fixture(autouse=True)
def _isolated_database_claims(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Provider tests run in-process; never open or clean the deployment registry."""
    monkeypatch.setattr(activation, "DATABASE_CLAIMS_DIR", tmp_path / "claims")


@pytest.fixture(autouse=True)
def _resolve_test_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give the clinic-os.dev fixture hosts a deterministic address.

    The endpoint-ownership check expands every dial target through the
    system resolver, so in-process activation needs ``*.clinic-os.dev``
    to resolve without depending on public DNS: the fixture maps it to
    the TEST-NET-1 documentation address.
    """
    real_getaddrinfo = socket.getaddrinfo

    def _resolve(
        host: bytes | str | None,
        port: bytes | str | int | None = 0,
        *args: int,
        **kwargs: int,
    ) -> Sequence[tuple[object, ...]]:
        if isinstance(host, str) and host.endswith(".clinic-os.dev"):
            host = "192.0.2.40"
        return real_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", _resolve)

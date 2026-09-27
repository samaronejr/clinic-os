"""Fixtures for the exemption-probe certifier: one seeded probe world per
session, a fresh clone of it per test (identity/probe_worlds.py)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from identity import probe_worlds

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator
    from pathlib import Path

    from rbac_fixtures import RbacGraph

_WORLD = pytest.StashKey[probe_worlds.WorldFactory]()


@pytest.fixture(scope="session")
def probe_world_secrets(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """Where the session world's KEK lives; the template goes with it."""
    yield tmp_path_factory.mktemp("probe-world-secrets")
    probe_worlds.drop_template()


@pytest.fixture
def seeded_world(
    request: pytest.FixtureRequest,
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    probe_world_secrets: Path,
) -> Iterator[probe_worlds.SeededWorld]:
    """Fresh clones of the session's seeded probe world for this test."""
    factory = probe_worlds.WorldFactory(rbac_graph, monkeypatch, probe_world_secrets)
    request.node.stash[_WORLD] = factory
    try:
        yield factory
    finally:
        factory.close()


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_teardown(item: pytest.Item) -> Generator[None, None, None]:
    """Close the test's world before any other teardown: the audit-trigger
    guard (tests/conftest.py) and the flush act on the connection's
    database, which must be the test database again, not a world clone."""
    factory = item.stash.get(_WORLD, None)
    if factory is not None:
        factory.close()
    return (yield)

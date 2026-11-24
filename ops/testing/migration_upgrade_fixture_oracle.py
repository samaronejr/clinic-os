"""Shared verdict and mutation-sweep fixture assertions."""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

from ops.testing.migration_upgrade_ai_intent import verify_ai_counts
from ops.testing.migration_upgrade_ai_invocation_verify import verify_invocation_fixture
from ops.testing.migration_upgrade_ai_values import verify_ai_literal_values
from ops.testing.migration_upgrade_core_verify import verify_core_fixture
from ops.testing.migration_upgrade_inbox_fixture_verify import (
    InboxFixtureResult,
    verify_inbox_fixture,
)
from ops.testing.migration_upgrade_native_effects import verify_native_effects

if TYPE_CHECKING:
    from collections.abc import Collection

    from psycopg import Connection


class FixtureResult(NamedTuple):
    """Fixture failures and resolved handles for further behavioral probes."""

    failures: list[str]
    ai_keys: dict[str, object]
    inbox: InboxFixtureResult | None


def check_fixtures(
    required: Collection[str], *, database: Connection[tuple[object, ...]] | None = None
) -> FixtureResult:
    """Check the fixture before any behavior probes modify it."""
    failures = verify_core_fixture(database=database)
    if not failures:
        failures.extend(verify_native_effects(required, database=database))
    ai_keys: dict[str, object] = {}
    if "ai" in required:
        failures.extend(
            verify_ai_counts(with_inbox="inbox" in required, database=database)
        )
        failures.extend(verify_invocation_fixture(database=database))
        failures.extend(verify_ai_literal_values(probe_keys=ai_keys, database=database))
    inbox = verify_inbox_fixture(database=database) if "inbox" in required else None
    if inbox is not None:
        failures.extend(inbox.failures)
    return FixtureResult(failures, ai_keys, inbox)

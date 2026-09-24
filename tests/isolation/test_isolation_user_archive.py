from __future__ import annotations

from typing import TYPE_CHECKING

from ops.testing.isolation_archive import archive_rollover
from ops.testing.isolation_common import load_json

from isolation.isolation_archive_fixtures import closed_user_archive_fixture
from isolation.isolation_rejection_fixtures import empty_inventory

if TYPE_CHECKING:
    from pathlib import Path


def test_archive_rollover_binds_complete_user_authorization_chain(
    tmp_path: Path,
) -> None:
    # Given: one closed USER repair rejection with immutable authorization evidence.
    fixture = closed_user_archive_fixture(tmp_path)
    ledger, _ = load_json(fixture.ledger_path)
    close_context = ledger["rejection_close"]
    assert isinstance(close_context, dict)

    # When: archive rollover validates and relocates the complete USER attempt.
    bundle = archive_rollover(
        fixture.ledger_path,
        fixture.request,
        inventory_reader=empty_inventory,
    )

    # Then: the tombstone binds USER intent, current binding, and terminal proof.
    tombstone, _ = load_json(bundle / "rejection.json")
    assert tombstone["rejections"] == [
        {"failure_class": "user-requested-fix", "lane": "USER"}
    ]
    assert tombstone["rejecting_lanes"] == ["USER"]
    assert tombstone["retry_kind"] == "source-fix"
    assert tombstone["failure_receipts_sha256"] is None
    assert (
        tombstone["user_fix_intent_sha256"] == close_context["user_fix_intent_sha256"]
    )
    assert (
        tombstone["user_fix_binding_sha256"] == close_context["user_fix_binding_sha256"]
    )
    assert (
        tombstone["terminal_revalidation_sha256"]
        == close_context["terminal_revalidation_sha256"]
    )

"""Real forward/backward/forward proof for the metadata-only survivor guards."""

from __future__ import annotations

from typing import Final

import pytest
from django.db import connection

from patients.test_intake_migrations import (
    _default_connection,
    _migrate,
    _scratch_database,
)

PRIOR: Final = ("intake", "0024_document_decrypt_context")
CURRENT: Final = ("intake", "0025_survivor_confirmations")


def _catalog() -> tuple[str, str, str]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_get_functiondef("
            "'clinic_app.intake_survivor_write_guard()'::regprocedure), "
            "pg_get_functiondef("
            "'clinic_app.intake_merge_transfer_guard()'::regprocedure), "
            "pg_get_triggerdef(t.oid) FROM pg_trigger t "
            "WHERE t.tgrelid='clinic_app.scheduling_appointment'::regclass "
            "AND t.tgname='zz_intake_survivor_binding'"
        )
        row = cursor.fetchone()
    assert row is not None
    return str(row[0]), str(row[1]), str(row[2])


@pytest.mark.django_db(transaction=True)
def test_survivor_confirmation_reverse_reapply_restores_exact_catalog(
    superuser_database_url: str,
) -> None:
    # Given: a fresh real database that has never applied intake.0025.
    with (
        _scratch_database(superuser_database_url) as scratch,
        _default_connection(scratch),
    ):
        _migrate([PRIOR])
        prior = _catalog()
        assert "BEFORE INSERT ON" in prior[2]

        # When: advance, reverse, and advance the trigger-only migration.
        _migrate([CURRENT])
        forward = _catalog()
        assert forward[0] != prior[0]
        assert forward[1] != prior[1]
        assert "BEFORE INSERT OR UPDATE OF status ON" in forward[2]
        _migrate([PRIOR])

        # Then: both prior function bodies and the INSERT-only trigger are exact.
        assert _catalog() == prior
        _migrate([CURRENT])
        assert _catalog() == forward

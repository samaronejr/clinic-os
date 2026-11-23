"""Keep stored patient envelopes out of the SQL sent by an agenda GET."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING

import pytest
from apps.core.secrets import secret_store
from apps.tenancy.envelope import KEK_SECRET_NAME, EnvelopeFormatError, protect
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext

from identity.permission_support import owner_context
from otp_test_support import runtime_role
from scheduling.test_agenda_board import LATER_NAMES, _board, _book_later_names, _world

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def _contains_envelope(value: object, envelopes: tuple[bytes, ...]) -> bool:
    if isinstance(value, str):
        text = value.lower()
        return any(envelope.hex() in text for envelope in envelopes)
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        return any(
            envelope in raw or envelope.hex().encode() in raw.lower()
            for envelope in envelopes
        )
    if isinstance(value, Mapping):
        return any(_contains_envelope(item, envelopes) for item in value.values())
    if isinstance(value, Sequence):
        return any(_contains_envelope(item, envelopes) for item in value)
    return False


def test_board_statements_never_carry_patient_name_envelopes(
    rbac_graph: RbacGraph,
) -> None:
    client, _, setup, _ = _world(rbac_graph)
    _book_later_names(setup)
    url = _board(setup.clinic_id, "list")
    with (
        transaction.atomic(),
        owner_context(setup.organization_id),
        connection.cursor() as cursor,
    ):
        wrong = protect(purpose="synthetic.other_field", plaintext=b"Sintetico")
        cursor.execute(
            "SELECT p.id, p.full_name FROM clinic_app.intake_patient p "
            "JOIN clinic_app.scheduling_appointment a ON a.patient_id = p.id "
            "WHERE a.clinic_id = %s ORDER BY a.start_at, a.practitioner_id, a.id",
            [setup.clinic_id],
        )
        stored = cursor.fetchall()
    assert len(stored) == 3
    envelopes = (*(bytes(envelope) for _, envelope in stored), wrong)
    assert len(set(envelopes)) == 4
    sensitive = (*envelopes, bytes.fromhex(secret_store().get_secret(KEK_SECRET_NAME)))

    bound_envelope = False

    def inspect_binds(
        execute: Callable[..., object],
        sql: str,
        params: object,
        many: bool,
        context: dict[str, object],
    ) -> object:
        nonlocal bound_envelope
        if _contains_envelope(sql, sensitive) or _contains_envelope(params, sensitive):
            bound_envelope = True
        return execute(sql, params, many, context)

    with (
        runtime_role(),
        CaptureQueriesContext(connection) as success,
        connection.execute_wrapper(inspect_binds),
    ):
        page = client.get(url)
    assert page.status_code == 200
    for name in ("Synthetic Booking Persona", *LATER_NAMES):
        assert name.encode() in page.content
    assert any("clinic_app.protected_decrypt(" in query["sql"] for query in success)
    success_text = any(_contains_envelope(query["sql"], sensitive) for query in success)
    success_binds = bound_envelope
    bound_envelope = False

    # Fault exactly one stored member, not the request, logger, or resolver.
    with (
        transaction.atomic(),
        owner_context(setup.organization_id),
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "ALTER TABLE clinic_app.intake_patient "
            "DISABLE TRIGGER intake_patient_mirror_guard"
        )
        cursor.execute(
            "UPDATE clinic_app.intake_patient SET full_name = %s WHERE id = %s",
            [wrong, stored[1][0]],
        )
        assert cursor.rowcount == 1
        cursor.execute(
            "ALTER TABLE clinic_app.intake_patient "
            "ENABLE TRIGGER intake_patient_mirror_guard"
        )
    with (
        runtime_role(),
        CaptureQueriesContext(connection) as failure,
        connection.execute_wrapper(inspect_binds),
        pytest.raises(EnvelopeFormatError),
    ):
        client.get(url)
    assert any("clinic_app.protected_decrypt(" in query["sql"] for query in failure)
    failure_text = any(_contains_envelope(query["sql"], sensitive) for query in failure)
    if success_text or success_binds or failure_text or bound_envelope:
        # Never let pytest's assertion introspection print any captured SQL.
        pytest.fail(
            "Board SQL carried protected material: "
            f"success text/binds={success_text}/{success_binds}, "
            f"failure text/binds={failure_text}/{bound_envelope}",
            pytrace=False,
        )

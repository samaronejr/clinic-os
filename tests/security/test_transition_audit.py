"""Transition rows whose audit append the threat models claim (todo 73 round 4)."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import psycopg
import pytest
from apps.billing.payouts import approve_payout, request_payout, transition_payout
from apps.billing.refunds import approve_refund, request_refund, transition_refund
from apps.billing.security import finance_session
from apps.interop import services

from billing.journal_support import (
    allocated,
    finance_scope,
    finance_setup,
    fresh_finance_session,
    invoice_terms,
)
from interop.support import committed, import_world, operating, patients_csv

if TYPE_CHECKING:
    from billing.journal_support import FinanceSetup
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ("finance_setup",)
PAYLOAD_KEYS = {
    "clinic_id",
    "http_method",
    "http_status",
    "object_verb",
    "reason_code",
    "request_id",
}


def _events(
    dsn: str, prefix: str, record: object
) -> list[tuple[str, dict[str, object]]]:
    with psycopg.connect(dsn) as database:
        rows = database.execute(
            "SELECT event_type, payload FROM clinic_app.audit_event "
            "WHERE event_type LIKE %s AND affected_record_id = %s ORDER BY seq",
            [f"{prefix}%", str(record)],
        ).fetchall()
    return [(str(name), dict(payload)) for name, payload in rows]


def test_import_job_transitions_append_metadata_only_audit_rows(
    rbac_graph: RbacGraph,
    superuser_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given an import committed through the real job state machine ...
    world = import_world(rbac_graph)
    report, _receipt = committed(world, patients_csv(3), monkeypatch)
    job_id = report.job.job_id
    with operating(world):
        reconciliation = services.reconciliation_report(
            clinic_id=rbac_graph.clinic_a, job_id=job_id
        )
        # ... and then rolled back.
        services.rollback_import(
            clinic_id=rbac_graph.clinic_a,
            job_id=job_id,
            expected_sequence=reconciliation.job.sequence,
            reason="data_quality",
            session_key=world.session,
        )
    # Then each transition appended its registered event against the job, in
    # order, with metadata-only payloads.
    events = _events(superuser_database_url, "interop.job.", job_id)
    names = [name for name, _payload in events]
    assert names[0] == "interop.job.uploaded"
    assert {"interop.job.committed", "interop.job.rolled_back"} <= set(names)
    assert names.index("interop.job.committed") < names.index("interop.job.rolled_back")
    for _name, payload in events:
        assert set(payload) <= PAYLOAD_KEYS
        assert payload["clinic_id"] == str(rbac_graph.clinic_a)


def test_refund_and_payout_transitions_append_metadata_only_audit_rows(
    finance_setup: FinanceSetup, superuser_database_url: str
) -> None:
    # Given a refund and a payout driven through request, step-up approval and send.
    clinic = finance_setup.clinic_id
    terms = invoice_terms(finance_setup)
    key = fresh_finance_session(finance_setup)
    with finance_scope(finance_setup):
        allocation = allocated(finance_setup, terms, 1000)
        refund = request_refund(
            clinic_id=clinic,
            allocation_id=allocation.pk,
            amount_minor=100,
            command_id=uuid4(),
        )
        payout = request_payout(
            clinic_id=clinic,
            practitioner_id=finance_setup.graph.physician,
            amount_minor=300,
            command_id=uuid4(),
        )
        with finance_session(clinic_id=clinic, session_key=key):
            approve_refund(clinic_id=clinic, refund_id=refund.pk)
            transition_refund(
                clinic_id=clinic, refund_id=refund.pk, next_state="sending"
            )
            approve_payout(clinic_id=clinic, payout_id=payout.pk)
            transition_payout(
                clinic_id=clinic, payout_id=payout.pk, next_state="sending"
            )
    # Then every transition appended its registered event against its record.
    for prefix, record in (
        ("billing.refund.", refund.pk),
        ("billing.payout.", payout.pk),
    ):
        events = _events(superuser_database_url, prefix, record)
        assert [name.removeprefix(prefix) for name, _payload in events] == [
            "requested",
            "approved",
            "sending",
        ]
        for _name, payload in events:
            assert set(payload) <= PAYLOAD_KEYS
            assert payload["clinic_id"] == str(clinic)

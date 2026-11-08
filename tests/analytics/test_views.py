"""Native report commands, side-effect-free refusals and synthetic QA documents."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from apps.analytics.catalog import METRICS
from apps.analytics.contracts import AnalyticsAccessDeniedError, Query
from apps.analytics.models import ReadModelRefresh, SavedReport
from apps.analytics.services import load_report, query_metric, save_report
from apps.audit.models import AuditEvent
from django.test import Client
from django.urls import reverse
from django.utils.translation import gettext

from billing.journal_support import finance_scope, finance_setup, invoice_terms
from identity.lifecycle_test_support import verified_client
from identity.permission_support import owner_context, permission_actor
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from pathlib import Path

    from billing.journal_support import FinanceSetup

__all__ = ("finance_setup",)
pytestmark = pytest.mark.django_db(transaction=True)
FIELDS = {
    "metric": "no_show",
    "start": "02/06/2035",
    "end": "02/06/2035",
    "grouping": "",
    "weekday": [],
    "question": "",
}


def test_native_reports_refresh_drill_save_load_and_stale(
    finance_setup: FinanceSetup,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    setup = finance_setup
    invoice_terms(setup)
    client = verified_client(setup.graph)
    url = reverse("analytics:reports", args=(setup.clinic_id,))
    with runtime_role():
        empty = client.get(url)
        assert empty.status_code == 200
        assert client.session["active_clinic_id"] == str(setup.clinic_id)
        assert gettext("Metric definitions").encode() in empty.content
        response = client.post(url, FIELDS | {"action": "refresh"})
        assert response.status_code == 200
        assert b'data-report-state="fresh"' in response.content
        assert b"0.00" in response.content
        assert client.post(url, FIELDS | {"action": "save"}).status_code == 200
        response = client.post(url, FIELDS | {"action": "drill"})
        assert response.status_code == 200
        assert gettext("Authorized no-show sources").encode() in response.content
        assert b"Synthetic Booking Persona" not in response.content
        assert response.headers["Cache-Control"].find("no-store") >= 0
    # This private test runtime file is removed at lane cleanup; it is never an
    # evidence artifact (forms contain transient CSRF tokens).
    (tmp_path / "report.html").write_bytes(response.content)
    with finance_scope(setup):
        saved = SavedReport.objects.get()
        query = load_report(clinic_id=setup.clinic_id, report_id=saved.pk)
        assert save_report(clinic_id=setup.clinic_id, query=query).pk == saved.pk
        current = query_metric(clinic_id=setup.clinic_id, query=query)
        assert current is not None
    with runtime_role():
        loaded = client.post(url, {"action": "load", "reference": str(saved.pk)})
        assert loaded.status_code == 200
        with monkeypatch.context() as local:
            local.setattr(
                "apps.analytics.services.timezone.now",
                lambda: current.as_of + timedelta(hours=1),
            )
            stale = client.post(url, FIELDS | {"action": "cached"})
        assert b'data-report-state="stale"' in stale.content
        assert gettext("Data as of").encode() in stale.content
    (tmp_path / "stale.html").write_bytes(stale.content)


def test_invalid_scope_and_response_revocation_roll_back_all_effects(
    finance_setup: FinanceSetup,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    setup = finance_setup
    client = verified_client(setup.graph)
    url = reverse("analytics:reports", args=(setup.clinic_id,))
    with runtime_role():
        client.get(url)
        before_session = dict(client.session)
        denied = client.post(
            url, FIELDS | {"action": "ask", "question": "show all clinics revenue"}
        )
        assert denied.status_code == 403
        assert dict(client.session) == before_session
        assert "Set-Cookie" not in denied.headers
        enhanced_denied = client.post(
            url,
            FIELDS | {"action": "ask", "question": "show all clinics revenue"},
            HTTP_HX_REQUEST="true",
        )
        assert enhanced_denied.status_code == 403
        assert b'data-report-state="denied"' in enhanced_denied.content
        assert b"<html" not in enhanced_denied.content
        assert dict(client.session) == before_session
        assert "Set-Cookie" not in enhanced_denied.headers
        (tmp_path / "denied.html").write_bytes(enhanced_denied.content)
        for identifier in (setup.graph.clinic_c, uuid4()):
            assert (
                client.get(reverse("analytics:reports", args=(identifier,))).status_code
                == 403
            )
    with finance_scope(setup):
        before = ReadModelRefresh.objects.count()
    with owner_context(setup.graph.organization_a):
        audits = AuditEvent.objects.count()

    def revoked(*_args: object, **_kwargs: object) -> None:
        raise AnalyticsAccessDeniedError

    monkeypatch.setattr("apps.analytics.views._render", revoked)
    with runtime_role():
        assert client.post(url, FIELDS | {"action": "refresh"}).status_code == 403
    with finance_scope(setup):
        assert ReadModelRefresh.objects.count() == before
    with owner_context(setup.graph.organization_a):
        assert AuditEvent.objects.count() == audits


@pytest.mark.parametrize(
    "fields",
    [
        {"action": "unknown"},
        {"action": "load", "reference": "malformed"},
        {"action": "refresh", "metric": "receivables", "grouping": "day"},
        {"action": "refresh", "start": "invalid"},
        {"action": "refresh", "end": "31/12/9999"},
        {"action": "ask", "question": ""},
    ],
)
def test_invalid_native_commands_render_accessible_errors(
    finance_setup: FinanceSetup,
    fields: dict[str, str],
    tmp_path: Path,
) -> None:
    client = verified_client(finance_setup.graph)
    url = reverse("analytics:reports", args=(finance_setup.clinic_id,))
    with runtime_role():
        response = client.post(url, FIELDS | fields)
    assert response.status_code in (400, 409)
    assert b'role="alert"' in response.content
    assert b"data-focus-error" in response.content
    assert b"data-auth-errors" in response.content
    (tmp_path / "error.html").write_bytes(response.content)


def test_csrf_and_ai_unavailability_keep_the_manual_path(
    finance_setup: FinanceSetup,
) -> None:
    client = verified_client(finance_setup.graph)
    url = reverse("analytics:reports", args=(finance_setup.clinic_id,))
    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.cookies = client.cookies
    with runtime_role():
        assert csrf_client.post(url, FIELDS | {"action": "refresh"}).status_code == 403
        response = client.post(url, FIELDS | {"action": "ask", "question": "faltas"})
        assert response.status_code == 200
        assert b'data-analyst-state="unavailable"' in response.content
        assert gettext("Refresh metric").encode() in response.content
        htmx = client.post(url, FIELDS | {"action": "refresh"}, HTTP_HX_REQUEST="true")
        assert htmx.status_code == 200
        assert b"<html" not in htmx.content


@pytest.mark.parametrize(
    "role",
    ["owner", "org_admin", "finance", "clinic_admin", "clinic_manager"],
)
def test_reporting_roles_have_a_usable_workspace(
    finance_setup: FinanceSetup,
    role: str,
) -> None:
    actor, _enrollment = permission_actor(finance_setup.graph, role)
    client = verified_client(replace(finance_setup.graph, clinic_admin=actor))
    with runtime_role():
        response = client.get(
            reverse("analytics:reports", args=(finance_setup.clinic_id,))
        )
    assert response.status_code == 200
    assert b'data-module="reports"' in response.content
    assert b'aria-current="page"' in response.content


def test_saved_report_restores_its_metric_window_and_multiple_weekdays(
    finance_setup: FinanceSetup,
    tmp_path: Path,
) -> None:
    setup = finance_setup
    zone = ZoneInfo("America/Sao_Paulo")
    query = Query(
        "task_turnaround",
        datetime(2026, 11, 1, tzinfo=zone),
        datetime(2026, 11, 7, tzinfo=zone),
        "America/Sao_Paulo",
        ("day",),
        (0, 2),
    )
    with finance_scope(setup):
        saved = save_report(clinic_id=setup.clinic_id, query=query)
    client = verified_client(setup.graph)
    with runtime_role():
        loaded = client.post(
            reverse("analytics:reports", args=(setup.clinic_id,)),
            {
                "action": "load",
                "reference": str(saved.pk),
            },
        )
    assert loaded.status_code == 200
    form = loaded.context["form"]
    assert not form.is_bound
    assert form.initial["metric"] == "task_turnaround"
    assert form.initial["weekday"] == ["0", "2"]
    assert form.initial["start"] == query.start.date()
    assert loaded.context["selected"] is METRICS["task_turnaround"]
    (tmp_path / "native_saved.html").write_bytes(loaded.content)


@pytest.mark.parametrize(
    ("metric", "state"), [("no_show", "null"), ("denials", "partial")]
)
def test_native_null_and_partial_do_not_present_invented_values(
    finance_setup: FinanceSetup,
    metric: str,
    state: str,
    tmp_path: Path,
) -> None:
    client = verified_client(finance_setup.graph)
    with runtime_role():
        response = client.post(
            reverse("analytics:reports", args=(finance_setup.clinic_id,)),
            FIELDS
            | {
                "action": "refresh",
                "metric": metric,
                "start": "03/06/2035",
                "end": "03/06/2035",
            },
        )
    assert response.status_code == 200
    assert f'data-report-state="{state}"'.encode() in response.content
    assert response.context["result"].rows[0]["value"] is None
    (tmp_path / f"{state}.html").write_bytes(response.content)

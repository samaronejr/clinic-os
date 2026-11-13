"""Support diagnostics screen: masked aggregates under a live customer grant only."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.comms.models import IntegrationOperation
from apps.identity import enterprise_authority as clock
from apps.identity.enterprise_access import approve_support_access
from apps.identity.enterprise_support_access import revoke_support_access
from apps.identity.models import User, UserClinicRole
from apps.tenancy.db import tenant_context
from apps.workflows.models import Task
from django.db import connection
from django.test import override_settings
from django.utils import timezone

from identity.enterprise_support import enterprise_setup, verified_actor_client
from identity.permission_support import (
    owner_context,
    permission_actor,
    permission_context,
)
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from uuid import UUID

    from apps.identity.models import SupportAccessGrant
    from django.test import Client

    from identity.enterprise_support import EnterpriseSetup
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)
URL = "/support/diagnostics/"
FORBIDDEN = 403
OK = 200
PATIENT_SENTINEL = "Sintetico Permission"
ERROR_SENTINEL = "SENTINEL-OUTBOX-ERROR-7f3a"
# The masked CSRF token differs per response; the denial must not.
CSRF = re.compile(r'name="csrfmiddlewaretoken" value="[^"]+"')


class _Marks(HTMLParser):
    """Collect the data-* hooks the screen renders, never its prose."""

    def __init__(self) -> None:
        super().__init__()
        self.marks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.marks.extend(name for name, _value in attrs if name.startswith("data-"))


def _marks(body: str) -> list[str]:
    parser = _Marks()
    parser.feed(body)
    return parser.marks


def _support_client(graph: RbacGraph) -> tuple[User, Client]:
    """A named support user belongs to its own organization, never the customer's."""
    support = User.objects.create(username=f"synthetic-support-{uuid4().hex}")
    with owner_context(graph.organization_b):
        UserClinicRole.objects.create(
            organization_id=graph.organization_b,
            clinic_id=graph.clinic_c,
            user=support,
            role="clinic_admin",
        )
    return support, verified_actor_client(support.pk)


def _approve(
    graph: RbacGraph, setup: EnterpriseSetup, support: User, *, minutes: int = 30
) -> SupportAccessGrant:
    with permission_context(graph, graph.clinic_admin):
        return approve_support_access(
            clinic_id=graph.clinic_a,
            support_user_id=support.pk,
            purpose="Synthetic diagnostic for a delayed reminder",
            expires_at=timezone.now() + timedelta(minutes=minutes),
            session_key=setup.session_key,
        )


def _seed_activity(graph: RbacGraph, clinician: UUID) -> None:
    with owner_context(graph.organization_a):
        IntegrationOperation.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            actor_id=clinician,
            channel="email",
            provider="synthetic",
            subject_type="synthetic.support",
            subject_id=uuid4(),
            status="failed",
            last_error=ERROR_SENTINEL,
            idempotency_key=uuid4(),
        )
        IntegrationOperation.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            actor_id=clinician,
            channel="sms",
            provider="synthetic",
            subject_type="synthetic.support",
            subject_id=uuid4(),
            idempotency_key=uuid4(),
        )
    # The task guard accepts an author holding tasks.assign (workflow bundle v2).
    with owner_context(graph.organization_a):
        UserClinicRole.objects.filter(
            organization_id=graph.organization_a, user_id=graph.clinic_admin
        ).update(workflow_bundle_version=2)
    with runtime_role(), tenant_context(graph.clinic_admin, graph.organization_a):
        Task.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            created_by_id=graph.clinic_admin,
            kind="checklist",
            subject_ref={"kind": "clinic", "id": str(graph.clinic_a)},
            due_at=datetime(2001, 1, 1, tzinfo=UTC),
            idempotency_key=uuid4(),
            fingerprint="a" * 64,
        )


def _post(client: Client, *, clinic: object, grant: object) -> tuple[int, str]:
    with runtime_role():
        response = client.post(URL, {"clinic_id": str(clinic), "grant_id": str(grant)})
    return response.status_code, response.content.decode()


@override_settings(CLINIC_ENTERPRISE_SYNTHETIC=True)
def test_support_screen_shows_masked_aggregates_and_no_identifiers(
    rbac_graph: RbacGraph,
) -> None:
    # Given a patient, a failed outbox row with error text, an overdue task and
    # a customer-approved grant for a separate named support user.
    setup = enterprise_setup(rbac_graph)
    clinician, enrollment = permission_actor(rbac_graph, "physician")
    support, client = _support_client(rbac_graph)
    grant = _approve(rbac_graph, setup, support)
    _seed_activity(rbac_graph, clinician)

    # When that support user opens the screen with selectors in the POST body.
    status, body = _post(client, clinic=rbac_graph.clinic_a, grant=grant.pk)

    # Then only counts and closed labels appear: no names, ids or error text.
    assert status == OK
    marks = _marks(body)
    assert "data-support-ready" in marks
    assert marks.count("data-support-queue") == 2
    assert marks.count("data-support-task") == 1
    assert "data-support-provider" in marks
    assert "data-support-staff" in marks
    for sentinel in (
        PATIENT_SENTINEL,
        ERROR_SENTINEL,
        str(enrollment),
        str(clinician),
        str(support.pk),
        str(grant.pk),
        support.username,
        "Synthetic diagnostic for a delayed reminder",
    ):
        assert sentinel not in body


@override_settings(CLINIC_ENTERPRISE_SYNTHETIC=True)
def test_expired_revoked_foreign_and_malformed_grants_share_one_denial(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given one live grant and a second grant that is revoked.
    setup = enterprise_setup(rbac_graph)
    support, client = _support_client(rbac_graph)
    grant = _approve(rbac_graph, setup, support, minutes=10)
    revoked = _approve(rbac_graph, setup, support)
    with permission_context(rbac_graph, rbac_graph.clinic_admin):
        revoke_support_access(
            clinic_id=rbac_graph.clinic_a,
            grant_id=revoked.pk,
            session_key=setup.session_key,
        )
    refusals = [
        _post(client, clinic=rbac_graph.clinic_a, grant=revoked.pk),
        _post(client, clinic=rbac_graph.clinic_b, grant=grant.pk),
        _post(client, clinic=rbac_graph.clinic_a, grant=uuid4()),
        _post(client, clinic="not-a-uuid", grant=grant.pk),
    ]
    # When the clock reaches the grant's exclusive expiry.
    monkeypatch.setattr(clock, "utc_now", lambda: grant.expires_at)
    refusals.append(_post(client, clinic=rbac_graph.clinic_a, grant=grant.pk))

    # Then every refusal is the same page with no diagnostic content.
    assert {status for status, _body in refusals} == {FORBIDDEN}
    assert len({CSRF.sub("", body) for _status, body in refusals}) == 1
    marks = _marks(refusals[0][1])
    assert "data-support-denied" in marks
    assert "data-support-ready" not in marks


@override_settings(CLINIC_ENTERPRISE_SYNTHETIC=True)
def test_another_users_session_or_no_session_cannot_use_the_grant(
    rbac_graph: RbacGraph, client: Client
) -> None:
    # Given a grant named for one support user.
    setup = enterprise_setup(rbac_graph)
    support, _support = _support_client(rbac_graph)
    grant = _approve(rbac_graph, setup, support)
    _other, other_client = _support_client(rbac_graph)

    # When an anonymous browser and a different verified user present it.
    anonymous = _post(client, clinic=rbac_graph.clinic_a, grant=grant.pk)
    other = _post(other_client, clinic=rbac_graph.clinic_a, grant=grant.pk)

    # Then both are refused before any diagnostic is read.
    assert anonymous[0] == other[0] == FORBIDDEN
    assert "data-support-ready" not in _marks(anonymous[1] + other[1])


@override_settings(CLINIC_ENTERPRISE_SYNTHETIC=True)
def test_support_user_cannot_open_the_patient_chart_or_staff_pages(
    rbac_graph: RbacGraph,
) -> None:
    # Given a live support grant and a real patient enrollment in that clinic.
    setup = enterprise_setup(rbac_graph)
    _clinician, enrollment = permission_actor(rbac_graph, "physician")
    support, client = _support_client(rbac_graph)
    grant = _approve(rbac_graph, setup, support)
    assert _post(client, clinic=rbac_graph.clinic_a, grant=grant.pk)[0] == OK

    # When the support session asks for the patient chart and staff screens.
    with runtime_role():
        responses = [
            client.post(
                f"/ehr/clinics/{rbac_graph.clinic_a}/patient-workspace/",
                {"enrollment_id": str(enrollment)},
            ),
            client.get(f"/intake/clinics/{rbac_graph.clinic_a}/patients/"),
            client.get(f"/clinics/{rbac_graph.clinic_a}/settings/governance/"),
        ]

    # Then nothing clinical is served and the patient's name never appears.
    assert all(response.status_code != OK for response in responses)
    assert all(
        PATIENT_SENTINEL not in response.content.decode() for response in responses
    )


@override_settings(CLINIC_ENTERPRISE_SYNTHETIC=True)
def test_overview_resolver_refuses_without_the_installed_grant(
    rbac_graph: RbacGraph,
) -> None:
    # Given a valid grant and the support user's verified session hash.
    from apps.identity.session_inventory import session_hash  # noqa: PLC0415

    from identity.lifecycle_test_support import session_key  # noqa: PLC0415

    setup = enterprise_setup(rbac_graph)
    support, client = _support_client(rbac_graph)
    grant = _approve(rbac_graph, setup, support)
    arguments: list[str | bytes] = [
        str(grant.pk),
        str(rbac_graph.clinic_a),
        session_hash(session_key(client)),
    ]

    # When runtime SQL calls the resolver without the service's grant GUC.
    with runtime_role(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM clinic_app.enterprise_support_overview(%s,%s,%s)",
            arguments,
        )
        rows = cursor.fetchone()

    # Then the resolver returns nothing at all.
    assert rows == (0,)
    # And a forged grant GUC with another session's hash still reads nothing.
    with runtime_role(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.current_support_grant',%s,false)", [str(grant.pk)]
        )
        try:
            cursor.execute(
                "SELECT count(*) FROM clinic_app.enterprise_support_overview(%s,%s,%s)",
                [arguments[0], arguments[1], session_hash("forged-session-key")],
            )
            forged = cursor.fetchone()
        finally:
            cursor.execute("SELECT set_config('app.current_support_grant','',false)")
    assert forged == (0,)


def test_get_renders_only_the_selector_form() -> None:
    from django.test import Client  # noqa: PLC0415

    # Selectors in the query string are ignored: GET never reads a grant.
    with runtime_role():
        response = Client().get(f"{URL}?clinic_id={uuid4()}&grant_id={uuid4()}")

    assert response.status_code == OK
    marks = _marks(response.content.decode())
    assert "data-support-diagnostics" in marks
    assert "data-support-ready" not in marks
    assert "data-support-denied" not in marks


@override_settings(CLINIC_ENTERPRISE_SYNTHETIC=True)
def test_cross_site_post_without_csrf_is_refused(rbac_graph: RbacGraph) -> None:
    # Given a live grant and the named support user's verified session.
    setup = enterprise_setup(rbac_graph)
    support, client = _support_client(rbac_graph)
    grant = _approve(rbac_graph, setup, support)
    client.handler.enforce_csrf_checks = True

    # When a cross-site form posts the selectors without the CSRF token.
    status, body = _post(client, clinic=rbac_graph.clinic_a, grant=grant.pk)

    # Then Django refuses it before the view reads anything.
    assert status == FORBIDDEN
    assert "data-support-ready" not in _marks(body)

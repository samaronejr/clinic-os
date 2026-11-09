"""Emergency request, scoped read and patient access-log pages over real sessions."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.identity.enterprise_access import EmergencyScope, grant_break_glass
from apps.identity.models import BreakGlassGrant
from django.test import override_settings
from django.utils import timezone

from identity.enterprise_support import enterprise_setup, verified_actor_client
from identity.lifecycle_test_support import session_key
from identity.permission_support import (
    owner_context,
    permission_actor,
    permission_context,
)
from patient_service_support import runtime_role
from renewal.test_patient_access import _issue, _patient_client, _redeem

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)
ACCESS_LOG = "/patient/security/access-log/"


@override_settings(CLINIC_ENTERPRISE_SYNTHETIC=True)
def test_emergency_pages_grant_and_read_only_under_break_glass_authority(
    rbac_graph: RbacGraph,
) -> None:
    # Given a verified clinician, another admitted clinician whose patient's
    # enrollment is requested, and a role that holds no break-glass permission.
    enterprise_setup(rbac_graph)
    clinician, _own = permission_actor(rbac_graph, "physician")
    other, enrollment = permission_actor(rbac_graph, "physician")
    receptionist, _patient = permission_actor(rbac_graph, "receptionist")
    clinician_client = verified_actor_client(clinician)
    other_client = verified_actor_client(other)
    receptionist_client = verified_actor_client(receptionist)
    page = f"/clinics/{rbac_graph.clinic_a}/account/emergency/"
    reason = {"reason": "Synthetic emergency", "minutes": 30}
    # When the clinician requests access, scope and reason in the body, after a
    # blank reason and an unknown enrollment that the view itself refuses.
    with runtime_role():
        shown = clinician_client.get(page)
        blank = clinician_client.post(
            page, {"enrollment_id": enrollment, "reason": "   ", "minutes": 30}
        )
        unknown = clinician_client.post(page, {"enrollment_id": uuid4(), **reason})
        submitted = clinician_client.post(page, {"enrollment_id": enrollment, **reason})
    assert shown.status_code == 200
    assert (blank.status_code, unknown.status_code) == (400, 400)
    assert submitted.status_code == 200
    assert submitted.context["granted"] is True
    with owner_context(rbac_graph.organization_a):
        # Exactly one grant: neither refused request wrote one.
        grant = BreakGlassGrant.objects.get(clinician_id=clinician)
    assert grant.enrollment_id == enrollment
    # Then only the grant's own clinician reads through it: the view refuses
    # another admitted clinician, the registry refuses the receptionist.
    with runtime_role():
        read = clinician_client.post(f"{page}read/", {"grant_id": grant.pk})
        admitted = other_client.get(page)
        foreign = other_client.post(f"{page}read/", {"grant_id": grant.pk})
        refused = receptionist_client.get(page)
        borrowed = receptionist_client.post(f"{page}read/", {"grant_id": grant.pk})
    assert read.status_code == 200
    assert read.context["snapshot"].enrollment_id == enrollment
    assert "no-store" in read.headers["Cache-Control"]
    assert admitted.status_code == 200
    assert foreign.status_code == 403
    assert refused.status_code == 403
    assert borrowed.status_code == 403


@override_settings(CLINIC_ENTERPRISE_SYNTHETIC=True)
def test_patient_access_log_lists_the_emergency_read_without_its_reason(
    rbac_graph: RbacGraph,
) -> None:
    # Given an emergency grant over one enrollment and that patient's session.
    enterprise_setup(rbac_graph)
    clinician, _own = permission_actor(rbac_graph, "physician")
    _other, enrollment = permission_actor(rbac_graph, "physician")
    clinician_client = verified_actor_client(clinician)
    with permission_context(rbac_graph, clinician):
        grant_break_glass(
            clinic_id=rbac_graph.clinic_a,
            scope=EmergencyScope(enrollment),
            reason="SINTETICO-SENTINELA-access-log-reason",
            expires_at=timezone.now() + timedelta(hours=1),
            session_key=session_key(clinician_client),
        )
    _grant_id, secret = _issue(rbac_graph, enrollment)
    patient_session = _redeem(rbac_graph.clinic_a, secret)
    assert patient_session is not None
    patient = _patient_client(patient_session)
    # When the patient opens the log and pages with a malformed selector.
    with runtime_role():
        listed = patient.get(ACCESS_LOG)
        malformed = patient.post(ACCESS_LOG, {"page": "x"})
        staff = clinician_client.get(ACCESS_LOG)
    # Then exactly the one unreviewed entry shows, and never its reason.
    assert listed.status_code == 200
    (entry,) = listed.context["entries"]
    assert entry["reviewed"] is False
    assert b"SINTETICO-SENTINELA" not in listed.content
    assert malformed.status_code == 400
    assert staff.status_code == 403

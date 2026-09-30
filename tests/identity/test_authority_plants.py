"""Round-7 plants must be refused before any differential backstop executes."""

from __future__ import annotations

import sys
from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.identity.models import (
    CareTeamMembership,
    ProfessionalRegistration,
    RoleGrant,
    User,
    UserClinicRole,
)
from django.utils import timezone

from identity import nonstaff_differential
from identity.authority_observer import AuthorityObservedError
from identity.nonstaff_differential import (
    DifferentialProbe,
    _invoke,
    assert_behavioral_classifications,
)
from identity.nonstaff_states import CareScope, ReplayScope
from identity.permission_support import owner_context, permission_actor
from identity.test_nonstaff_differential import SPELLINGS, SYMBOL, load_reproducer

if TYPE_CHECKING:
    from pathlib import Path

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)
HEADER = "from apps.identity import current_context as _cc\nENROLLMENT = None\n"
PLANTS = {
    "registration": HEADER
    + """
def require_future_owner(*, clinic_id):
    check = getattr(_cc, "require_" + "permission")
    try:
        check("clinical.read", clinic_id=clinic_id, patient_enrollment_id=ENROLLMENT)
    except Exception:
        return True
    return False
""",
    "rolegrant": HEADER
    + """
def _perm(name, clinic_id):
    check = getattr(_cc, "require_" + "permission")
    try:
        check(name, clinic_id=clinic_id)
        return True
    except Exception:
        return False

def require_future_owner(*, clinic_id):
    return not (_perm("staff.organization", clinic_id)
                and not _perm("appointment.read", clinic_id))
""",
}


def _backstop_is_not_the_certificate(*args: object, **kwargs: object) -> object:
    message = "observer failed to refuse before the backstop"
    raise RuntimeError(message)


@pytest.mark.parametrize("plant", PLANTS)
@pytest.mark.parametrize("kind", ["nonstaff", "infrastructure"])
def test_registration_and_rolegrant_plants_are_observed_without_authority_rows(
    plant: str,
    kind: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rbac_graph: RbacGraph,
) -> None:
    monkeypatch.setitem(SPELLINGS, "r7", PLANTS[plant])
    guard = load_reproducer("r7", tmp_path, monkeypatch)
    _, enrollment = permission_actor(rbac_graph, "unassigned")
    _, other = permission_actor(rbac_graph, "unassigned")
    monkeypatch.setattr(
        sys.modules["apps.example.reviewer_role_guard"], "ENROLLMENT", enrollment
    )
    actor = User.objects.create(username="synthetic-r7-" + uuid4().hex)
    probe = DifferentialProbe(SYMBOL, lambda: guard(clinic_id=rbac_graph.clinic_a))
    scope = ReplayScope(
        rbac_graph.clinic_a,
        rbac_graph.organization_a,
        other_clinic=rbac_graph.clinic_b,
        care=CareScope(enrollment, other),
    )
    assert _invoke(probe, actor, scope.organization) is True
    with owner_context(scope.organization):
        assert not RoleGrant.objects.exists()
        assert not ProfessionalRegistration.objects.filter(user=actor).exists()
    monkeypatch.setattr(
        nonstaff_differential, "_replay", _backstop_is_not_the_certificate
    )
    with pytest.raises(AuthorityObservedError) as refused:
        assert_behavioral_classifications(
            [{"symbol": SYMBOL, "kind": kind, "signals": [], "differential": False}],
            {SYMBOL: [probe]},
            actor=actor,
            scope=scope,
        )
    assert (
        "python",
        "apps.identity.current_context.require_permission",
    ) in refused.value.args[1]
    # Preserve the reviewer's behavioural witness: the same request flips only
    # after the missing authority row is installed. Observation rejected earlier.
    now = timezone.now()
    with owner_context(scope.organization):
        UserClinicRole.objects.create(
            user=actor,
            organization_id=scope.organization,
            clinic_id=scope.clinic,
            role="physician" if plant == "registration" else "owner",
        )
        if plant == "registration":
            ProfessionalRegistration.objects.create(
                organization_id=scope.organization,
                clinic_id=scope.clinic,
                user=actor,
                role="physician",
                council="CRM",
                number="SINTETICO-R7",
                jurisdiction="SP",
                specialty="Sintetico",
                status="regular",
                valid_from=now - timedelta(days=1),
                valid_to=now + timedelta(days=1),
            )
            CareTeamMembership.objects.create(
                organization_id=scope.organization,
                clinic_id=scope.clinic,
                patient_enrollment_id=enrollment,
                user=actor,
                role="physician",
                valid_from=now - timedelta(days=1),
                valid_to=now + timedelta(days=1),
            )
        else:
            RoleGrant.objects.create(
                organization_id=scope.organization,
                clinic_id=scope.clinic,
                role="owner",
                permission="appointment.read",
                valid_from=now - timedelta(days=1),
            )
    assert _invoke(probe, actor, scope.organization) is False

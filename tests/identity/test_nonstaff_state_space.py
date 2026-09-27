"""The decision domain includes all role subsets, scope and care revocation."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.identity.models import User

from identity.nonstaff_differential import (
    DifferentialProbe,
    StaffDependentError,
    assert_behavioral_classifications,
)
from identity.nonstaff_states import (
    CARE_LIFECYCLES,
    CARE_PATIENT_SCOPES,
    CLINIC_LAYOUTS,
    ROLE_SUBSETS,
    ROLES,
    CareScope,
    ReplayScope,
    assert_membership_schema,
    profiles,
)
from identity.permission_support import permission_actor
from identity.test_nonstaff_differential import SPELLINGS, SYMBOL, load_reproducer

if TYPE_CHECKING:
    from pathlib import Path

    from identity.guard_classification import Candidate
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_role_domain_is_the_complete_model_power_set() -> None:
    assert_membership_schema()
    assert len(ROLE_SUBSETS) == 2 ** len(ROLES)
    assert len(set(ROLE_SUBSETS)) == len(ROLE_SUBSETS)
    masks = {sum(1 << ROLES.index(role) for role in subset) for subset in ROLE_SUBSETS}
    assert masks == set(range(2 ** len(ROLES)))
    assert () in ROLE_SUBSETS
    assert tuple(ROLES) in ROLE_SUBSETS


def test_scope_and_care_dimensions_are_crossed_with_every_subset() -> None:
    scope = CareScope(uuid4(), uuid4())
    actual = tuple(profiles(uuid4(), scope))
    expected = {(layout, "absent", "target") for layout in CLINIC_LAYOUTS} | {
        (layout, life, patient)
        for layout in CLINIC_LAYOUTS
        for life in CARE_LIFECYCLES
        for patient in CARE_PATIENT_SCOPES
    }
    assert set(actual) == expected
    assert len(actual) == len(CLINIC_LAYOUTS) * (
        1 + len(CARE_LIFECYCLES) * len(CARE_PATIENT_SCOPES)
    )
    assert len(actual) * len(ROLE_SUBSETS) == 27648


@pytest.mark.parametrize(
    "roles", [("owner", "physician"), ("owner", "physician", "clinic_admin")]
)
def test_combination_guard_cannot_claim_nonstaff(
    roles: tuple[str, ...],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rbac_graph: RbacGraph,
) -> None:
    source = """from apps.identity.current_context import _load_current_actor

def require_future_owner(*, clinic_id):
    actor = _load_current_actor()
    has = getattr(actor, "_has_" + "role")
    return not all(has(role) for role in REQUIRED)
"""
    monkeypatch.setitem(
        SPELLINGS, "combination", "REQUIRED = " + repr(roles) + "\n" + source
    )
    guard = load_reproducer("combination", tmp_path, monkeypatch)
    actor = User.objects.create(username="synthetic-combination-" + uuid4().hex)
    row: Candidate = {"symbol": SYMBOL, "kind": "nonstaff", "signals": []}
    with pytest.raises(StaffDependentError) as refused:
        assert_behavioral_classifications(
            [row],
            {
                SYMBOL: [
                    DifferentialProbe(
                        SYMBOL, lambda: guard(clinic_id=rbac_graph.clinic_a)
                    )
                ]
            },
            actor=actor,
            scope=ReplayScope(rbac_graph.clinic_a, rbac_graph.organization_a),
        )
    assert set(refused.value.witnesses.values()) == {False, True}
    denied = next(
        key for key, allowed in refused.value.witnesses.items() if not allowed
    )
    assert set(denied.rsplit("/", 1)[1].split(",")) == set(roles)


def test_revoked_care_membership_cannot_claim_nonstaff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rbac_graph: RbacGraph,
) -> None:
    source = """from apps.identity.models import CareTeamMembership

def require_future_owner(*, clinic_id):
    return not CareTeamMembership.objects.filter(
        user_id=current_actor_id(), clinic_id=clinic_id,
        revoked_at__isnull=False,
    ).exists()
"""
    monkeypatch.setitem(SPELLINGS, "revoked", source)
    guard = load_reproducer("revoked", tmp_path, monkeypatch)
    actor_id, enrollment = permission_actor(rbac_graph, "unassigned")
    _, other = permission_actor(rbac_graph, "unassigned")
    actor = User.objects.get(pk=actor_id)
    row: Candidate = {"symbol": SYMBOL, "kind": "nonstaff", "signals": []}
    with pytest.raises(StaffDependentError) as refused:
        assert_behavioral_classifications(
            [row],
            {
                SYMBOL: [
                    DifferentialProbe(
                        SYMBOL, lambda: guard(clinic_id=rbac_graph.clinic_a)
                    )
                ]
            },
            actor=actor,
            scope=ReplayScope(
                rbac_graph.clinic_a,
                rbac_graph.organization_a,
                care=CareScope(enrollment, other),
            ),
        )
    assert set(refused.value.witnesses.values()) == {False, True}
    assert any(
        "/revoked/" in key and not value
        for key, value in refused.value.witnesses.items()
    )


def test_other_clinic_membership_is_not_treated_as_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rbac_graph: RbacGraph,
) -> None:
    source = """def require_future_owner(*, clinic_id):
    return not UserClinicRole.objects.filter(user_id=current_actor_id()).exclude(
        clinic_id=clinic_id,
    ).exists()
"""
    monkeypatch.setitem(SPELLINGS, "other_clinic", source)
    guard = load_reproducer("other_clinic", tmp_path, monkeypatch)
    actor = User.objects.create(username="synthetic-other-clinic-" + uuid4().hex)
    row: Candidate = {"symbol": SYMBOL, "kind": "nonstaff", "signals": []}
    with pytest.raises(StaffDependentError) as refused:
        assert_behavioral_classifications(
            [row],
            {
                SYMBOL: [
                    DifferentialProbe(
                        SYMBOL, lambda: guard(clinic_id=rbac_graph.clinic_a)
                    )
                ]
            },
            actor=actor,
            scope=ReplayScope(
                rbac_graph.clinic_a,
                rbac_graph.organization_a,
                other_clinic=rbac_graph.clinic_b,
            ),
        )
    assert any(
        key.startswith("other/") and not value
        for key, value in refused.value.witnesses.items()
    )

"""Observation rejects gates; named differential cases retain concrete witnesses."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.core import telemetry
from apps.identity.models import (
    PhysicianProfile,
    ProfessionalRegistration,
    RoleGrant,
    User,
)

from identity.authority_observer import AuthorityObservedError
from identity.nonstaff_differential import (
    DifferentialProbe,
    StaffDependentError,
    _replay,
    assert_behavioral_classifications,
)
from identity.nonstaff_states import ROLE_CASES, ROLES, CareScope, ReplayScope
from identity.permission_support import owner_context, permission_actor
from identity.test_nonstaff_differential import SPELLINGS, SYMBOL, load_reproducer

if TYPE_CHECKING:
    from pathlib import Path

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_backstop_retains_singletons_and_multirole_cases() -> None:
    assert () in ROLE_CASES
    assert all((role,) in ROLE_CASES for role in ROLES)
    assert ("owner", "physician") in ROLE_CASES
    assert ("nurse", "scheduler", "finance") in ROLE_CASES
    assert len(ROLE_CASES) == len(ROLES) + 5


def test_backstop_executes_authority_row_examples(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    actor_id, enrollment = permission_actor(rbac_graph, "unassigned")
    _, other = permission_actor(rbac_graph, "unassigned")
    actor = User.objects.get(pk=actor_id)
    monkeypatch.setenv(telemetry.OPS_METRICS_NETWORKS_ENV, "192.0.2.0/24")
    probe = DifferentialProbe(
        "apps.core.telemetry._allowed_networks", telemetry._allowed_networks, bool
    )
    report = _replay(
        [probe],
        actor=actor,
        scope=ReplayScope(
            rbac_graph.clinic_a,
            rbac_graph.organization_a,
            other_clinic=rbac_graph.clinic_b,
            care=CareScope(enrollment, other),
        ),
    )
    assert report.decisions[probe.symbol] == [(True,) * report.state_count]
    with owner_context(rbac_graph.organization_a):
        assert (
            RoleGrant.objects.filter(
                role="owner", permission="appointment.read"
            ).count()
            == 1
        )
        profile = PhysicianProfile.objects.get(user=actor)
        registration = ProfessionalRegistration.objects.get(user=actor)
        assert registration.physician_profile_id == profile.pk
    assert User.objects.get(pk=actor.pk).is_active


def _refuse_and_replay(
    probe: DifferentialProbe, actor: User, scope: ReplayScope
) -> StaffDependentError:
    with pytest.raises(AuthorityObservedError):
        assert_behavioral_classifications(
            [{"symbol": SYMBOL, "kind": "nonstaff", "signals": []}],
            {SYMBOL: [probe]},
            actor=actor,
            scope=scope,
        )
    with pytest.raises(StaffDependentError) as refused:
        _replay([probe], actor=actor, scope=scope)
    return refused.value


@pytest.mark.parametrize(
    "roles",
    [
        ("owner", "physician"),
        ("owner", "physician", "clinic_admin"),
        ("nurse", "scheduler", "finance"),
    ],
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
    refused = _refuse_and_replay(
        DifferentialProbe(SYMBOL, lambda: guard(clinic_id=rbac_graph.clinic_a)),
        actor,
        ReplayScope(rbac_graph.clinic_a, rbac_graph.organization_a),
    )
    assert set(refused.witnesses.values()) == {False, True}
    denied = next(key for key, allowed in refused.witnesses.items() if not allowed)
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
    refused = _refuse_and_replay(
        DifferentialProbe(SYMBOL, lambda: guard(clinic_id=rbac_graph.clinic_a)),
        actor,
        ReplayScope(
            rbac_graph.clinic_a,
            rbac_graph.organization_a,
            care=CareScope(enrollment, other),
        ),
    )
    assert set(refused.witnesses.values()) == {False, True}
    assert any(
        "/revoked/" in key and not value for key, value in refused.witnesses.items()
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
    refused = _refuse_and_replay(
        DifferentialProbe(SYMBOL, lambda: guard(clinic_id=rbac_graph.clinic_a)),
        actor,
        ReplayScope(
            rbac_graph.clinic_a,
            rbac_graph.organization_a,
            other_clinic=rbac_graph.clinic_b,
        ),
    )
    assert any(
        key.startswith("other/") and not value
        for key, value in refused.witnesses.items()
    )

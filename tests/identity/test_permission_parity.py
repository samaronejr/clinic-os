"""Legacy parity at actual callable boundaries, with a reviewed broad census."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import TYPE_CHECKING, NotRequired, TypedDict, cast
from uuid import uuid4

import pytest
from apps.identity import stepup
from apps.identity.models import User, UserClinicRole
from apps.identity.permissions import (
    BUNDLES_V1,
    PERMISSIONS,
    PROFESSIONAL_PERMISSIONS_V1,
)
from apps.tenancy.db import tenant_context
from django.db import connection
from django.test import override_settings

from auth.stepup_test_support import STEP_UP_NOW
from identity import legacy_operational_boundaries as operational
from identity import legacy_prescription_boundaries as prescriptions
from identity import legacy_sql_boundaries as sql_boundaries
from identity import legacy_teleconsult_boundaries as teleconsult
from identity import legacy_view_boundaries as views
from identity import permission_gate_census as census
from identity.legacy_clinical_boundaries import BOUNDARIES as CLINICAL_BOUNDARIES
from identity.legacy_guard_inventory import declared_probes, discover
from identity.legacy_identity_boundaries import BOUNDARIES as IDENTITY_BOUNDARIES
from identity.legacy_owner_boundaries import BOUNDARIES as OWNER_BOUNDARIES
from identity.legacy_parity_support import LEGACY, exercise, target_code, world
from identity.legacy_predicate_boundaries import BOUNDARIES as PREDICATE_BOUNDARIES
from identity.legacy_scope_boundaries import BOUNDARIES as SCOPE_BOUNDARIES
from identity.legacy_tenant_boundaries import exercise_tenant_boundaries
from identity.permission_support import owner_context
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)
CORE = (
    *IDENTITY_BOUNDARIES,
    *CLINICAL_BOUNDARIES,
    *PREDICATE_BOUNDARIES,
    *SCOPE_BOUNDARIES,
    *OWNER_BOUNDARIES,
)


class Candidate(TypedDict):
    symbol: str
    signals: list[str]
    kind: str
    probes: NotRequired[list[str]]
    enforced_by: NotRequired[list[str]]
    reason: NotRequired[str]


class Inventory(TypedDict):
    schema_version: int
    candidates: list[Candidate]
    probes: list[str]
    method: list[str]


INVENTORY = cast(
    "Inventory", json.loads(Path(__file__).with_name("legacy_guards.json").read_text())
)


EXEMPT_KINDS = frozenset(
    {"nonstaff", "provider", "infrastructure", "v2", "presentation", "data_operation"}
)


def live_graph(sources: dict[str, str] | None = None) -> census.Graph:
    """Build the reference graph over the live permission decisions."""
    with connection.cursor() as cursor:
        decisions = census.live_decisions(cursor)
    return census.build_graph(decisions, sources)


def _check_permission_gates(
    candidates: list[Candidate], graph: census.Graph | None
) -> None:
    """A permission boundary is never exempt, however it is spelled.

    A function that reaches a permission decision, directly or through any
    helper chain, alias or reference, needs a probe or a named delegation,
    never an exemption label. Only the identity permission subsystem itself
    is the v2 contract. The set is derived from the live catalog and the
    transitive reference graph (identity/permission_gate_census.py); the
    old spelling list must stay inside it. An exemption the census cannot
    follow fails closed unless reviewed.
    """
    graph = live_graph() if graph is None else graph
    gated = census.permission_gated(graph)
    assert census.spelled_gates(graph) <= set(gated)
    for row in candidates:
        if row["kind"] not in EXEMPT_KINDS:
            continue
        if row["symbol"] in gated:
            assert row["kind"] == "v2", (row["symbol"], gated[row["symbol"]])
            assert row["symbol"].startswith("apps.identity."), row["symbol"]
        unfollowed = census.unreviewed_dynamic_dispatch(graph, row["symbol"])
        assert not unfollowed, (row["symbol"], unfollowed)


def check_inventory(
    inventory: Inventory,
    declared: set[str] | None = None,
    graph: census.Graph | None = None,
) -> None:
    """Assert the reviewed census matches the tree and its own rules.

    ``declared`` defaults to the probes the boundary modules declare and
    ``graph`` to the live source; the rule tests pass a reduced probe set
    or substituted source to model a withdrawn probe or a refactor.
    """
    assert inventory["schema_version"] == 2
    candidates = inventory["candidates"]
    assert len({row["symbol"] for row in candidates}) == len(candidates)
    assert discover() == {row["symbol"]: row["signals"] for row in candidates}
    declared = declared_probes() if declared is None else declared
    assert sorted(declared) == inventory["probes"]
    bases = {probe.split("#", 1)[0] for probe in declared}
    for row in candidates:
        if row["kind"] == "direct":
            assert "probes" in row
            assert row["symbol"] in bases
            assert set(row["probes"]) <= declared
        elif row["kind"] == "polymorphic":
            assert "probes" in row
            assert row["probes"]
            assert all(
                target_code(probe) is target_code(row["symbol"])
                for probe in row["probes"]
            )
        elif row["kind"] == "delegated":
            assert "enforced_by" in row
            assert row["enforced_by"]
            assert set(row["enforced_by"]) <= bases
        else:
            assert row["kind"] in EXEMPT_KINDS
            assert "reason" in row
            assert row["reason"]
    # All definitions using the canonical role helper are directly exercised;
    # callers cannot again be replaced with tests of a harvested role tuple.
    for row in candidates:
        if "role_helper" in row["signals"]:
            assert row["kind"] in {"direct", "polymorphic"}, row["symbol"]
    _check_permission_gates(candidates, graph)
    # A symbol with an executed probe is classified as what the probe
    # proves; an exemption cannot sit on top of an executable oracle.
    for row in candidates:
        if row["symbol"] in bases:
            assert row["kind"] in {"direct", "polymorphic"}, row["symbol"]


def test_every_authorization_candidate_is_accounted_for() -> None:
    check_inventory(INVENTORY)


def _exempted(symbol: str) -> Inventory:
    mutated = copy.deepcopy(INVENTORY)
    row = next(row for row in mutated["candidates"] if row["symbol"] == symbol)
    row["kind"] = "infrastructure"
    row.pop("probes", None)
    row["reason"] = "Owner/bootstrap, audit, crypto, migration or OS-tty boundary."
    return mutated


@pytest.mark.parametrize(
    "symbol",
    [
        "apps.intake.demographics.set_intake_policy",
        "apps.intake.demographics.search_patient_identifiers",
        "apps.intake.access.authorized_enrollment_for",
    ],
)
def test_census_rejects_exempting_a_permission_gate(symbol: str) -> None:
    """Even with its probe withdrawn, a permission gate is never exempt."""
    mutated = _exempted(symbol)
    declared = declared_probes() - {symbol}
    mutated["probes"] = sorted(declared)
    with pytest.raises(AssertionError, match=symbol.replace(".", r"\.")):
        check_inventory(mutated, declared)


def test_census_rejects_exempting_a_probed_boundary() -> None:
    """A tenant-bound primitive with an executed oracle stays classified by it."""
    symbol = "apps.tenancy.envelope.blind_indexes"
    with pytest.raises(AssertionError, match=symbol.replace(".", r"\.")):
        check_inventory(_exempted(symbol))


@pytest.fixture(autouse=True)
def fixed_verification_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stepup, "_utc_now_seconds", lambda: STEP_UP_NOW)


@pytest.mark.parametrize("legacy_role", LEGACY)
@pytest.mark.parametrize("family", sorted({boundary.family for boundary in CORE}))
def test_actual_legacy_boundaries(
    rbac_graph: RbacGraph, legacy_role: str, family: str
) -> None:
    subject = world(rbac_graph, legacy_role)
    if family == "organization":
        with owner_context(rbac_graph.organization_a):
            UserClinicRole.objects.get_or_create(
                organization_id=rbac_graph.organization_a,
                clinic_id=rbac_graph.clinic_b,
                user_id=subject.actor.pk,
                role=legacy_role,
            )
    for boundary in CORE:
        if boundary.family == family:
            exercise(boundary, subject)


@pytest.mark.parametrize("legacy_role", LEGACY)
@pytest.mark.parametrize(
    "family", ["operational", "prescription", "http", "teleconsult", "sql"]
)
def test_actual_domain_boundaries(
    rbac_graph: RbacGraph,
    legacy_role: str,
    family: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subject = world(rbac_graph, legacy_role)
    with override_settings(
        BILLING_SYNTHETIC_PIX=True,
        PRESCRIPTION_SYNTHETIC_SIGNING=True,
        PHYSICIAN_SYNTHETIC_REGISTRY=True,
        TELECONSULT_SYNTHETIC_PROVIDER=True,
    ):
        op = operational.seed_operational(subject)
        if family == "operational":
            selected = operational.boundaries(op)
        elif family == "sql":
            selected = sql_boundaries.boundaries(op)
        elif family == "teleconsult":
            selected = teleconsult.boundaries(
                teleconsult.seed_teleconsult(subject, op, monkeypatch)
            )
        else:
            rx = prescriptions.seed_prescription(subject)
            selected = (
                prescriptions.boundaries(rx)
                if family == "prescription"
                else views.boundaries(op, rx)
            )
        for boundary in selected:
            exercise(boundary, subject)


@pytest.mark.parametrize("legacy_role", LEGACY)
def test_actual_tenant_boundaries(rbac_graph: RbacGraph, legacy_role: str) -> None:
    exercise_tenant_boundaries(world(rbac_graph, legacy_role))


_GATE_CALLS = """            try:
                actor_id = require_permission(
                    "configuration.clinic", clinic_id=clinic_id
                )
            except CurrentActorError:
                actor_id = require_permission(
                    "configuration.organization", clinic_id=clinic_id
                )
"""
_POLICY = "apps.intake.demographics.set_intake_policy"
# Each refactor moves set_intake_policy's permission decision out of its
# own body; none of them may let the census accept an exemption.
_REFACTORS = {
    "one-line helper (reviewer)": (
        "            actor_id = _config_actor(clinic_id)\n",
        """

def _config_actor(clinic_id):
    try:
        return require_permission("configuration.clinic", clinic_id=clinic_id)
    except CurrentActorError:
        return require_permission("configuration.organization", clinic_id=clinic_id)
""",
    ),
    "two-level helper chain": (
        "            actor_id = _outer(clinic_id)\n",
        """

def _outer(clinic_id):
    return _inner(clinic_id, "configuration.clinic")


def _inner(clinic_id, permission):
    return require_permission(permission, clinic_id=clinic_id)
""",
    ),
    "import alias": (
        '            actor_id = _decide("configuration.clinic", clinic_id=clinic_id)\n',
        """

from apps.identity.current_context import require_permission as _decide
""",
    ),
    "module attribute": (
        '            actor_id = _context.require_permission("configuration.clinic",'
        " clinic_id=clinic_id)\n",
        """

import apps.identity.current_context as _context
""",
    ),
}


def _refactored_graph(body: str, appendix: str) -> census.Graph:
    path = Path(census.ROOT, "apps/intake/demographics.py")
    source = path.read_text()
    assert source.count(_GATE_CALLS) == 1
    source = source.replace(_GATE_CALLS, body) + appendix
    return live_graph({"apps.intake.demographics": source})


@pytest.mark.parametrize("refactor", sorted(_REFACTORS))
def test_census_refuses_exempting_a_wrapped_permission_gate(refactor: str) -> None:
    """NEW-2: moving the decision behind a helper, chain, alias or module
    attribute does not let the census accept an exemption."""
    body, appendix = _REFACTORS[refactor]
    graph = _refactored_graph(body, appendix)
    assert _POLICY in census.permission_gated(graph)
    mutated = _exempted(_POLICY)
    declared = declared_probes() - {_POLICY}
    mutated["probes"] = sorted(declared)
    with pytest.raises(AssertionError, match=_POLICY.replace(".", r"\.")):
        check_inventory(mutated, declared, graph)


def test_permission_decision_is_actor_differential(rbac_graph: RbacGraph) -> None:
    """The census root is behavioural: has_permission, executed as the
    runtime role, answers exactly each bundle for actors that differ only
    in role. Role rows combine by EXISTS, so a combination answers the union
    of its members; the empty set and the full union are executed too
    (10 roles + none + all = 12 states x every permission; each actor also
    holds an other-clinic role, which must never leak here). Professional
    permissions additionally need a current registration and care-team
    scope (tests/identity/test_permission_scope.py); these actors hold none,
    so those answer no for every state."""
    states: list[tuple[str, tuple[UserClinicRole.Role, ...]]] = [
        (role, (UserClinicRole.Role(role),)) for role in sorted(BUNDLES_V1)
    ]
    states.append(("none", ()))
    states.append(("all", tuple(UserClinicRole.Role(role) for role in BUNDLES_V1)))
    checked = 0
    for label, roles in states:
        actor = User.objects.create(username=f"census-{label}-{uuid4().hex}")
        with owner_context(rbac_graph.organization_a):
            # Tenant membership through another clinic only: the "none"
            # state still enters the organization but holds no role here.
            UserClinicRole.objects.create(
                organization_id=rbac_graph.organization_a,
                clinic_id=rbac_graph.clinic_b,
                user_id=actor.pk,
                role=UserClinicRole.Role.RECEPTIONIST,
            )
            for role in roles:
                UserClinicRole.objects.create(
                    organization_id=rbac_graph.organization_a,
                    clinic_id=rbac_graph.clinic_a,
                    user_id=actor.pk,
                    role=role,
                )
        granted = (
            set().union(*(BUNDLES_V1[str(role)] for role in roles))
            - PROFESSIONAL_PERMISSIONS_V1
        )
        with (
            runtime_role(),
            tenant_context(actor.pk, rbac_graph.organization_a),
            connection.cursor() as cursor,
        ):
            for permission in sorted(PERMISSIONS):
                cursor.execute(
                    "SELECT clinic_app.has_permission(%s, %s, NULL)",
                    [permission, rbac_graph.clinic_a],
                )
                assert cursor.fetchone() == (permission in granted,), (
                    label,
                    permission,
                )
                checked += 1
    assert checked == len(states) * len(PERMISSIONS)

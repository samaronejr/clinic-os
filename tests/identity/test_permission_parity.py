"""Legacy parity at actual callable boundaries, with a reviewed broad census."""

from __future__ import annotations

import collections
import copy
import dataclasses
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
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
from apps.intake import demographics
from apps.tenancy.db import tenant_context
from django.core import signing
from django.db import connection, transaction
from django.test import override_settings

from auth.stepup_test_support import STEP_UP_NOW
from database_urls import database_url_for_name
from identity import actor_channels, exemption_probes, probe_states
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
    from collections.abc import Mapping

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
    # The static graph fails closed on names it cannot resolve.
    assert not graph.unresolved, graph.unresolved
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


# Labels that claim "no staff permission gate here". v2 is the permission
# subsystem itself (identity only, anchored by the has_permission test).
PROBED_EXEMPT_KINDS = EXEMPT_KINDS - {"v2"}


def _check_exemption_probes(
    candidates: list[Candidate],
    probes: Mapping[str, exemption_probes.ExemptionProbe],
    graph: census.Graph | None,
) -> None:
    """An exemption is valid only with an executed differential probe.

    Static analysis cannot prove the absence of a gate, so the probe is the
    authority: every exempt row must have a probe (executed across the whole
    derived staff-state matrix by
    ``test_every_exemption_probe_is_staff_independent``),
    and the registry holds no probe for anything else. Where the static
    graph derives a gate for a probed exemption, the two disagree and the
    census fails.
    """
    exempt = {row["symbol"] for row in candidates if row["kind"] in PROBED_EXEMPT_KINDS}
    unprobed = sorted(exempt - set(probes))
    assert not unprobed, ("exemption without an executed differential probe", unprobed)
    assert set(probes) <= exempt, sorted(set(probes) - exempt)
    graph = live_graph() if graph is None else graph
    disagree = sorted(set(census.permission_gated(graph)) & set(probes))
    assert not disagree, (
        "static graph derives a gate for a probed exemption",
        disagree,
    )


def check_inventory(
    inventory: Inventory,
    declared: set[str] | None = None,
    graph: census.Graph | None = None,
    probes: Mapping[str, exemption_probes.ExemptionProbe] | None = None,
) -> None:
    """Assert the reviewed census matches the tree and its own rules.

    ``declared`` defaults to the probes the boundary modules declare,
    ``graph`` to the live source and ``probes`` to the exemption probe
    registry; the rule tests pass reduced sets or substituted source to
    model a withdrawn probe or a refactor.
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
    graph = live_graph() if graph is None else graph
    _check_permission_gates(candidates, graph)
    _check_exemption_probes(
        candidates, exemption_probes.PROBES if probes is None else probes, graph
    )
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


_CLASS_GATE = """

class _Gate:
    def __call__(self, clinic_id):
        return require_permission("configuration.clinic", clinic_id=clinic_id)

    def check(self, clinic_id):
        return require_permission("configuration.clinic", clinic_id=clinic_id)
"""
_UNQUALIFIED_SQL_GATE = """
CREATE FUNCTION clinic_app.zz_cfg_gate(clinic pg_catalog.uuid)
RETURNS pg_catalog.uuid LANGUAGE plpgsql
SET search_path = pg_catalog, clinic_app AS $f$
BEGIN
    IF NOT has_permission('configuration.clinic', clinic, NULL) THEN
        RAISE EXCEPTION USING ERRCODE = '42501', MESSAGE = 'denied';
    END IF;
    RETURN NULLIF(current_setting('app.current_user_id', true), '')::pg_catalog.uuid;
END $f$;
"""
_TRIGGER_GATE = """
CREATE FUNCTION clinic_app.zz_policy_gate() RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, clinic_app AS $f$
BEGIN
    IF NOT clinic_app.has_permission('configuration.clinic', NEW.clinic_id, NULL) THEN
        RAISE EXCEPTION USING ERRCODE = '42501', MESSAGE = 'denied';
    END IF;
    RETURN NEW;
END $f$;
CREATE TRIGGER zz_policy_gate BEFORE INSERT ON clinic_app.intake_clinicintakepolicy
FOR EACH ROW EXECUTE FUNCTION clinic_app.zz_policy_gate();
"""
_CALL = '"configuration.clinic", clinic_id=clinic_id'
# The reviewer's 11 bypass shapes (M1-M11): (body, appendix, database DDL).
_REVIEWER_SHAPES: dict[str, tuple[str, str, str]] = {
    "M1 module-level functools.partial": (
        "            actor_id = _gate(clinic_id=clinic_id)\n",
        "\nimport functools\n_gate = functools.partial("
        'require_permission, "configuration.clinic")\n',
        "",
    ),
    "M2 module-level lambda": (
        "            actor_id = _gate(clinic_id)\n",
        f"\n_gate = lambda clinic_id: require_permission({_CALL})\n",
        "",
    ),
    "M3 dispatch table": (
        f'            actor_id = _GATES["clinic"]({_CALL})\n',
        '\n_GATES = {"clinic": require_permission}\n',
        "",
    ),
    "M4 getattr with a constant name": (
        f'            actor_id = getattr(_ctx, "require_permission")({_CALL})\n',
        "\nimport apps.identity.current_context as _ctx\n",
        "",
    ),
    "M5 module instance __call__": (
        "            actor_id = _gate(clinic_id)\n",
        _CLASS_GATE + "\n_gate = _Gate()\n",
        "",
    ),
    "M6 class-level __call__": (
        "            actor_id = _Gate()(clinic_id)\n",
        _CLASS_GATE,
        "",
    ),
    "M7 method on a local instance": (
        "            _g = _Gate()\n            actor_id = _g.check(clinic_id)\n",
        _CLASS_GATE,
        "",
    ),
    "M8 in-function lambda": (
        "            actor_id = (lambda c: require_permission("
        '"configuration.clinic", clinic_id=c))(clinic_id)\n',
        "",
        "",
    ),
    "M9 in-function functools.partial": (
        "            actor_id = functools.partial(require_permission, "
        '"configuration.clinic")(clinic_id=clinic_id)\n',
        "\nimport functools\n",
        "",
    ),
    "M10 SQL function calling has_permission unqualified": (
        "            actor_id = _cfg(clinic_id)\n",
        """

def _cfg(clinic_id):
    with connection.cursor() as cursor:
        cursor.execute("SELECT zz_cfg_gate(%s)", [clinic_id])
        return cursor.fetchone()[0]
""",
        _UNQUALIFIED_SQL_GATE,
    ),
    "M11 gate only in a BEFORE INSERT trigger": (
        "            actor_id = current_actor_id()\n",
        "\nfrom apps.identity.current_context import current_actor_id\n",
        _TRIGGER_GATE,
    ),
}


def _shape_graph(body: str, appendix: str, ddl: str) -> census.Graph:
    path = Path(census.ROOT, "apps/intake/demographics.py")
    source = path.read_text()
    assert source.count(_GATE_CALLS) == 1
    sources = {"apps.intake.demographics": source.replace(_GATE_CALLS, body) + appendix}
    if not ddl:
        return live_graph(sources)
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(ddl)
        graph = live_graph(sources)
        transaction.set_rollback(True)
    return graph


@pytest.mark.parametrize("shape", sorted(_REVIEWER_SHAPES))
def test_census_refuses_every_reviewer_bypass_shape(shape: str) -> None:
    """R3-1: whatever the spelling or indirection (Python or database side),
    an exemption whose differential probe is withdrawn is refused."""
    graph = _shape_graph(*_REVIEWER_SHAPES[shape])
    mutated = _exempted(_POLICY)
    declared = declared_probes() - {_POLICY}
    mutated["probes"] = sorted(declared)
    with pytest.raises(AssertionError):
        check_inventory(mutated, declared, graph)


def _probe_world(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> exemption_probes.ProbeWorld:
    actor_channels.enable_function_statistics(
        database_url_for_name(
            os.environ["TEST_SUPERUSER_DATABASE_URL"],
            str(connection.settings_dict["NAME"]),
        )
    )
    subject = world(rbac_graph, "receptionist")
    op = operational.seed_operational(subject)
    rx = prescriptions.seed_prescription(subject)
    tc = teleconsult.seed_teleconsult(subject, op, monkeypatch)
    return exemption_probes.build_world(subject, op, rx, tc)


_SYNTHETIC = {
    "BILLING_SYNTHETIC_PIX": True,
    "PRESCRIPTION_SYNTHETIC_SIGNING": True,
    "PHYSICIAN_SYNTHETIC_REGISTRY": True,
    "TELECONSULT_SYNTHETIC_PROVIDER": True,
}


def _volatility_is_per_call(
    probe: exemption_probes.ExemptionProbe, probe_world: exemption_probes.ProbeWorld
) -> bool:
    """A normalized part must change between two runs of one state.

    The second run's signing clock is an hour later, so a timestamped
    token cannot look stable just because both runs fell in one second.
    """
    raw = dataclasses.replace(probe, normalize=None)
    actor = probe_world.matrix.states[0].actor
    first = exemption_probes.execute(raw, probe_world, actor)
    later = SimpleNamespace(time=lambda: time.time() + 3600)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(signing, "time", later)
        second = exemption_probes.execute(raw, probe_world, actor)
    return first != second


def test_every_exemption_probe_is_staff_independent(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every census exemption runs under every state of the staff-state
    matrix derived from the live permission decision (identity/
    probe_states.py: the role power set, credential levels per bundle
    class, every registration/care/profile value, inactive, user flags,
    elsewhere, open and closed assignment, each single role-permission
    removal, cumulative role-grant removals) and reaches one exactly identical
    outcome; every call line of its body executes in some state; the
    declared class (success/refusal) holds, as deployed for patient code;
    the function never observes the actor (identity/actor_channels.py:
    the primary rule; the matrix is the behavioural cross-check);
    and the rows written realize every declared value of every varied
    input. The original 13 states (10 roles, none, all, the assigned
    physician) are still in the matrix."""
    started = time.monotonic()
    with override_settings(**_SYNTHETIC):
        probe_world = _probe_world(rbac_graph, monkeypatch)
        states = probe_world.matrix.states
        labels = [state.label for state in states]
        assert len(set(labels)) == len(labels)
        assert {*BUNDLES_V1, "none", "all", "assigned"} <= set(labels)
        families = collections.Counter(state.family for state in states)
        assert families["power"] == 2 ** len(BUNDLES_V1)
        assert families == {
            "power": 1024,
            "credential": 193,
            "realization": 75,
            "inactive": 2,
            "elsewhere": 2,
            "flags": 2,
            "assigned": 3,
            "removal": 101,
            "grant": 11,
            "control": 1,
        }
        built = time.monotonic()
        probes = [
            exemption_probes.PROBES[key] for key in sorted(exemption_probes.PROBES)
        ]
        runs = exemption_probes.run_matrix(probes, probe_world)
        ran = time.monotonic()
        failures: dict[str, object] = {}
        for probe in probes:
            run = runs[probe.symbol]
            found = exemption_probes.problems(probe, run)
            expected = {"success": "ok", "refusal": "raise"}[probe.reaches]
            declared = (
                {exemption_probes.reached(run.baseline)}
                if run.baseline is not None
                else {exemption_probes.reached(o) for o in run.outcomes.values()}
            )
            if declared != {expected}:
                found.append(f"declared {probe.reaches}, reached {sorted(declared)}")
            if probe.normalize is not None and not _volatility_is_per_call(
                probe, probe_world
            ):
                found.append("normalized part does not change between calls")
            print(  # noqa: T201 - per-probe evidence line (pytest -s)
                "PROBE",
                json.dumps(
                    {
                        "symbol": probe.symbol,
                        "context": probe.context,
                        "reaches": probe.reaches,
                        "baseline": sorted(declared),
                        "states": len(run.outcomes),
                        "distinct_outcomes": len(set(run.outcomes.values())),
                        "outcome": str(next(iter(run.outcomes.values())))[:200],
                        "call_lines": sorted(run.required),
                        "reached_call_lines": sorted(run.required & run.reached),
                        "normalized": probe.why,
                        "seconds": round(run.seconds, 2),
                    }
                ),
            )
            if found:
                failures[probe.symbol] = found
        realized = probe_states.realized(probe_world.matrix)
        for key, (_kind, values) in probe_states.DIMENSIONS.items():
            if isinstance(values, frozenset) and realized[key] != values:
                failures[key] = (sorted(values - realized[key]), "not realized")
    print(  # noqa: T201 - matrix size and cost evidence (pytest -s)
        "MATRIX",
        json.dumps(
            {
                "states": len(states),
                "families": dict(sorted(families.items())),
                "probes": len(probes),
                "executions": len(states) * len(probes),
                "build_seconds": round(built - started, 1),
                "run_seconds": round(ran - built, 1),
            }
        ),
    )
    if failures:
        pytest.fail(
            "\n".join(f"{symbol}: {detail}" for symbol, detail in failures.items())
        )


def _configuration_answer(pw: exemption_probes.ProbeWorld) -> bool:
    """The live decision set_intake_policy asks, read as data."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.has_permission('configuration.clinic', %s, NULL) "
            "OR clinic_app.has_permission('configuration.organization', %s, NULL)",
            [pw.w.clinic, pw.w.clinic],
        )
        row = cursor.fetchone()
    return bool(row and row[0])


def test_differential_probe_classifies_a_gated_function_as_gated(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a real probe, a permission-gated function is classified gated:
    set_intake_policy decides differently across role states, so no
    exemption for it could ever pass the probe execution. Across the whole
    matrix it succeeds in exactly the states whose live decision grants a
    configuration permission at that moment (an oracle probe reads it in
    the same run)."""
    probe = exemption_probes.ExemptionProbe(
        _POLICY,
        "staff",
        "success",
        lambda pw: demographics.set_intake_policy(
            clinic_id=pw.w.clinic, required_fields=[]
        ),
    )
    oracle = exemption_probes.ExemptionProbe(
        f"{_POLICY}#oracle",
        "staff",
        "success",
        _configuration_answer,
        code=_configuration_answer.__code__,
    )
    with override_settings(**_SYNTHETIC):
        probe_world = _probe_world(rbac_graph, monkeypatch)
        runs = exemption_probes.run_matrix([probe, oracle], probe_world)
    outcomes = runs[_POLICY].outcomes
    assert not runs[_POLICY].not_entered
    assert not exemption_probes.staff_independent(outcomes)
    allowed = {state for state, outcome in outcomes.items() if outcome[0] == "ok"}
    granted = {
        state
        for state, outcome in runs[oracle.symbol].outcomes.items()
        if outcome == ("ok", ("bool", True))
    }
    assert allowed == granted
    original = {*BUNDLES_V1, "none", "all", "assigned"}
    assert allowed & original == {
        "all",
        "clinic_admin",
        "clinic_manager",
        "finance",
        "org_admin",
        "owner",
    }

"""The exemption probes certify only what they vary, reach and compare.

R4-1: an exemption probe accepted a gate keyed on staff state outside its
13 role states, proved only that the body was entered, and compared type
names. These tests pin the three repairs: the state matrix is derived from
the inputs the live permission decision reads (and a new input fails), a
probe must execute every call line of the body in some state, and outcomes
are compared exactly. Each gate variant below must be refused.
"""

from __future__ import annotations

import contextlib
import dataclasses
import inspect
import re
from datetime import UTC, datetime, timedelta
from types import FunctionType
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.ehr import finalization
from apps.identity import stepup
from apps.identity.current_context import CurrentActorError, require_permission
from apps.tenancy.db import tenant_context
from django.db import connection, transaction
from django.test import override_settings

from auth.stepup_test_support import STEP_UP_NOW
from identity import actor_channels, exemption_probes, probe_states
from identity import permission_gate_census as census
from identity.permission_inputs import decision_inputs
from identity.test_permission_parity import _SYNTHETIC, INVENTORY, _probe_world
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from collections.abc import Callable

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)
_EXECUTED_CALL = re.compile(r"clinic_app\.([a-z_][a-z0-9_]*)\s*\(")


@pytest.fixture(autouse=True)
def fixed_verification_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stepup, "_utc_now_seconds", lambda: STEP_UP_NOW)


def executed_decisions(graph: RbacGraph) -> set[str]:
    """What the Python permission entry actually executes (captured, not
    listed): has_permission, and the actor loader behind it."""
    executed: list[str] = []

    def capture(
        execute: Callable[..., object],
        sql: str,
        params: object,
        many: bool,
        context: object,
    ) -> object:
        executed.append(sql)
        return execute(sql, params, many, context)

    with (
        runtime_role(),
        tenant_context(graph.physician, graph.organization_a),
        connection.execute_wrapper(capture),
        contextlib.suppress(CurrentActorError),
    ):
        require_permission("clinical.read", clinic_id=graph.clinic_a)
    called = {name for sql in executed for name in _EXECUTED_CALL.findall(sql)}
    assert census.ROOT_DECISION in called
    return called


def _matrix_gaps(executed: set[str]) -> tuple[set[str], set[str]]:
    """(undeclared, stale): derived decision inputs vs the declared matrix.

    Roots are every live decision the census derives plus ``executed``.
    """
    with connection.cursor() as cursor:
        roots = set(census.live_decisions(cursor).functions) | executed
        derived = decision_inputs(cursor, roots).keys()
    declared = set(probe_states.DIMENSIONS)
    return set(derived) - declared, declared - set(derived)


def test_probe_matrix_covers_every_decision_input(rbac_graph: RbacGraph) -> None:
    """Every input the live decision reads is declared, varied or fixed with
    a reason, and nothing stale is declared. Varied values are checked
    against the written rows in test_every_exemption_probe_is_staff_independent."""
    undeclared, stale = _matrix_gaps(executed_decisions(rbac_graph))
    assert not undeclared, (
        "decision inputs the probe matrix does not vary",
        undeclared,
    )
    assert not stale, ("declared inputs the decision no longer reads", stale)
    for key, (kind, value) in probe_states.DIMENSIONS.items():
        if kind == "varied":
            assert isinstance(value, frozenset), key
            assert len(value) > 1, key
        else:
            assert kind == "fixed", key
            assert isinstance(value, str), key
            assert value.strip(), key


_WRAPPER = """
CREATE FUNCTION clinic_app.zz_scoped_decision(perm text, clinic uuid)
RETURNS boolean LANGUAGE sql STABLE AS $f$
  SELECT clinic_app.has_permission(perm, clinic, NULL) AND NOT EXISTS (
    SELECT 1 FROM clinic_app.identity_userpreference p
    WHERE p.user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
$f$;
"""
_DYNAMIC = """
CREATE FUNCTION clinic_app.zz_dynamic_decision(perm text, clinic uuid)
RETURNS boolean LANGUAGE plpgsql STABLE AS $f$
DECLARE granted boolean;
BEGIN
  EXECUTE 'SELECT clinic_app.has_permission($1, $2, NULL)' INTO granted
    USING perm, clinic;
  RETURN granted;
END $f$;
"""


def test_a_new_decision_input_fails_the_matrix_check(rbac_graph: RbacGraph) -> None:
    """Mutation proof of the derivation (rolled back): has_permission
    branching on a column the matrix does not vary, a decision wrapper
    reading a new table, and an opaque (dynamic SQL) decision all fail."""
    executed = executed_decisions(rbac_graph)
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_catalog.pg_get_functiondef("
                "'clinic_app.has_permission(text, uuid, uuid)'::regprocedure)"
            )
            (definition,) = cursor.fetchone() or ("",)
            body = re.search(r"RETURN EXISTS \((.*)\);\s*END", definition, re.DOTALL)
            assert body is not None
            prefix = definition[: definition.index("RETURN EXISTS (")]
            # The live definition, now also branching on identity_user.is_staff.
            staff_gate = (
                ") AND NOT EXISTS (SELECT 1 FROM clinic_app.identity_user staff "
                "WHERE staff.id=actor AND staff.is_staff);\nEND $function$"
            )
            replaced = "".join((prefix, "RETURN EXISTS (", body.group(1), staff_gate))
            cursor.execute("SET LOCAL ROLE clinic_resolver")
            cursor.execute(replaced)
            cursor.execute("RESET ROLE")
        undeclared, _ = _matrix_gaps(executed)
        transaction.set_rollback(True)
    assert "column:has_permission:identity_user.is_staff" in undeclared
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(_WRAPPER)
        undeclared, _ = _matrix_gaps(executed)
        transaction.set_rollback(True)
    assert "column:zz_scoped_decision:identity_userpreference.user_id" in undeclared
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(_DYNAMIC)
        with pytest.raises(census.CensusError, match="dynamic SQL"):
            _matrix_gaps(executed)
        transaction.set_rollback(True)
    assert _matrix_gaps(executed) == (set(), set())


_SYMBOL = "apps.ehr.finalization._next_version"
_RETURN = "    return int(row[0])\n"
_DENIED = "    if row is None:\n        raise ClinicalAccessDeniedError\n"
_GATE_IMPORTS = (
    "import functools\n"
    "from apps.identity.current_context import (\n"
    "    CurrentActorError, _load_current_actor, current_actor_id,\n"
    "    require_permission,\n"
    ")\n"
    "from apps.identity.models import ProfessionalRegistration\n"
)
# (replaced text, replacement, appendix, the repair that must refuse it)
_VARIANTS: dict[str, tuple[str, str, str, str]] = {
    # The reviewer's exact R4-1 repro: a module-level partial on a
    # registration-gated permission inside try/except.
    "r4-1 partial on break_glass.request_scoped": (
        _RETURN,
        "    try:\n"
        "        _scoped(clinic_id=_CLINIC)\n"
        "    except CurrentActorError:\n"
        "        return int(row[0])\n"
        "    return None\n",
        _GATE_IMPORTS + "_scoped = functools.partial(require_permission, "
        "'break_glass.request_scoped')\n",
        "states",
    ),
    "care-team membership": (
        _RETURN,
        "    try:\n"
        "        _scoped(clinic_id=_CLINIC, patient_enrollment_id=_ENROLLMENT)\n"
        "    except CurrentActorError:\n"
        "        return int(row[0])\n"
        "    return None\n",
        _GATE_IMPORTS + "_scoped = functools.partial(require_permission, "
        "'observation.write')\n",
        "states",
    ),
    "is_active": (
        _RETURN,
        "    try:\n"
        "        current_actor_id()\n"
        "    except CurrentActorError:\n"
        "        return None\n"
        "    return int(row[0])\n",
        _GATE_IMPORTS,
        "states",
    ),
    "registration revoked": (
        _RETURN,
        "    if ProfessionalRegistration.objects.filter(\n"
        "        user_id=current_actor_id(), revoked_at__isnull=False\n"
        "    ).exists():\n"
        "        return None\n"
        "    return int(row[0])\n",
        _GATE_IMPORTS,
        "states",
    ),
    "value-only change": (
        _RETURN,
        "    with connection.cursor() as gate:\n"
        "        gate.execute(\n"
        '            "SELECT clinic_app.has_permission("\n'
        "            \"'configuration.clinic', %s, NULL)::int\", [_CLINIC]\n"
        "        )\n"
        "        (granted,) = gate.fetchone()\n"
        "    return int(row[0]) + granted\n",
        "",
        "exact",
    ),
    # R5-1a: configuration.clinic granted AND appointment.read denied. Every
    # role holding the first also holds the second, so only a removal of
    # exactly (role, appointment.read) reaches it.
    "r5-1a single role-permission removal": (
        _RETURN,
        "    try:\n"
        "        _cfg(clinic_id=_CLINIC)\n"
        "    except CurrentActorError:\n"
        "        return int(row[0])\n"
        "    try:\n"
        "        _agenda(clinic_id=_CLINIC)\n"
        "    except CurrentActorError:\n"
        "        return None\n"
        "    return int(row[0])\n",
        _GATE_IMPORTS
        + "_cfg = functools.partial(require_permission, 'configuration.clinic')\n"
        + "_agenda = functools.partial(require_permission, 'appointment.read')\n",
        "states",
    ),
    # R5-1b: a branch on a user flag of the loaded actor.
    "r5-1b is_superuser": (
        _RETURN,
        "    try:\n"
        "        flagged = _load_current_actor().is_superuser\n"
        "    except CurrentActorError:\n"
        "        flagged = False\n"
        "    if flagged:\n"
        "        return None\n"
        "    return int(row[0])\n",
        _GATE_IMPORTS,
        "states",
    ),
    # R5-1c: a Python check on the actor's non-open encounters.
    "r5-1c closed encounter": (
        _RETURN,
        "    try:\n"
        "        actor = current_actor_id()\n"
        "    except CurrentActorError:\n"
        "        return int(row[0])\n"
        "    if Encounter.objects.filter(physician_id=actor).exclude(\n"
        "        state='open'\n"
        "    ).exists():\n"
        "        return None\n"
        "    return int(row[0])\n",
        _GATE_IMPORTS,
        "states",
    ),
    # A read of a relation whose row policy observes the actor, with no
    # actor accessor, SQL setting or permission call in sight.
    "rls touch": (
        _RETURN,
        "    if ClinicalDocumentVersion.objects.filter(state='draft').exists():\n"
        "        return None\n"
        "    return int(row[0])\n",
        "",
        "rule",
    ),
    "gate on a line no probe input reaches": (
        _DENIED,
        "    if row is None:\n"
        "        _scoped(clinic_id=_CLINIC)\n"
        "        raise ClinicalAccessDeniedError\n",
        _GATE_IMPORTS + "_scoped = functools.partial(require_permission, "
        "'configuration.clinic')\n",
        "reach",
    ),
}
_EXPECTED = {
    "states": "outcome differs across states",
    "exact": "outcome differs across states",
    "reach": "call lines never executed in any state",
    "rule": "observes the actor",
}
# Gates the actor rule cannot see: never executed, so never observing.
_BEYOND_RULE = frozenset({"gate on a line no probe input reaches"})


def _mutant(name: str, bindings: dict[str, object]) -> FunctionType:
    """Compile a mutated copy of _next_version at its own file lines."""
    old, new, appendix, _ = _VARIANTS[name]
    original = finalization._next_version
    source = inspect.getsource(original)
    assert source.count(old) == 1, name
    first = original.__code__.co_firstlineno
    namespace = dict(vars(finalization)) | bindings
    exec(appendix, namespace)  # noqa: S102 - the mutated probe target
    code = compile(
        "\n" * (first - 1) + source.replace(old, new),
        finalization.__file__,
        "exec",
    )
    exec(code, namespace)  # noqa: S102 - the mutated probe target
    function = namespace["_next_version"]
    assert isinstance(function, FunctionType)
    assert function is not original
    return function


def _invoker(
    function: FunctionType,
) -> Callable[[exemption_probes.ProbeWorld], object]:
    """The registered probe's input, applied to the mutated copy."""
    return lambda pw: function(pw.w.version.document)


def _variant_probes(
    probe_world: exemption_probes.ProbeWorld,
) -> list[exemption_probes.ExemptionProbe]:
    registered = exemption_probes.PROBES[_SYMBOL]
    bindings: dict[str, object] = {
        "_CLINIC": probe_world.w.clinic,
        "_ENROLLMENT": probe_world.op.enrollment,
    }
    probes = [registered]
    for name in _VARIANTS:
        function = _mutant(name, bindings)
        probes.append(
            dataclasses.replace(
                registered,
                symbol=f"{_SYMBOL}#{name}",
                invoke=_invoker(function),
                code=function.__code__,
            )
        )
    return probes


def test_exemption_probes_refuse_every_r4_gate_variant(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R4-1, R5-1 and their variants, each run by the registered
    _next_version probe (same context, input and class) against a mutated
    copy of the function. The matrix alone (the actor rule left out of the
    verdict) refuses every behavioural shape by the repair it targets; the
    actor rule refuses every shape that executes; the unmutated function is
    certified by both in the same run."""
    with override_settings(**_SYNTHETIC):
        probe_world = _probe_world(rbac_graph, monkeypatch)
        probes = _variant_probes(probe_world)
        runs = exemption_probes.run_matrix(probes, probe_world)
    matrix = {
        probe.symbol: exemption_probes.problems(
            probe, runs[probe.symbol], observed=False
        )
        for probe in probes
    }
    rule = {
        probe.symbol: exemption_probes.actor_problems(runs[probe.symbol])
        for probe in probes
    }
    for probe in probes:
        print(  # noqa: T201 - receipt
            "VERDICT",
            probe.symbol,
            {"matrix": matrix[probe.symbol] or "certified"},
            {"rule": rule[probe.symbol] or "certified"},
        )
    assert matrix[_SYMBOL] == []
    assert rule[_SYMBOL] == []
    accepted = [
        name
        for name, (*_, repair) in _VARIANTS.items()
        if repair != "rule"
        and not any(
            problem.startswith(_EXPECTED[repair])
            for problem in matrix[f"{_SYMBOL}#{name}"]
        )
    ]
    assert not accepted, ("gate variants the matrix accepted", accepted)
    unseen = [
        name
        for name in _VARIANTS
        if name not in _BEYOND_RULE and not rule[f"{_SYMBOL}#{name}"]
    ]
    assert not unseen, ("gate variants the actor rule accepted", unseen)


_RULE_SHAPES = (
    "r5-1a single role-permission removal",
    "r5-1b is_superuser",
    "r5-1c closed encounter",
    "rls touch",
)


def test_actor_rule_alone_refuses_every_r5_shape(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the matrix disabled (one state, so nothing to differ), the actor
    rule alone refuses each R5-1 shape and an RLS-relation touch, and still
    certifies the unmutated function. Each shape is refused by the channel
    that sees it (receipt printed per shape)."""
    with override_settings(**_SYNTHETIC):
        probe_world = _probe_world(rbac_graph, monkeypatch)
        probes = [
            probe
            for probe in _variant_probes(probe_world)
            if probe.symbol == _SYMBOL or probe.symbol.split("#")[1] in _RULE_SHAPES
        ]
        runs = exemption_probes.run_matrix(
            probes, probe_world, probe_world.matrix.states[:1]
        )
    for probe in probes:
        run = runs[probe.symbol]
        print("RULE", probe.symbol, run.observed)  # noqa: T201 - receipt
        assert len(run.outcomes) == 1
        found = exemption_probes.actor_problems(run)
        if probe.symbol == _SYMBOL:
            assert found == []
        else:
            assert found, probe.symbol
    observed = {
        probe.symbol.split("#")[1]: " ".join(
            runs[probe.symbol].observed.get("none", [])
        )
        for probe in probes[1:]
    }
    assert "calls clinic_app.has_permission" in observed[_RULE_SHAPES[0]]
    assert "calls clinic_app.load_current_user" in observed[_RULE_SHAPES[1]]
    assert "statement reads an actor setting" in observed[_RULE_SHAPES[2]]
    assert (
        "statement touches actor relation ehr_clinicaldocumentversion"
        in (observed[_RULE_SHAPES[3]])
    )


def test_reclassified_functions_observe_the_actor(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The four former exemptions the actor rule refused keep their probes as
    evidence: each observes the actor, and the census now classifies each by
    an executed boundary (direct) or a named one (delegated), never as an
    exemption."""
    rows = {row["symbol"]: row for row in INVENTORY["candidates"]}
    for symbol in exemption_probes.RECLASSIFIED:
        assert rows[symbol]["kind"] in {"direct", "delegated"}, symbol
        assert symbol not in exemption_probes.PROBES
    probes = [
        exemption_probes.ALL_PROBES[symbol]
        for symbol in sorted(exemption_probes.RECLASSIFIED)
    ]
    with override_settings(**_SYNTHETIC):
        probe_world = _probe_world(rbac_graph, monkeypatch)
        runs = exemption_probes.run_matrix(
            probes, probe_world, probe_world.matrix.states[:1]
        )
    for probe in probes:
        found = exemption_probes.actor_problems(runs[probe.symbol])
        print("RECLASSIFIED", probe.symbol, found)  # noqa: T201 - receipt
        assert found, probe.symbol


def _field_outcome(name: str, value: object) -> exemption_probes.Outcome:
    """The canonical outcome of a returned record holding ``name``."""
    record = dataclasses.make_dataclass("Record", [("state", str), (name, object)])
    return ("ok", exemption_probes.canonical(record("accepted", value)))


@pytest.mark.parametrize(
    ("symbol", "field", "present"),
    [
        ("apps.scheduling.waitlist.respond_to_offer", "appointment_id", uuid4()),
        (
            "apps.intake.patient_access.patient_session_overview",
            "idle_expires_at",
            datetime(2035, 1, 1, tzinfo=UTC),
        ),
    ],
)
def test_masks_keep_presence_and_type(symbol: str, field: str, present: object) -> None:
    """R5-1d: a mask blanks a field's per-call content, never whether it is
    there: None and a value stay distinct, two present values match."""
    normalize = exemption_probes.PROBES[symbol].normalize
    assert normalize is not None
    absent = normalize(_field_outcome(field, None))
    assert absent != normalize(_field_outcome(field, present))
    later = present + timedelta(seconds=1) if isinstance(present, datetime) else uuid4()
    assert normalize(_field_outcome(field, present)) == normalize(
        _field_outcome(field, later)
    )
    assert normalize(_field_outcome(field, present)) != normalize(
        _field_outcome(field, str(present))
    )


_INLINABLE = """
CREATE FUNCTION clinic_app.zz_inline_actor() RETURNS pg_catalog.uuid
LANGUAGE sql STABLE AS $f$
  SELECT NULLIF(pg_catalog.current_setting('app.current_user_id', true), '')::uuid
$f$;
"""
_BINDER = """
CREATE FUNCTION clinic_app.zz_bind_actor(actor pg_catalog.uuid) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $f$
BEGIN
  PERFORM pg_catalog.set_config('app.current_user_id', actor::text, true);
END $f$;
"""
_POLICY_TABLE = """
CREATE TABLE clinic_app.zz_actor_rows (owner_id pg_catalog.uuid);
ALTER TABLE clinic_app.zz_actor_rows ENABLE ROW LEVEL SECURITY;
CREATE POLICY zz_own ON clinic_app.zz_actor_rows TO clinic_app USING (
  owner_id = NULLIF(pg_catalog.current_setting('app.current_user_id', true), '')::uuid
);
"""


def test_actor_catalog_is_derived_and_fails_closed(rbac_graph: RbacGraph) -> None:
    """The actor channels come from the live system, and what cannot be
    observed at run time fails closed (all DDL rolled back): an inlinable
    actor-reading SQL function and a function that binds an actor setting
    raise; a new relation whose policy reads the actor joins the set."""
    with runtime_role():
        settings = actor_channels.actor_settings(
            lambda: tenant_context(rbac_graph.physician, rbac_graph.organization_a),
            rbac_graph.physician,
        )
    assert settings == {"app.current_user_id"}
    with connection.cursor() as cursor:
        catalog = actor_channels.actor_catalog(cursor, settings)
        graph = census.build_graph(census.live_decisions(cursor))
    assert {"has_permission", "load_current_user"} <= set(catalog.functions.values())
    assert "ehr_encounter" in catalog.relations
    accessors = actor_channels.python_accessors(graph, catalog)
    assert {
        "apps.identity.current_context._actor_uuid_from_guc",
        "apps.identity.current_context._load_current_actor",
        "apps.identity.current_context.current_actor_id",
        "apps.identity.current_context.require_permission",
    } <= set(accessors)
    for ddl, message in ((_INLINABLE, "may be inlined"), (_BINDER, "binds an actor")):
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(ddl)
                with pytest.raises(census.CensusError, match=message):
                    actor_channels.actor_catalog(cursor, settings)
            transaction.set_rollback(True)
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(_POLICY_TABLE)
            grown = actor_channels.actor_catalog(cursor, settings)
        transaction.set_rollback(True)
    assert "zz_actor_rows" in grown.relations


def test_an_unlisted_accessor_fails_closed(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Python accessor missing from the derived set still cannot hide: the
    statement it sends names its frame as an unlisted accessor."""
    missing = "apps.identity.current_context._actor_uuid_from_guc"
    with override_settings(**_SYNTHETIC):
        probe_world = _probe_world(rbac_graph, monkeypatch)
        observer = probe_world.observer
        trimmed = dataclasses.replace(
            observer,
            accessors={
                code: symbol
                for code, symbol in observer.accessors.items()
                if symbol != missing
            },
        )
        probe_world = dataclasses.replace(probe_world, observer=trimmed)
        probes = [
            probe
            for probe in _variant_probes(probe_world)
            if probe.symbol.endswith("#r5-1c closed encounter")
        ]
        runs = exemption_probes.run_matrix(
            probes, probe_world, probe_world.matrix.states[:1]
        )
    found = " ".join(runs[probes[0].symbol].observed["none"])
    assert f"unlisted accessor {missing} reaches the actor" in found

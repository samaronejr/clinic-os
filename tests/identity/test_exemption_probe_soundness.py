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
from types import FunctionType
from typing import TYPE_CHECKING

import pytest
from apps.ehr import finalization
from apps.identity import stepup
from apps.identity.current_context import CurrentActorError, require_permission
from apps.tenancy.db import tenant_context
from django.db import connection, transaction
from django.test import override_settings

from auth.stepup_test_support import STEP_UP_NOW
from identity import exemption_probes, probe_states
from identity import permission_gate_census as census
from identity.permission_inputs import decision_inputs
from identity.test_permission_parity import _SYNTHETIC, _probe_world
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
    "    CurrentActorError, current_actor_id, require_permission,\n"
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
}


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


def test_exemption_probes_refuse_every_r4_gate_variant(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R4-1 and its variants, each run by the registered _next_version
    probe (same context, input and class) against a mutated copy of the
    function: every one is refused, by the repair it targets, while the
    unmutated function is still certified in the same run."""
    registered = exemption_probes.PROBES[_SYMBOL]
    with override_settings(**_SYNTHETIC):
        probe_world = _probe_world(rbac_graph, monkeypatch)
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
        runs = exemption_probes.run_matrix(probes, probe_world)
    verdicts = {
        probe.symbol: exemption_probes.problems(probe, runs[probe.symbol])
        for probe in probes
    }
    for symbol, found in verdicts.items():
        print("VERDICT", symbol, found or "certified")  # noqa: T201 - receipt
    assert verdicts[_SYMBOL] == []
    accepted = [
        name
        for name, (*_, repair) in _VARIANTS.items()
        if not any(
            problem.startswith(_EXPECTED[repair])
            for problem in verdicts[f"{_SYMBOL}#{name}"]
        )
    ]
    assert not accepted, ("gate variants the census accepted", accepted)

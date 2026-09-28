"""The principal_scope gate prover accepts only shapes it can prove (no DB)."""

from __future__ import annotations

import pytest

from identity.machine_gate import (
    EARLY_RETURN,
    NOT_GATED,
    SETTING_WRITE,
    UNBOUND_CLINIC,
    UNGATED_UUID,
    member_gate,
)

# principal_has's signature; the gate's clinic must be its uuid parameter.
PARAMETERS = ("perm", "clinic")
TYPES = ("text", "uuid")
PRINCIPAL = "NULLIF(current_setting('app.current_principal',true),'')::uuid"
OWN_CLINIC = (
    "(SELECT s.clinic_id FROM clinic_app.identity_serviceprincipal s"  # noqa: S608 - fixed analyzer input, never executed.
    f" WHERE s.id = {PRINCIPAL})"
)


def _gate(clinic: str = "clinic") -> str:
    return f"clinic_app.principal_scope({PRINCIPAL}, {clinic})"


GATE = _gate()
SQL_SHAPES = {
    "and_gate": (f"SELECT {GATE} IS NOT NULL AND clinic IS NOT NULL", []),
    "gate_only": (f"SELECT {GATE} IS NOT NULL;", []),
    "nested_or_in_case": (
        f"SELECT {GATE} IS NOT NULL AND CASE WHEN a OR b THEN true END",
        [],
    ),
    "or_setting": (
        f"SELECT {GATE} IS NOT NULL OR current_setting('app.current_principal',"
        "true) = 'x'",
        [NOT_GATED],
    ),
    "or_argument": (
        f"SELECT {GATE} IS NOT NULL OR EXISTS (SELECT 1 FROM t WHERE t.a = clinic)",  # noqa: S608 - fixed analyzer input, never executed.
        [NOT_GATED],
    ),
    "or_before_and": (f"SELECT a OR b AND {GATE} IS NOT NULL", [NOT_GATED]),
    "parenthesized_or": (f"SELECT ({GATE} IS NOT NULL OR a)", [NOT_GATED]),
    "negated": (f"SELECT NOT {GATE} IS NOT NULL AND a", [NOT_GATED]),
    "compared": (f"SELECT {GATE} IS NOT NULL = false AND a", [NOT_GATED]),
    "is_null": (f"SELECT {GATE} IS NULL AND a", [NOT_GATED]),
    "coalesced": (f"SELECT COALESCE({GATE} IS NOT NULL, true)", [NOT_GATED]),
    "case_wrapped": (
        f"SELECT CASE WHEN {GATE} IS NOT NULL THEN a ELSE true END",
        [NOT_GATED],
    ),
    "between": (f"SELECT {GATE} IS NOT NULL AND a BETWEEN 1 AND 2", [NOT_GATED]),
    "from_where": (f"SELECT true FROM t WHERE {GATE} IS NOT NULL", [NOT_GATED]),  # noqa: S608 - fixed analyzer input, never executed.
    "union": (f"SELECT {GATE} IS NOT NULL UNION SELECT true", [NOT_GATED]),
    "two_statements": (
        f"SELECT set_config('a.b','c',true); SELECT {GATE} IS NOT NULL",
        [SETTING_WRITE, NOT_GATED],
    ),
    "cte": (f"WITH x AS (SELECT 1) SELECT {GATE} IS NOT NULL", [NOT_GATED]),
    "quoted_name": (
        'SELECT "clinic_app"."principal_scope"(a, clinic) IS NOT NULL',
        [NOT_GATED],
    ),
    "other_schema": (
        "SELECT public.principal_scope(a, clinic) IS NOT NULL",
        [NOT_GATED],
    ),
    "unbalanced": (f"SELECT {GATE} IS NOT NULL AND (a", [NOT_GATED]),
    # The gate must check the clinic the caller asked about (review round 6).
    "own_clinic_subquery": (
        f"SELECT {_gate(OWN_CLINIC)} IS NOT NULL",
        [UNBOUND_CLINIC],
    ),
    "constant_clinic": (
        f"SELECT {_gate(chr(39) + '00000000-0000-0000-0000-000000000001' + chr(39))}"
        " IS NOT NULL",
        [UNBOUND_CLINIC],
    ),
    "coalesce_clinic": (
        f"SELECT {_gate(f'COALESCE(clinic, {OWN_CLINIC})')} IS NOT NULL",
        [UNBOUND_CLINIC],
    ),
    "cast_clinic": (f"SELECT {_gate('clinic::uuid')} IS NOT NULL", [UNBOUND_CLINIC]),
    "positional_clinic": (f"SELECT {_gate('$2')} IS NOT NULL", [UNBOUND_CLINIC]),
    "quoted_clinic": (
        f"SELECT {_gate(chr(34) + 'clinic' + chr(34))} IS NOT NULL",
        [UNBOUND_CLINIC],
    ),
    "qualified_clinic": (f"SELECT {_gate('f.clinic')} IS NOT NULL", [UNBOUND_CLINIC]),
    "undeclared_name": (f"SELECT {_gate('tenant')} IS NOT NULL", [UNBOUND_CLINIC]),
    "text_parameter": (f"SELECT {_gate('perm')} IS NOT NULL", [UNBOUND_CLINIC]),
    "three_arguments": (f"SELECT {_gate('clinic, a')} IS NOT NULL", [UNBOUND_CLINIC]),
    "one_bound_gate_suffices": (
        f"SELECT {_gate(OWN_CLINIC)} IS NOT NULL AND {GATE} IS NOT NULL",
        [],
    ),
    # No setting writes anywhere (review round 7).
    "set_config_conjunct": (
        f"SELECT {GATE} IS NOT NULL"
        " AND set_config('app.current_user_id', '', true) IS NOT NULL",
        [SETTING_WRITE],
    ),
    "quoted_set_config": (
        f"SELECT {GATE} IS NOT NULL AND \"set_config\"('app.x', '', true) = ''",
        [SETTING_WRITE],
    ),
}
# principal_has's reviewed shape, with the regions around the gate marked.
PLPGSQL = """DECLARE registered_tenant uuid; principal uuid; tenant uuid;{declare}
BEGIN
 {pre}
 registered_tenant := clinic_app.principal_scope(
  NULLIF(current_setting('app.current_principal',true),'')::uuid,{clinic});
 {between}
 IF {condition} THEN RETURN {refusal}; END IF;
 {post}
 principal := NULLIF(current_setting('app.current_principal',true),'')::uuid;
 tenant := NULLIF(current_setting('app.current_tenant',true),'')::uuid;
 IF tenant IS NULL OR tenant<>registered_tenant THEN RETURN false; END IF;
 RETURN EXISTS (SELECT 1 FROM clinic_app.identity_serviceprincipalgrant g
  WHERE g.principal_id=principal AND g.permission=perm);
{handlers}END """
SET_LOCAL = "SET LOCAL app.current_user_id = '';"
PLPGSQL_SHAPES = {
    "reviewed": ({}, []),
    "refusal_null": ({"refusal": "NULL"}, []),
    "no_handler": ({"handlers": ""}, []),
    "handler_grants": (
        {"handlers": "EXCEPTION WHEN others THEN RETURN true;\n"},
        [EARLY_RETURN],
    ),
    # Nothing may run before the gate (review round 7).
    "raise_before_gate": ({"pre": "RAISE NOTICE 'sintetico';"}, [NOT_GATED]),
    "early_true": ({"pre": "IF a THEN RETURN true; END IF;"}, [NOT_GATED]),
    "early_paren_true": ({"pre": "IF a THEN RETURN(true); END IF;"}, [NOT_GATED]),
    "early_if_exists": (
        {"pre": "IF EXISTS (SELECT 1 FROM t) THEN RETURN true; END IF;"},
        [NOT_GATED],
    ),
    "early_in_loop": ({"pre": "LOOP RETURN true; END LOOP;"}, [NOT_GATED]),
    "execute_before": ({"pre": "EXECUTE 'SELECT 1';"}, [NOT_GATED]),
    "reassigned_before": ({"pre": "clinic := principal;"}, [NOT_GATED]),
    "set_local_before": (
        {"pre": f"IF (SELECT count(*) FROM t) > 0 THEN {SET_LOCAL} END IF;"},  # noqa: S608 - fixed analyzer input, never executed.
        [SETTING_WRITE, NOT_GATED],
    ),
    "declare_set_config": (
        {"declare": " x text := set_config('app.current_user_id', '', true);"},
        [SETTING_WRITE, NOT_GATED],
    ),
    "declare_default": (
        {"declare": " x text DEFAULT current_setting('app.current_user_id', true);"},
        [NOT_GATED],
    ),
    "declare_cursor": ({"declare": " c CURSOR FOR SELECT 1;"}, [NOT_GATED]),
    "declare_shadow": ({"declare": " clinic uuid;"}, [UNBOUND_CLINIC]),
    "alias_positional": ({"declare": " c ALIAS FOR $2;"}, [UNBOUND_CLINIC]),
    # The refusal IF must follow the gate immediately, in its exact form.
    "not_adjacent": ({"between": "registered_tenant := principal;"}, [NOT_GATED]),
    "gate_returns_true": ({"refusal": "true"}, [NOT_GATED]),
    "other_disjunct": (
        {"condition": "tenant IS NULL OR registered_tenant IS NULL"},
        [NOT_GATED],
    ),
    "subquery_gate": (
        {"clinic": "(SELECT s.clinic_id FROM t s WHERE s.id = principal)"},
        [UNBOUND_CLINIC],
    ),
    "constant_gate": (
        {"clinic": "'00000000-0000-0000-0000-000000000001'::uuid"},
        [UNBOUND_CLINIC],
    ),
    # No setting writes after the gate either.
    "reset_after": ({"post": "RESET app.current_user_id;"}, [SETTING_WRITE]),
    "set_local_after": ({"post": SET_LOCAL}, [SETTING_WRITE]),
    "set_config_after": (
        {"post": "PERFORM set_config('app.current_tenant', 'x', true);"},
        [SETTING_WRITE],
    ),
    "update_set_after": ({"post": "UPDATE t SET a = 1;"}, [SETTING_WRITE]),
    # Reassigning after the gate only narrows a validated decision.
    "reassigned_after": ({"post": "clinic := principal;"}, []),
}


def _plpgsql(**parts: str) -> str:
    values = {
        "declare": "",
        "pre": "",
        "clinic": "clinic",
        "between": "",
        "condition": "registered_tenant IS NULL",
        "refusal": "false",
        "post": "",
        "handlers": "EXCEPTION WHEN invalid_text_representation THEN RETURN false;\n",
    }
    return PLPGSQL.format(**{**values, **parts})


def _kinds(problems: list[str]) -> list[str]:
    return [problem.split(":", 1)[0] for problem in problems]


def _check(
    language: str,
    source: str,
    parameters: tuple[str, ...] = PARAMETERS,
    types: tuple[str, ...] = TYPES,
) -> list[str]:
    return _kinds(
        member_gate(
            language=language,
            source=source,
            returns_boolean=True,
            parameters=parameters,
            types=types,
        )
    )


@pytest.mark.parametrize("shape", sorted(SQL_SHAPES))
def test_sql_member_gate(shape: str) -> None:
    body, expected = SQL_SHAPES[shape]
    assert _check("sql", body) == expected


@pytest.mark.parametrize("shape", sorted(PLPGSQL_SHAPES))
def test_plpgsql_member_gate(shape: str) -> None:
    parts, expected = PLPGSQL_SHAPES[shape]
    assert _check("plpgsql", _plpgsql(**parts)) == expected


MINIMAL = "BEGIN v := {gate}; IF v IS NULL THEN RETURN false; END IF; RETURN true; END"


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (MINIMAL.format(gate=_gate("clinic")), []),
        # The gate inside a nested block or loop is not the first statement.
        (
            f"BEGIN BEGIN v := {GATE}; IF v IS NULL THEN RETURN false; END IF; END;"
            " RETURN true; END",
            [NOT_GATED],
        ),
        (
            f"BEGIN LOOP v := {GATE}; IF v IS NULL THEN RETURN false; END IF; EXIT;"
            " END LOOP; RETURN true; END",
            [NOT_GATED],
        ),
        # The assignment must be exactly the helper call.
        (
            f"BEGIN v := COALESCE({GATE}, a); IF v IS NULL THEN RETURN false;"
            " END IF; RETURN true; END",
            [NOT_GATED],
        ),
        # An ELSIF branch is not the refusal IF.
        (
            f"BEGIN v := {GATE}; IF v IS NULL THEN RETURN false; ELSIF a THEN"
            " RETURN false; END IF; RETURN true; END",
            [NOT_GATED],
        ),
        # A gated RETURN is not the accepted plpgsql gate form.
        (f"BEGIN RETURN {GATE} IS NOT NULL AND a; END", [NOT_GATED]),
        ("BEGIN RETURN true; END", [NOT_GATED]),
        ("BEGIN NULL; END", [NOT_GATED]),
        ("SELECT 1", [NOT_GATED]),
        ("BEGIN IF a THEN NULL; END", [NOT_GATED]),
        # The gate's result variable may not be the clinic parameter itself.
        (
            f"BEGIN clinic := {GATE}; IF clinic IS NULL THEN RETURN false; END IF;"
            " RETURN true; END",
            [UNBOUND_CLINIC],
        ),
    ],
)
def test_plpgsql_control_flow_is_proven_or_refused(
    source: str, expected: list[str]
) -> None:
    assert _check("plpgsql", source) == expected


@pytest.mark.parametrize(
    ("language", "source", "parameters", "types", "expected"),
    [
        # Review round 7 (D1-r7b): the gate checks one uuid, the member decides
        # for another.
        (
            "sql",
            f"SELECT {_gate('clinic')} IS NOT NULL AND target_clinic IS NOT NULL",
            ("clinic", "target_clinic"),
            ("uuid", "uuid"),
            [UNGATED_UUID],
        ),
        (
            "plpgsql",
            MINIMAL.format(gate=_gate("clinic")),
            ("clinic", "target_clinic"),
            ("uuid", "uuid"),
            [UNGATED_UUID],
        ),
        # Each uuid parameter behind its own gate conjunct is accepted.
        (
            "sql",
            f"SELECT {_gate('clinic')} IS NOT NULL"
            f" AND {_gate('target_clinic')} IS NOT NULL",
            ("clinic", "target_clinic"),
            ("uuid", "uuid"),
            [],
        ),
        # A clinic can hide in an array or a domain over uuid: fail closed.
        (
            "sql",
            f"SELECT {GATE} IS NOT NULL AND targets IS NOT NULL",
            ("perm", "clinic", "targets"),
            ("text", "uuid", "uuid[]"),
            [UNGATED_UUID],
        ),
        (
            "plpgsql",
            MINIMAL.format(gate=_gate("clinic")),
            ("clinic", "target"),
            ("uuid", "clinic_app.clinic_ref"),
            [UNGATED_UUID],
        ),
        # Unnamed or partly named parameters cannot be bound.
        ("sql", f"SELECT {GATE} IS NOT NULL", ("",), ("uuid",), [UNBOUND_CLINIC]),
        (
            "sql",
            f"SELECT {GATE} IS NOT NULL",
            ("clinic",),
            ("uuid", "uuid"),
            [UNGATED_UUID],
        ),
    ],
)
def test_every_uuid_parameter_is_gated(
    language: str,
    source: str,
    parameters: tuple[str, ...],
    types: tuple[str, ...],
    expected: list[str],
) -> None:
    assert _check(language, source, parameters, types) == expected


@pytest.mark.parametrize(
    ("language", "returns_boolean"), [("c", True), ("plv8", True), ("sql", False)]
)
def test_unprovable_member_kinds_fail_closed(
    language: str, returns_boolean: bool
) -> None:
    problems = member_gate(
        language=language,
        source=f"SELECT {GATE} IS NOT NULL",
        returns_boolean=returns_boolean,
        parameters=PARAMETERS,
        types=TYPES,
    )
    assert _kinds(problems) == [NOT_GATED]

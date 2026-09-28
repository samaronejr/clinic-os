"""The principal_scope gate prover accepts only shapes it can prove (no DB)."""

from __future__ import annotations

import pytest

from identity.machine_gate import (
    EARLY_RETURN,
    NOT_GATED,
    UNBOUND_CLINIC,
    gate_clinic,
    member_gate,
)

# principal_has's parameters; the gate's clinic must be one of them.
PARAMETERS = ("perm", "clinic")
PRINCIPAL = "NULLIF(current_setting('app.current_principal',true),'')::uuid"
OWN_CLINIC = (
    "(SELECT s.clinic_id FROM clinic_app.identity_serviceprincipal s"  # noqa: S608 - fixed analyzer input, never executed.
    f" WHERE s.id = {PRINCIPAL})"
)

GATE = (
    "clinic_app.principal_scope(NULLIF(current_setting('app.current_principal',"
    "true),'')::uuid, clinic)"
)
SQL_SHAPES = {
    "and_gate": (f"SELECT {GATE} IS NOT NULL AND clinic IS NOT NULL", None),
    "gate_only": (f"SELECT {GATE} IS NOT NULL;", None),
    "nested_or_in_case": (
        f"SELECT {GATE} IS NOT NULL AND CASE WHEN a OR b THEN true END",
        None,
    ),
    "or_setting": (
        f"SELECT {GATE} IS NOT NULL OR current_setting('app.current_principal',"
        "true) = 'x'",
        NOT_GATED,
    ),
    "or_argument": (
        f"SELECT {GATE} IS NOT NULL OR EXISTS (SELECT 1 FROM t WHERE t.a = clinic)",  # noqa: S608 - fixed analyzer input, never executed.
        NOT_GATED,
    ),
    "or_before_and": (f"SELECT a OR b AND {GATE} IS NOT NULL", NOT_GATED),
    "parenthesized_or": (f"SELECT ({GATE} IS NOT NULL OR a)", NOT_GATED),
    "negated": (f"SELECT NOT {GATE} IS NOT NULL AND a", NOT_GATED),
    "compared": (f"SELECT {GATE} IS NOT NULL = false AND a", NOT_GATED),
    "is_null": (f"SELECT {GATE} IS NULL AND a", NOT_GATED),
    "coalesced": (f"SELECT COALESCE({GATE} IS NOT NULL, true)", NOT_GATED),
    "case_wrapped": (
        f"SELECT CASE WHEN {GATE} IS NOT NULL THEN a ELSE true END",
        NOT_GATED,
    ),
    "between": (f"SELECT {GATE} IS NOT NULL AND a BETWEEN 1 AND 2", NOT_GATED),
    "from_where": (f"SELECT true FROM t WHERE {GATE} IS NOT NULL", NOT_GATED),  # noqa: S608 - fixed analyzer input, never executed.
    "union": (f"SELECT {GATE} IS NOT NULL UNION SELECT true", NOT_GATED),
    "two_statements": (
        f"SELECT set_config('a.b','c',true); SELECT {GATE} IS NOT NULL",
        NOT_GATED,
    ),
    "cte": (f"WITH x AS (SELECT 1) SELECT {GATE} IS NOT NULL", NOT_GATED),
    "quoted_name": (
        'SELECT "clinic_app"."principal_scope"(a, clinic) IS NOT NULL',
        NOT_GATED,
    ),
    "other_schema": ("SELECT public.principal_scope(a, clinic) IS NOT NULL", NOT_GATED),
    "unbalanced": (f"SELECT {GATE} IS NOT NULL AND (a", NOT_GATED),
    # The gate must check the clinic the caller asked about (review round 6).
    "own_clinic_subquery": (
        f"SELECT clinic_app.principal_scope({PRINCIPAL}, {OWN_CLINIC}) IS NOT NULL",
        UNBOUND_CLINIC,
    ),
    "constant_clinic": (
        f"SELECT clinic_app.principal_scope({PRINCIPAL},"
        " '00000000-0000-0000-0000-000000000001'::uuid) IS NOT NULL",
        UNBOUND_CLINIC,
    ),
    "coalesce_clinic": (
        f"SELECT clinic_app.principal_scope({PRINCIPAL},"
        f" COALESCE(clinic, {OWN_CLINIC})) IS NOT NULL",
        UNBOUND_CLINIC,
    ),
    "cast_clinic": (
        f"SELECT clinic_app.principal_scope({PRINCIPAL}, clinic::uuid) IS NOT NULL",
        UNBOUND_CLINIC,
    ),
    "positional_clinic": (
        f"SELECT clinic_app.principal_scope({PRINCIPAL}, $2) IS NOT NULL",
        UNBOUND_CLINIC,
    ),
    "quoted_clinic": (
        f'SELECT clinic_app.principal_scope({PRINCIPAL}, "clinic") IS NOT NULL',
        UNBOUND_CLINIC,
    ),
    "qualified_clinic": (
        f"SELECT clinic_app.principal_scope({PRINCIPAL}, f.clinic) IS NOT NULL",
        UNBOUND_CLINIC,
    ),
    "undeclared_name": (
        f"SELECT clinic_app.principal_scope({PRINCIPAL}, tenant) IS NOT NULL",
        UNBOUND_CLINIC,
    ),
    "three_arguments": (
        f"SELECT clinic_app.principal_scope({PRINCIPAL}, clinic, a) IS NOT NULL",
        UNBOUND_CLINIC,
    ),
    "one_bound_gate_suffices": (
        f"SELECT clinic_app.principal_scope({PRINCIPAL}, {OWN_CLINIC}) IS NOT NULL"
        f" AND {GATE} IS NOT NULL",
        None,
    ),
}
# principal_has's reviewed shape, with the pre-gate and gate regions marked.
PLPGSQL = """DECLARE principal uuid; tenant uuid; registered_tenant uuid;{declare}
BEGIN
 BEGIN
  principal := NULLIF(current_setting('app.current_principal',true),'')::uuid;
  tenant := NULLIF(current_setting('app.current_tenant',true),'')::uuid;
 EXCEPTION WHEN invalid_text_representation THEN RETURN false;
 END;
 {pre}
 registered_tenant := clinic_app.principal_scope(principal,{clinic});
 {between}
 IF tenant IS NULL OR registered_tenant IS NULL OR tenant<>registered_tenant
 THEN RETURN {refusal}; END IF;
 {post}
 RETURN EXISTS (SELECT 1 FROM clinic_app.identity_serviceprincipalgrant g
  WHERE g.principal_id=principal AND g.permission=perm);
{handlers}END """
EARLY_TRUE = "IF tenant IS NULL THEN RETURN true; END IF;"
PLPGSQL_SHAPES = {
    "reviewed": ({}, None),
    "raise_before_gate": ({"pre": "RAISE EXCEPTION 'sintetico';"}, None),
    "refusal_null": ({"refusal": "NULL"}, None),
    "early_true": ({"pre": EARLY_TRUE}, EARLY_RETURN),
    "early_paren_true": ({"pre": "IF a THEN RETURN(true); END IF;"}, EARLY_RETURN),
    "early_if_exists": (
        {"pre": "IF EXISTS (SELECT 1 FROM t) THEN RETURN true; END IF;"},
        EARLY_RETURN,
    ),
    "early_if_not_exists": (
        {"pre": "IF NOT EXISTS (SELECT 1 FROM t) THEN RETURN true; END IF;"},
        EARLY_RETURN,
    ),
    "early_in_loop": ({"pre": "LOOP RETURN true; END LOOP;"}, EARLY_RETURN),
    "early_in_case": (
        {"pre": "CASE WHEN a THEN RETURN true; ELSE NULL; END CASE;"},
        EARLY_RETURN,
    ),
    "gate_returns_true": ({"refusal": "true"}, EARLY_RETURN),
    "not_adjacent": ({"between": "registered_tenant := principal;"}, EARLY_RETURN),
    "handler_grants": (
        {"handlers": "EXCEPTION WHEN others THEN RETURN true;\n"},
        EARLY_RETURN,
    ),
    "handler_refuses": (
        {"handlers": "EXCEPTION WHEN others THEN RETURN false;\n"},
        None,
    ),
    # The clinic parameter must reach the gate untouched (review round 6).
    "reassigned_before": ({"pre": "clinic := principal;"}, UNBOUND_CLINIC),
    "select_into_before": (
        {"pre": "SELECT s.clinic_id INTO clinic FROM t s;"},
        UNBOUND_CLINIC,
    ),
    "for_loop_before": (
        {"pre": "FOR clinic IN SELECT a FROM t LOOP NULL; END LOOP;"},
        UNBOUND_CLINIC,
    ),
    "declare_shadow": ({"declare": " clinic uuid;"}, UNBOUND_CLINIC),
    "alias_positional": (
        {"declare": " c ALIAS FOR $2;", "pre": "c := principal;"},
        UNBOUND_CLINIC,
    ),
    "subquery_gate": (
        {"clinic": "(SELECT s.clinic_id FROM t s WHERE s.id = principal)"},
        UNBOUND_CLINIC,
    ),
    "constant_gate": (
        {"clinic": "'00000000-0000-0000-0000-000000000001'::uuid"},
        UNBOUND_CLINIC,
    ),
    # Reassigning after the gate only narrows a validated decision.
    "reassigned_after": ({"post": "clinic := principal;"}, None),
}


def _plpgsql(**parts: str) -> str:
    values = {
        "declare": "",
        "pre": "",
        "clinic": "clinic",
        "between": "",
        "refusal": "false",
        "post": "",
        "handlers": "",
    }
    return PLPGSQL.format(**{**values, **parts})


def _kinds(problems: list[str]) -> list[str]:
    return [problem.split(":", 1)[0] for problem in problems]


@pytest.mark.parametrize("shape", sorted(SQL_SHAPES))
def test_sql_member_gate(shape: str) -> None:
    body, expected = SQL_SHAPES[shape]
    problems = member_gate(
        language="sql", source=body, returns_boolean=True, parameters=PARAMETERS
    )
    assert _kinds(problems) == ([expected] if expected else []), problems


@pytest.mark.parametrize("shape", sorted(PLPGSQL_SHAPES))
def test_plpgsql_member_gate(shape: str) -> None:
    parts, expected = PLPGSQL_SHAPES[shape]
    problems = member_gate(
        language="plpgsql",
        source=_plpgsql(**parts),
        returns_boolean=True,
        parameters=PARAMETERS,
    )
    assert _kinds(problems) == ([expected] if expected else []), problems


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        # The gate inside a nested block or loop does not dominate the outer return.
        (
            "BEGIN BEGIN v := clinic_app.principal_scope(a,b); IF v IS NULL THEN "
            "RETURN false; END IF; END; RETURN true; END",
            EARLY_RETURN,
        ),
        (
            "BEGIN LOOP v := clinic_app.principal_scope(a,b); IF v IS NULL THEN "
            "RETURN false; END IF; EXIT; END LOOP; RETURN true; END",
            EARLY_RETURN,
        ),
        # The assignment must be exactly the helper call.
        (
            "BEGIN v := COALESCE(clinic_app.principal_scope(a,b), a); IF v IS NULL "
            "THEN RETURN false; END IF; RETURN true; END",
            EARLY_RETURN,
        ),
        # An ELSIF branch is not a refusal-only gate.
        (
            "BEGIN v := clinic_app.principal_scope(a,b); IF v IS NULL THEN RETURN "
            "false; ELSIF a THEN RETURN false; END IF; RETURN true; END",
            EARLY_RETURN,
        ),
        # A gated RETURN conjunction is itself the gate.
        (f"BEGIN RETURN {GATE} IS NOT NULL AND a; END", None),
        ("BEGIN RETURN true; END", EARLY_RETURN),
        # The gate's result variable may not be the clinic parameter itself.
        (
            "BEGIN clinic := clinic_app.principal_scope(a, clinic); IF clinic IS NULL"
            " THEN RETURN false; END IF; RETURN true; END",
            UNBOUND_CLINIC,
        ),
        ("BEGIN NULL; END", NOT_GATED),
        ("SELECT 1", NOT_GATED),
        ("BEGIN IF a THEN NULL; END", NOT_GATED),
    ],
)
def test_plpgsql_control_flow_is_proven_or_refused(
    source: str, expected: str | None
) -> None:
    problems = member_gate(
        language="plpgsql", source=source, returns_boolean=True, parameters=PARAMETERS
    )
    assert _kinds(problems) == ([expected] if expected else []), problems


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
    )
    assert _kinds(problems) == [NOT_GATED]


def test_gate_clinic_names_the_checked_parameter() -> None:
    clinic = gate_clinic(language="plpgsql", source=_plpgsql(), parameters=PARAMETERS)
    assert clinic == "clinic"
    # Unnamed parameters can only be reached positionally, which is refused.
    problems = member_gate(
        language="sql",
        source=f"SELECT {GATE} IS NOT NULL",
        returns_boolean=True,
        parameters=("", ""),
    )
    assert _kinds(problems) == [UNBOUND_CLINIC]
    with pytest.raises(AssertionError):
        gate_clinic(
            language="sql",
            source=f"SELECT clinic_app.principal_scope({PRINCIPAL}, {OWN_CLINIC})"
            " IS NOT NULL",
            parameters=PARAMETERS,
        )

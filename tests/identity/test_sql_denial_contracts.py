"""Census SQL denial contracts: derived from pg_proc, declared once, redeemed once."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from django.core.exceptions import PermissionDenied
from django.db import DatabaseError, connection, transaction

from identity import legacy_sql_boundaries as sql_boundaries
from identity.legacy_parity_support import LEGACY, Boundary, exercise, world
from identity.sql_denial_contracts import (
    CONTRACTS,
    MANAGER_REFUSAL,
    Contract,
    Declared,
    Refusal,
    Shape,
    SqlArgument,
    SqlVerdict,
    contract,
    decide,
    derive,
    probe,
    redeem,
    sql_rowset,
)
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from identity.legacy_parity_support import LegacyWorld
    from rbac_fixtures import RbacGraph

MALFORMED = "malformed"
UUID_VALUE = UUID("00000000-0000-4000-8000-000000000013")
SET_OF_UUID = Contract("clinic_app.synthetic_set", Shape.SET, (UUID,), 1)
BOOLEAN = Contract("clinic_app.synthetic_boolean", Shape.BOOLEAN, (bool,), 1)
NULLABLE_UUID = Contract("clinic_app.synthetic_scalar", Shape.SCALAR, (UUID,), 1)
RAISING_SET = Contract(
    "clinic_app.synthetic_raising_set", Shape.SET, (UUID,), 1, MANAGER_REFUSAL
)
RAISING_COUNT = Contract(
    "clinic_app.synthetic_raising_count",
    Shape.SCALAR,
    (int,),
    1,
    MANAGER_REFUSAL,
    positive_count=True,
)


@pytest.mark.parametrize(
    ("found", "rows", "expected"),
    [
        (SET_OF_UUID, [], False),
        # The reviewer's survivor: a NULL row is data for a set function.
        (SET_OF_UUID, [(None,)], True),
        (SET_OF_UUID, [(None,), (UUID_VALUE,)], True),
        (SET_OF_UUID, [(UUID_VALUE,)], True),
        (SET_OF_UUID, [(str(UUID_VALUE),)], MALFORMED),
        (SET_OF_UUID, [(UUID_VALUE, None)], MALFORMED),
        (BOOLEAN, [(False,)], False),
        (BOOLEAN, [(True,)], True),
        (BOOLEAN, [], MALFORMED),
        (BOOLEAN, [(None,)], MALFORMED),
        (BOOLEAN, [("f",)], MALFORMED),
        (BOOLEAN, [(0,)], MALFORMED),
        (BOOLEAN, [(False,), (False,)], MALFORMED),
        (BOOLEAN, [(False,), (True,)], MALFORMED),
        (BOOLEAN, [(False, None)], MALFORMED),
        (NULLABLE_UUID, [(None,)], False),
        (NULLABLE_UUID, [(UUID_VALUE,)], True),
        (NULLABLE_UUID, [], MALFORMED),
        (NULLABLE_UUID, [(None,), (None,)], MALFORMED),
        (NULLABLE_UUID, [(None,), (UUID_VALUE,)], MALFORMED),
        (NULLABLE_UUID, [(False,)], MALFORMED),
        (NULLABLE_UUID, [(str(UUID_VALUE),)], MALFORMED),
        # Where the declared raise denies, no rowset is a denial.
        (RAISING_SET, [], MALFORMED),
        (RAISING_SET, [(UUID_VALUE,)], True),
        (RAISING_COUNT, [(None,)], MALFORMED),
        (RAISING_COUNT, [(True,)], MALFORMED),
        (RAISING_COUNT, [(0,)], MALFORMED),
        (RAISING_COUNT, [(-1,)], MALFORMED),
        (RAISING_COUNT, [(3,)], True),
    ],
)
def test_the_comparison_accepts_only_the_exact_derived_denial(
    found: Contract, rows: list[tuple[object, ...]], expected: bool | str
) -> None:
    if expected == MALFORMED:
        with pytest.raises(AssertionError, match="neither the exact denial"):
            decide(found, rows)
    else:
        assert decide(found, rows) is expected


def test_each_shape_derives_its_exact_denial() -> None:
    assert SET_OF_UUID.denial == ()
    assert BOOLEAN.denial == sql_rowset([(False,)])
    assert NULLABLE_UUID.denial == sql_rowset([(None,)])
    composite = Contract("clinic_app.synthetic_row", Shape.SCALAR, (UUID, str), 0)
    assert composite.denial == sql_rowset([(None, None)])
    assert RAISING_SET.denial is None
    assert RAISING_COUNT.denial is None


@pytest.mark.django_db
def test_unclassified_absent_and_mismatched_functions_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with connection.cursor() as cursor:
        with pytest.raises(AssertionError, match="unclassified"):
            contract("clinic_app.principal_scope", cursor)
        with pytest.raises(AssertionError, match="not exactly one"):
            derive("clinic_app.sql_contract_absent", cursor)
        for function, wrong in (
            ("clinic_app.retention_care_patients", Shape.SCALAR),
            ("clinic_app.ehr_version_scope", Shape.SET),
            ("clinic_app.user_has_org", Shape.SCALAR),
        ):
            with monkeypatch.context() as patch:
                patch.setitem(CONTRACTS, function, Declared(wrong))
                with pytest.raises(AssertionError, match="catalog shape"):
                    contract(function, cursor)


@pytest.mark.django_db
def test_only_the_declared_raise_is_a_denial(monkeypatch: pytest.MonkeyPatch) -> None:
    function = "clinic_app.patient_registry_count"
    arguments: list[SqlArgument] = ["sintetico-kek", uuid4(), "SINTETICO", None]
    with runtime_role(), transaction.atomic():
        verdict = probe(function, arguments)
        assert verdict.observed == MANAGER_REFUSAL
        assert redeem(verdict, function) is False
        for other in (
            None,
            Refusal("42501", "clinic manager authority is required."),
            Refusal("22023", MANAGER_REFUSAL.message),
        ):
            with monkeypatch.context() as patch:
                patch.setitem(CONTRACTS, function, Declared(Shape.SCALAR, other))
                with pytest.raises(DatabaseError):
                    probe(function, arguments)
        transaction.set_rollback(True)


@pytest.mark.django_db
def test_redeem_accepts_only_issued_unredeemed_verdicts() -> None:
    function = "clinic_app.user_has_org"
    with runtime_role(), transaction.atomic():
        with pytest.raises(AssertionError, match="bypassed probe"):
            redeem(not function, function)
        forged = SqlVerdict(function, allowed=False, observed=sql_rowset([(False,)]))
        with pytest.raises(AssertionError, match="bypassed probe"):
            redeem(forged, function)
        issued = probe(function, [uuid4()])
        assert issued.observed == sql_rowset([(False,)])
        assert redeem(issued, function) is False
        with pytest.raises(AssertionError, match="bypassed probe"):
            redeem(issued, function)
        with pytest.raises(AssertionError, match="verdict for"):
            redeem(probe(function, [uuid4()]), "clinic_app.waitlist_staff")
        with pytest.raises(AssertionError, match="arity"):
            probe(function, [])
        transaction.set_rollback(True)


def _bypass(w: LegacyWorld, valid: bool) -> bool:
    # The pre-contract adapter shape: its own verdict from its own rows.
    with connection.cursor() as cursor:
        organization = w.graph.organization_a if valid else w.graph.organization_b
        cursor.execute("SELECT clinic_app.user_has_org(%s)", [organization])
        return bool(cursor.fetchall() == [(True,)])


def _python_denial(w: LegacyWorld, valid: bool) -> object:
    raise PermissionDenied


@pytest.mark.django_db(transaction=True)
def test_census_harness_refuses_adapters_that_bypass_the_contract(
    rbac_graph: RbacGraph,
) -> None:
    subject = world(rbac_graph, "owner")
    symbol = "clinic_app.user_has_org"
    exercise(
        Boundary(
            symbol,
            "sql",
            LEGACY,
            lambda w, ok: sql_boundaries.query(
                "user_has_org",
                [w.graph.organization_a if ok else w.graph.organization_b],
            ),
        ),
        subject,
    )
    with pytest.raises(AssertionError, match="bypassed probe"):
        exercise(Boundary(symbol, "sql", LEGACY, _bypass), subject)
    with pytest.raises(PermissionDenied):
        exercise(Boundary(symbol, "sql", LEGACY, _python_denial), subject)
    with pytest.raises(AssertionError, match="verdict for"):
        exercise(
            Boundary(
                symbol,
                "sql",
                LEGACY,
                lambda w, ok: sql_boundaries.query("waitlist_staff", [w.clinic]),
            ),
            subject,
        )

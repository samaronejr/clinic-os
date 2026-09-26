"""Behavioural exemption check: change stored staff state, not the request.

Adapters supply real arguments, never authorization results. Missing adapters,
pre-target failures and unexpected errors fail closed. Static analysis is not
consulted by this gate.
"""

from __future__ import annotations

import sys
from contextlib import nullcontext
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import uuid4

from apps.audit.services import SystemAuditAccessRejectedError
from apps.identity.models import User, UserClinicRole
from django.core.management import CommandError
from django.db import DatabaseError, connection, transaction
from psycopg import sql

from identity.legacy_parity_support import DENIALS, target_code
from identity.permission_support import owner_context

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from types import FrameType
    from uuid import UUID

    from identity.guard_classification import Candidate

STAFF_STATES = (None, *UserClinicRole.Role.values)
EXEMPT_KINDS = frozenset({"nonstaff", "infrastructure"})


def completed(value: object) -> bool:
    return value is not False


@dataclass(frozen=True)
class DifferentialProbe:
    symbol: str
    invoke: Callable[[], object]
    decision: Callable[[object], bool] = completed
    expected: bool = True
    database_role: str = "clinic_app"
    patient_session: UUID | None = None
    owns_transaction: bool = False


class StaffDependentError(AssertionError):
    """An exemption changed its decision when only staff membership changed."""


def _membership(
    actor: User, clinic: UUID, organization: UUID, role: str | None, membership_id: UUID
) -> None:
    with owner_context(organization):
        UserClinicRole.objects.filter(user=actor).delete()
        if role is not None:
            UserClinicRole.objects.create(
                id=membership_id,
                user=actor,
                clinic_id=clinic,
                organization_id=organization,
                role=role,
            )
        assert list(
            UserClinicRole.objects.filter(user=actor).values_list("role", flat=True)
        ) == ([] if role is None else [role])


def _invoke(probe: DifferentialProbe, actor: User, organization: UUID) -> bool:
    code = target_code(probe.symbol)
    assert code is not None
    entered = False

    def observe(frame: FrameType, event: str, _arg: object) -> None:
        nonlocal entered
        if event == "call" and frame.f_code is code:
            entered = True

    assert probe.database_role in {"clinic_app", "clinic_owner"}
    with connection.cursor() as cursor:
        cursor.execute(
            sql.SQL("SET ROLE {}").format(sql.Identifier(probe.database_role))
        )
        cursor.execute(
            "SELECT set_config('app.current_user_id', %s, false), "
            "set_config('app.current_tenant', %s, false), "
            "set_config('app.current_patient_session', %s, false)",
            [str(actor.pk), str(organization), str(probe.patient_session or "")],
        )
    previous = sys.getprofile()
    try:
        with nullcontext() if probe.owns_transaction else transaction.atomic():
            sys.setprofile(observe)
            try:
                result = probe.invoke()
                allowed = probe.decision(result)
            except (*DENIALS, SystemAuditAccessRejectedError, CommandError):
                allowed = False
            except DatabaseError as error:
                # Syntax, missing fixtures, constraint failures etc. are NOT
                # authorization refusals and cannot establish invariance.
                if getattr(error.__cause__, "sqlstate", None) != "42501":
                    raise
                allowed = False
            finally:
                sys.setprofile(previous)
                if not probe.owns_transaction:
                    transaction.set_rollback(True)
    finally:
        sys.setprofile(previous)
        with connection.cursor() as cursor:
            cursor.execute("RESET ROLE")
            cursor.execute(
                "SELECT set_config('app.current_user_id', '', false), "
                "set_config('app.current_tenant', '', false), "
                "set_config('app.current_patient_session', '', false)"
            )
    assert entered, (probe.symbol, "target_not_entered")
    return allowed


def assert_staff_invariant(
    probe: DifferentialProbe, *, actor: User, clinic: UUID, organization: UUID
) -> tuple[bool, ...]:
    """Replay the same invocation for the same UUID under all ten roles and none.

    Database effects roll back per invocation. Patient entry adapters enter
    the real patient boundary and roll back its own durable transaction.
    Staff fixture changes commit separately so those entry points stay real.
    """
    before = User.objects.filter(pk=actor.pk).values().get()
    membership_id = uuid4()
    with owner_context(organization):
        assert not UserClinicRole.objects.filter(user=actor).exists()
    decisions = []
    try:
        for role in STAFF_STATES:
            _membership(actor, clinic, organization, role, membership_id)
            decisions.append(_invoke(probe, actor, organization))
            assert User.objects.filter(pk=actor.pk).values().get() == before
    finally:
        _membership(actor, clinic, organization, None, membership_id)
    if len(set(decisions)) != 1:
        raise StaffDependentError(
            probe.symbol, dict(zip(STAFF_STATES, decisions, strict=True))
        )
    assert decisions == [probe.expected] * len(STAFF_STATES), (
        probe.symbol,
        "invalid_control",
        decisions,
    )
    return tuple(decisions)


def assert_behavioral_classifications(
    candidates: Sequence[Candidate],
    probes: Mapping[str, Sequence[DifferentialProbe]],
    *,
    actor: User,
    clinic: UUID,
    organization: UUID,
) -> dict[str, list[tuple[bool, ...]]]:
    # Shared patient/staff facades may retain supplementary patient-context
    # replays after being classified as delegated. This flag can only ADD
    # checks: a nonstaff/infrastructure row always requires execution.
    required = {
        row["symbol"]
        for row in candidates
        if row["kind"] in EXEMPT_KINDS or row.get("differential", False)
    }
    assert required == set(probes), (
        "missing_or_stale_differential_adapters",
        required ^ set(probes),
    )
    receipts: dict[str, list[tuple[bool, ...]]] = {}
    for symbol in sorted(required):
        assert probes[symbol], (symbol, "missing_scenarios")
        receipts[symbol] = []
        for probe in probes[symbol]:
            assert probe.symbol == symbol
            receipts[symbol].append(
                assert_staff_invariant(
                    probe,
                    actor=actor,
                    clinic=clinic,
                    organization=organization,
                )
            )
    return receipts

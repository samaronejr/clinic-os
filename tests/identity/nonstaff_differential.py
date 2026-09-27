"""Behavioural exemption check: change stored staff state, not the request.

Adapters supply real arguments, never authorization results. Missing adapters,
pre-target failures and unexpected errors fail closed. Static analysis is not
consulted by this gate.
"""

from __future__ import annotations

import sys
from contextlib import nullcontext
from dataclasses import dataclass
from functools import partial
from time import perf_counter
from typing import TYPE_CHECKING

from apps.audit.services import SystemAuditAccessRejectedError
from apps.identity.models import User, UserClinicRole
from django.core.management import CommandError
from django.db import DatabaseError, connection, transaction
from psycopg import sql

from identity.legacy_parity_support import DENIALS, target_code
from identity.nonstaff_parallel import replay_profiles
from identity.nonstaff_states import (
    ROLE_SUBSETS,
    ReplayScope,
    StaffState,
    assert_membership_schema,
    profiles,
    set_care_profile,
    set_memberships,
)
from identity.permission_support import owner_context

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from types import CodeType, FrameType
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
    """Retain the old singleton diagnostic and attach exhaustive state witnesses."""

    def __init__(
        self, symbol: str, legacy: dict[str | None, bool], witnesses: dict[str, bool]
    ) -> None:
        super().__init__(symbol, legacy)
        self.witnesses = witnesses


@dataclass(frozen=True)
class DifferentialReport:
    decisions: dict[str, list[tuple[bool, ...]]]
    subset_count: int
    profile_count: int
    state_count: int
    elapsed_seconds: float
    serial_profile_seconds: float | None = None
    parallel_profile_seconds: float | None = None


def _invoke(
    probe: DifferentialProbe,
    actor: User,
    organization: UUID,
    code: CodeType | None = None,
) -> bool:
    code = code or target_code(probe.symbol)
    assert code is not None
    entered = False

    def observe(frame: FrameType, event: str, _arg: object) -> None:
        nonlocal entered
        if event == "call" and frame.f_code is code:
            entered = True
            # A single genuine entry is the witness. Do not profile the whole
            # Django/render/crypto stack tens of thousands of times.
            sys.setprofile(previous)

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


def _check_decisions(
    probe: DifferentialProbe, decisions: list[bool], states: list[StaffState]
) -> None:
    if len(set(decisions)) != 1:
        legacy = {
            role: decisions[ROLE_SUBSETS.index(() if role is None else (role,))]
            for role in STAFF_STATES
        }
        first = decisions[0]
        changed = next(i for i, value in enumerate(decisions) if value != first)
        raise StaffDependentError(
            probe.symbol,
            legacy,
            {
                states[0].key(): first,
                states[changed].key(): decisions[changed],
            },
        )
    assert decisions == [probe.expected] * len(states), (
        probe.symbol,
        "invalid_control",
        decisions,
    )


def _replay(
    probes: Sequence[DifferentialProbe],
    *,
    actor: User,
    scope: ReplayScope,
    selected_profiles: tuple[tuple[str, str, str], ...] | None = None,
) -> DifferentialReport:
    start = perf_counter()
    assert_membership_schema()
    before = User.objects.filter(pk=actor.pk).values().get()
    with owner_context(scope.organization):
        assert not UserClinicRole.objects.filter(user=actor).exists()
    codes = [target_code(probe.symbol) for probe in probes]
    values: list[list[bool]] = [[] for _ in probes]
    states: list[StaffState] = []
    combinations = selected_profiles or tuple(profiles(scope.other_clinic, scope.care))
    previous_care = None
    try:
        for layout, care, patient in combinations:
            if scope.care is not None and previous_care != (care, patient):
                set_care_profile(actor, scope, care, patient)
                previous_care = (care, patient)
            for roles in ROLE_SUBSETS:
                state = StaffState(roles, layout, care, patient)
                set_memberships(actor, scope, state)
                states.append(state)
                for index, probe in enumerate(probes):
                    values[index].append(
                        _invoke(probe, actor, scope.organization, codes[index])
                    )
                assert User.objects.filter(pk=actor.pk).values().get() == before
            # A failure can stop further profiles, but acceptance requires every
            # subset in every profile. Never sample or cache a guard decision.
            for probe, decisions in zip(probes, values, strict=True):
                _check_decisions(probe, decisions, states)
    finally:
        if scope.care is not None:
            set_care_profile(actor, scope, "absent", "target")
        set_memberships(actor, scope, StaffState(()))
    receipts: dict[str, list[tuple[bool, ...]]] = {}
    for probe, decisions in zip(probes, values, strict=True):
        receipts.setdefault(probe.symbol, []).append(tuple(decisions))
    assert len(states) == len(ROLE_SUBSETS) * len(combinations)
    return DifferentialReport(
        receipts,
        len(ROLE_SUBSETS),
        len(combinations),
        len(states),
        perf_counter() - start,
    )


def assert_staff_invariant(
    probe: DifferentialProbe, *, actor: User, clinic: UUID, organization: UUID
) -> tuple[bool, ...]:
    """Standalone role power-set gate; the census additionally crosses scopes."""
    return _replay(
        [probe],
        actor=actor,
        scope=ReplayScope(clinic, organization),
    ).decisions[probe.symbol][0]


def assert_behavioral_classifications(
    candidates: Sequence[Candidate],
    probes: Mapping[str, Sequence[DifferentialProbe]],
    *,
    actor: User,
    scope: ReplayScope,
    parallel: bool = False,
) -> DifferentialReport:
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
    ordered = []
    for symbol in sorted(required):
        assert probes[symbol], (symbol, "missing_scenarios")
        for probe in probes[symbol]:
            assert probe.symbol == symbol
            ordered.append(probe)
    if not parallel:
        return _replay(ordered, actor=actor, scope=scope)
    groups = tuple(profiles(scope.other_clinic, scope.care))
    # Run the first complete profile both ways over identical seeded data.
    # This is an equality control, not a sampled replacement for any profile.
    serial = _replay(ordered, actor=actor, scope=scope, selected_profiles=(groups[0],))
    reports, elapsed = replay_profiles(
        groups,
        partial(_replay_group, ordered, actor, scope),
    )
    assert reports[0].decisions == serial.decisions
    assert reports[0].state_count == serial.state_count == len(ROLE_SUBSETS)
    merged: dict[str, list[tuple[bool, ...]]] = {}
    for report in reports:
        assert report.subset_count == len(ROLE_SUBSETS)
        for symbol, scenarios in report.decisions.items():
            if symbol not in merged:
                merged[symbol] = [() for _ in scenarios]
            for index, values in enumerate(scenarios):
                merged[symbol][index] += values
    count = sum(report.state_count for report in reports)
    assert count == len(groups) * len(ROLE_SUBSETS)
    assert sum(report.profile_count for report in reports) == len(groups)
    assert all(
        len(values) == count and len(set(values)) == 1
        for scenarios in merged.values()
        for values in scenarios
    )
    return DifferentialReport(
        merged,
        len(ROLE_SUBSETS),
        len(groups),
        count,
        elapsed,
        serial.elapsed_seconds,
        reports[0].elapsed_seconds,
    )


def _replay_group(
    probes: Sequence[DifferentialProbe],
    actor: User,
    scope: ReplayScope,
    group: tuple[tuple[str, str, str], ...],
) -> DifferentialReport:
    return _replay(probes, actor=actor, scope=scope, selected_profiles=group)

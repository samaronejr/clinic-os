"""Authority observation is the certificate; a small matrix is only a backstop."""

from __future__ import annotations

import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from time import perf_counter
from typing import TYPE_CHECKING
from uuid import UUID

from apps.audit.services import SystemAuditAccessRejectedError
from apps.identity.models import User, UserClinicRole
from apps.intake.patient_access import patient_session_context
from django.core.management import CommandError
from django.db import DatabaseError, connection, transaction
from psycopg import sql

from identity.authority_catalog import Catalog
from identity.authority_observer import AuthorityObserver
from identity.legacy_parity_support import DENIALS, target_code
from identity.nonstaff_states import (
    ROLE_CASES,
    ReplayScope,
    StaffState,
    add_authority_backstop,
    assert_membership_schema,
    backstop_states,
    set_care_profile,
    set_memberships,
)
from identity.permission_support import owner_context

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence
    from types import FrameType

    from identity.authority_observer import Touch
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
    patient_context: bool = False


class StaffDependentError(AssertionError):
    """A backstop witness, not a claim to enumerate all authority inputs."""

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
    observations: dict[str, tuple[Touch, ...]] = field(default_factory=dict)
    channels: dict[str, list[str]] = field(default_factory=dict)
    executed: frozenset[str] = frozenset()


@contextmanager
def _invocation_context(probe: DifferentialProbe) -> Iterator[None]:
    if probe.patient_context:
        # Trusted adapter preparation is outside the observed callable. Its
        # real patient boundary runs, including session validity and GUC reset.
        with patient_session_context(probe.patient_session or UUID(int=0)):
            try:
                yield
            finally:
                transaction.set_rollback(True)
    elif probe.owns_transaction:
        yield
    else:
        with transaction.atomic():
            try:
                yield
            finally:
                transaction.set_rollback(True)


def _bind_actor(probe: DifferentialProbe, actor: User, organization: UUID) -> None:
    """Trusted fixture binding, deliberately outside observed guard execution."""
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


def _invoke(
    probe: DifferentialProbe,
    actor: User,
    organization: UUID,
    *,
    authority: AuthorityObserver | None = None,
    errors: list[type[BaseException] | None] | None = None,
) -> bool:
    code = target_code(probe.symbol)
    assert code is not None
    entered = False
    error_type: type[BaseException] | None = None

    def observe(frame: FrameType, event: str, arg: object) -> None:
        nonlocal entered
        if authority is not None:
            authority.profile(frame, event, arg)
        if event == "call" and frame.f_code is code:
            entered = True
            if authority is None:
                sys.setprofile(previous)

    _bind_actor(probe, actor, organization)
    previous = sys.getprofile()
    try:
        with _invocation_context(probe):
            sys.setprofile(observe)
            try:
                if authority is None:
                    allowed = probe.decision(probe.invoke())
                else:
                    authority.install()
                    try:
                        allowed = probe.decision(probe.invoke())
                    finally:
                        authority.restore()
            except (*DENIALS, SystemAuditAccessRejectedError, CommandError) as error:
                error_type = type(error)
                allowed = False
            except DatabaseError as error:
                if getattr(error.__cause__, "sqlstate", None) != "42501":
                    raise
                error_type = type(error)
                allowed = False
            finally:
                sys.setprofile(previous)
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
    if errors is not None:
        errors.append(error_type)
    return allowed


def _check_decisions(
    probe: DifferentialProbe, decisions: list[bool], states: list[StaffState]
) -> None:
    if len(set(decisions)) != 1:
        legacy = {
            role: decisions[ROLE_CASES.index(() if role is None else (role,))]
            for role in STAFF_STATES
        }
        first = decisions[0]
        changed = next(i for i, value in enumerate(decisions) if value != first)
        raise StaffDependentError(
            probe.symbol,
            legacy,
            {states[0].key(): first, states[changed].key(): decisions[changed]},
        )
    assert decisions == [probe.expected] * len(states), (
        probe.symbol,
        "invalid_control",
        decisions,
    )


def _replay(
    probes: Sequence[DifferentialProbe], *, actor: User, scope: ReplayScope
) -> DifferentialReport:
    start = perf_counter()
    assert_membership_schema()
    before = User.objects.filter(pk=actor.pk).values().get()
    with owner_context(scope.organization):
        assert not UserClinicRole.objects.filter(user=actor).exists()
    values: list[list[bool]] = [[] for _ in probes]
    errors: list[list[type[BaseException] | None]] = [[] for _ in probes]
    states = list(backstop_states(scope))
    previous_care = None
    try:
        for state in states:
            User.objects.filter(pk=actor.pk).update(
                is_active=state.authority != "inactive"
            )
            if scope.care is not None and previous_care != (
                state.care,
                state.care_patient,
            ):
                set_care_profile(actor, scope, state.care, state.care_patient)
                previous_care = state.care, state.care_patient
            set_memberships(actor, scope, state)
            add_authority_backstop(actor, scope, state)
            for index, probe in enumerate(probes):
                values[index].append(
                    _invoke(
                        probe,
                        actor,
                        scope.organization,
                        errors=errors[index],
                    )
                )
            expected = {**before, "is_active": state.authority != "inactive"}
            assert User.objects.filter(pk=actor.pk).values().get() == expected
        for probe, decisions, raised in zip(probes, values, errors, strict=True):
            _check_decisions(probe, decisions, states)
            assert len(set(raised)) == 1, (probe.symbol, "exception_type_changed")
    finally:
        User.objects.filter(pk=actor.pk).update(is_active=before["is_active"])
        if scope.care is not None:
            set_care_profile(actor, scope, "absent", "target")
        set_memberships(actor, scope, StaffState(()))
    receipts: dict[str, list[tuple[bool, ...]]] = {}
    for probe, decisions in zip(probes, values, strict=True):
        receipts.setdefault(probe.symbol, []).append(tuple(decisions))
    return DifferentialReport(
        receipts, len(ROLE_CASES), len(states), len(states), perf_counter() - start
    )


def assert_staff_invariant(
    probe: DifferentialProbe, *, actor: User, clinic: UUID, organization: UUID
) -> tuple[bool, ...]:
    """Exercise regression examples only; observation separately certifies absence."""
    return _replay(
        [probe], actor=actor, scope=ReplayScope(clinic, organization)
    ).decisions[probe.symbol][0]


def assert_behavioral_classifications(
    candidates: Sequence[Candidate],
    probes: Mapping[str, Sequence[DifferentialProbe]],
    *,
    actor: User,
    scope: ReplayScope,
) -> DifferentialReport:
    start = perf_counter()
    required = {
        row["symbol"]: row
        for row in candidates
        if row["kind"] in EXEMPT_KINDS
        or row.get("differential", False)
        or row.get("observe", False)
    }
    assert set(required) == set(probes), (
        "missing_or_stale_differential_adapters",
        set(required) ^ set(probes),
    )
    assert required, "no_executable_guard_candidates"
    catalog = Catalog()
    channels = catalog.derive()
    observed: dict[str, tuple[Touch, ...]] = {}
    backstop = []
    executed = set()
    for symbol in sorted(required):
        assert probes[symbol], (symbol, "missing_scenarios")
        touches = set()
        for probe in probes[symbol]:
            assert probe.symbol == symbol
            observer = AuthorityObserver(catalog, channels)
            decision = _invoke(probe, actor, scope.organization, authority=observer)
            # Observation wins even if a plant catches a denial and returns True.
            if required[symbol]["kind"] in EXEMPT_KINDS:
                observer.assert_nonstaff(symbol)
                backstop.append(probe)
            assert decision is probe.expected, (symbol, "invalid_control", decision)
            touches.update(observer.touches)
        observed[symbol] = tuple(sorted(touches))
        if required[symbol].get("observe", False):
            assert any(kind != "opaque" for kind, _name in touches), (
                symbol,
                "declared_authority_channel_not_reached",
            )
            executed.add(symbol + "#observed")
    replay = _replay(backstop, actor=actor, scope=scope)
    return DifferentialReport(
        replay.decisions,
        replay.subset_count,
        replay.profile_count,
        replay.state_count,
        perf_counter() - start,
        observations=observed,
        channels={
            "relations": sorted(
                catalog.relations[oid].name for oid in channels.relations
            ),
            "columns": sorted(
                catalog.relations[oid].name + "." + column
                for oid in channels.relations
                for column in catalog.relations[oid].columns
            ),
            "python_accessors": sorted(
                set(observer._accessors.values())
                | set(observer._accessor_objects.values())
            ),
            "gucs": sorted(channels.settings),
            "functions": sorted(
                catalog.functions[oid].name for oid in channels.functions
            ),
        },
        executed=frozenset(executed),
    )

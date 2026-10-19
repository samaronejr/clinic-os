"""Bind merged candidates to executed domain authority and relation oracles."""

from __future__ import annotations

import sys
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from functools import partial
from inspect import unwrap
from secrets import token_hex
from typing import TYPE_CHECKING

from apps.identity.models import UserClinicRole
from django.test import override_settings

from auth.stepup_test_support import STEP_UP_NOW
from identity.legacy_parity_support import target_code
from identity.passkey_authority_oracle import run_passkey_census
from identity.permission_support import owner_context
from rbac_fixtures import RbacGraph
from rbac_fixtures import rbac_graph as seed_graph

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path
    from types import FrameType

    import pytest


class ExecutedProofs:
    """Issue a probe only after its callable executes and its oracle passes."""

    def __init__(self, symbols: set[str]) -> None:
        self.targets: dict[tuple[str, str], str] = {}
        self.certificates: dict[str, set[str]] = {}
        for symbol in symbols:
            code = target_code(symbol)
            assert code is not None, symbol
            self.targets[code.co_filename, code.co_qualname] = symbol
        assert len(self.targets) == len(symbols), "aliased census targets"

    def run(self, label: str, oracle: Callable[[], None]) -> None:
        entered: set[str] = set()

        def observe(frame: FrameType, event: str, _argument: object) -> None:
            if event == "call":
                key = frame.f_code.co_filename, frame.f_code.co_qualname
                if key in self.targets:
                    entered.add(self.targets[key])

        previous = sys.getprofile()
        sys.setprofile(observe)
        try:
            oracle()
        finally:
            sys.setprofile(previous)
        for symbol in entered:
            self.certificates.setdefault(symbol, set()).add(label)

    def executed(self) -> set[str]:
        missing = set(self.targets.values()) - set(self.certificates)
        assert not missing, ("missing merged authority oracles", sorted(missing))
        return {symbol + "#cut1" for symbol in self.certificates}


@contextmanager
def _resource_clock(patch: pytest.MonkeyPatch, database_url: str) -> Iterator[None]:
    # Reuse the scheduling fixture, including its independent authority clock
    # and ORM timestamp control. Its request protocol is supplied explicitly;
    # no scheduling-world clone is needed for this serial census.
    from types import SimpleNamespace  # noqa: PLC0415

    from scheduling.conftest import controlled_scheduling_clock  # noqa: PLC0415

    request = SimpleNamespace(
        config=SimpleNamespace(getoption=lambda _name: None),
        fixturenames=("resource_clock",),
        module=SimpleNamespace(__name__=__name__),
        getfixturevalue=lambda name: (
            database_url if name == "superuser_database_url" else None
        ),
    )
    clock = controlled_scheduling_clock._get_wrapped_function()(request, patch, None)
    assert next(clock) == datetime(2035, 6, 1, tzinfo=UTC)
    try:
        yield
    finally:
        assert next(clock, None) is None


def _scheduling_proofs(
    proofs: ExecutedProofs,
    graph: Callable[[], RbacGraph],
    patch: pytest.MonkeyPatch,
    database_url: str,
) -> None:
    """Run the stored-role, exact-permission and native-response witnesses."""
    from scheduling import (  # noqa: PLC0415
        test_resource_http,
        test_resource_permission_identity,
        test_resource_role_matrix,
        test_resource_staff_outcomes,
    )

    with patch.context() as clock_patch, _resource_clock(clock_patch, database_url):
        for name in sorted(test_resource_permission_identity.GUARDS):
            subject = graph()
            proofs.run(
                f"scheduling exact permission:{name}",
                partial(
                    test_resource_permission_identity.test_guard_decides_by_its_exact_permission,
                    subject,
                    name,
                ),
            )
        for role in test_resource_role_matrix.ROLES:
            for scheduling_oracle in (
                test_resource_role_matrix.test_configuration_boundaries_follow_the_role_bundle,
                test_resource_role_matrix.test_generation_command_follows_the_role_bundle,
                test_resource_role_matrix.test_booking_and_transitions_follow_the_role_bundle,
                test_resource_role_matrix.test_settings_route_follows_the_role_bundle,
            ):
                subject = graph()
                proofs.run(
                    f"{scheduling_oracle.__name__}:{role}",
                    partial(scheduling_oracle, subject, role),
                )
        subject = graph()
        proofs.run(
            "scheduling settings commands and scope refusals",
            lambda: (
                test_resource_http.test_settings_publish_generate_retire_and_reject_scope_tampering(
                    subject
                )
            ),
        )
        for role in (
            UserClinicRole.Role.SCHEDULER,
            UserClinicRole.Role.CLINIC_MANAGER,
        ):
            subject = graph()
            proofs.run(
                f"scheduling native booking and transition:{role}",
                partial(
                    test_resource_staff_outcomes.test_authorized_staff_prepare_book_move_and_reach_safe_destinations,
                    subject,
                    role,
                    htmx=False,
                ),
            )


def _scanner(local: pytest.MonkeyPatch, database_now: Callable[[], datetime]) -> None:
    """Observe scanner publication through the real memory broker."""
    from unittest import mock  # noqa: PLC0415

    from apps.workflows import engine, tasks  # noqa: PLC0415
    from django.db import connection  # noqa: PLC0415

    from patient_service_support import runtime_role  # noqa: PLC0415

    with runtime_role():
        now = database_now()
        with local.context() as clock:
            clock.setattr(engine, "utc_now", lambda: now)
            steps = engine.due_steps()
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM clinic_app.workflows_due_tasks(%s)",
                    [now],
                )
                due = tuple(row[0] for row in cursor.fetchall())
            assert steps or due, "scanner positive control is empty"
            with (
                mock.patch.object(
                    tasks.execute_step_job,
                    "apply_async",
                    wraps=tasks.execute_step_job.apply_async,
                ) as sent_steps,
                mock.patch.object(
                    tasks.escalate_job,
                    "apply_async",
                    wraps=tasks.escalate_job.apply_async,
                ) as sent_tasks,
            ):
                assert tasks.scan_due() == len(steps) + len(due)
            assert sent_steps.call_args_list == [
                mock.call(kwargs={"step_id": str(identifier)}) for identifier in steps
            ]
            assert sent_tasks.call_args_list == [
                mock.call(kwargs={"task_id": str(identifier)}) for identifier in due
            ]


def run_cut1_census(
    symbols: set[str],
    patch: pytest.MonkeyPatch,
    secret_root: Path,
    database_url: str,
    record_property: Callable[[str, object], None],
) -> set[str]:
    """Execute existing independent role, narrowing, scope and response proofs."""
    from apps.workflows import engine  # noqa: PLC0415

    from consent import test_authority as consent  # noqa: PLC0415
    from identity.domain_sql_oracles import DELEGATE_CASES  # noqa: PLC0415
    from patients import test_duplicates  # noqa: PLC0415
    from workflows import decision_sites, test_authority, test_engine  # noqa: PLC0415
    from workflows.guard_cases import seed_guard_world  # noqa: PLC0415
    from workflows.guard_discovery import discover_guards  # noqa: PLC0415

    proofs = ExecutedProofs(symbols)
    database_now = engine.utc_now

    def graph(*, workflows: bool = False) -> RbacGraph:
        result = seed_graph._get_wrapped_function()(secret_root)
        assert isinstance(result, RbacGraph)
        if workflows:
            for organization in (result.organization_a, result.organization_b):
                with owner_context(organization):
                    UserClinicRole.objects.filter(organization_id=organization).update(
                        workflow_bundle_version=2
                    )
        return result

    duplicate_cases = (
        test_duplicates.test_equal_identifiers_create_an_open_candidate_without_merging,
        test_duplicates.test_merge_recreates_forward_rows_with_links_and_fresh_access,
        test_duplicates.test_new_charge_for_an_aliased_identity_is_rejected,
        test_duplicates.test_native_review_form_applies_displayed_versions_and_requires_csrf,
        test_duplicates.test_survivor_portal_preserves_released_history_and_original_receipts,
        test_duplicates.test_unmerge_separates_views_without_moving_later_survivor_charges,
    )
    for duplicate_oracle in duplicate_cases:
        proofs.run(duplicate_oracle.__name__, partial(duplicate_oracle, graph()))
    for role in UserClinicRole.Role.values:
        proofs.run(
            f"duplicate exact role and persisted session:{role}",
            partial(
                test_duplicates.test_merge_authority_checks_every_stored_role_and_exact_live_session,
                graph(),
                role,
            ),
        )
    for consent_oracle in (
        consent.test_every_guard_refuses_every_catalog_role_without_its_permission,
        consent.test_inactive_foreign_clinic_and_narrowed_bundles_are_refused,
    ):
        subject = graph()
        proofs.run(consent_oracle.__name__, partial(consent_oracle, subject))
    _scheduling_proofs(proofs, graph, patch, database_url)
    with patch.context() as delegate_clock:
        delegate_clock.setattr(time, "time", lambda: STEP_UP_NOW)
        for name, delegate_oracle in DELEGATE_CASES.items():
            subject = graph()
            proofs.run(
                f"delegate stored authority, state, scope and response:{name}",
                partial(delegate_oracle, subject, secret_root.parent),
            )
    run_passkey_census(proofs, graph)
    secret = secret_root / "workflow-synthetic-callback.secret"
    assert not secret.exists()
    secret.write_text(token_hex(32), encoding="ascii")
    secret.chmod(0o600)
    try:
        with (
            override_settings(WORKFLOWS_SYNTHETIC_ENABLED=True),
            patch.context() as local,
        ):
            for case in test_authority.CASES:
                subject = graph(workflows=True)
                world = seed_guard_world(subject, local)
                with decision_sites.rewritten():
                    guard = discover_guards()[case.symbol]
                    code = unwrap(guard.function).__code__
                    proofs.run(
                        f"workflow role, state, relation and narrowing:{case.symbol}",
                        partial(
                            test_authority._verify_guard,
                            case,
                            world,
                            code,
                            local,
                            lambda _name, _value: None,
                        ),
                    )
            subject = graph(workflows=True)
            proofs.run(
                "workflow timer scanner and stored step scope",
                lambda: test_engine.test_timer_clock_and_scanner_no_early_execution(
                    subject, local
                ),
            )
            seed_guard_world(graph(workflows=True), local)

            proofs.run(
                "workflow scanner publishes exactly the stored due IDs",
                partial(_scanner, local, database_now),
            )
    finally:
        secret.unlink()
    record_property(
        "cut1_authority_certificates",
        {symbol: sorted(labels) for symbol, labels in proofs.certificates.items()},
    )
    return proofs.executed()

"""Executed workflow census adapters; no delegated label substitutes for a probe."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from apps.comms.adapters import PermanentSendError
from apps.workflows import engine, task_services, tasks

from identity.legacy_guard_inventory import discover
from identity.nonstaff_differential import DifferentialProbe
from workflows.guard_cases import (
    GuardCase,
    GuardWorld,
    allowed_result,
    guard_cases,
    seed_guard_world,
)

if TYPE_CHECKING:
    import pytest

    from identity.nonstaff_subjects import NonstaffSubjects


def _invoke_case(case: GuardCase, world: GuardWorld) -> object:
    try:
        return case.invoke(world)
    except PermanentSendError:
        return False


def workflow_probes(
    data: NonstaffSubjects, patch: pytest.MonkeyPatch
) -> list[DifferentialProbe]:
    world = seed_guard_world(data.legacy.graph, patch)
    published: list[object] = []

    def publish_job(**kwargs: object) -> None:
        published.append(kwargs)

    patch.setattr(tasks.execute_step_job, "apply_async", publish_job)
    patch.setattr(tasks.escalate_job, "apply_async", publish_job)
    cases = {case.symbol: case for case in guard_cases()}
    extra = {
        "apps.workflows.engine._step_scope": DifferentialProbe(
            "apps.workflows.engine._step_scope",
            lambda: engine._step_scope(world.pending.pk),
            bool,
        ),
        "apps.workflows.engine.due_steps": DifferentialProbe(
            "apps.workflows.engine.due_steps", engine.due_steps, bool
        ),
        "apps.workflows.tasks.scan_due": DifferentialProbe(
            "apps.workflows.tasks.scan_due", tasks.scan_due, bool
        ),
        "apps.workflows.task_services._command": DifferentialProbe(
            "apps.workflows.task_services._command",
            lambda: task_services._command(
                world.assigned, world.assigned.revision, "probe", {}
            ),
        ),
    }
    probes: list[DifferentialProbe] = []
    for symbol in discover():
        if not symbol.startswith("apps.workflows."):
            continue
        if symbol in extra:
            probes.append(extra[symbol])
        else:
            case = cases[symbol]
            probes.append(
                DifferentialProbe(
                    symbol,
                    partial(_invoke_case, case, world),
                    allowed_result,
                    expected=case.owns_transaction,
                    owns_transaction=case.owns_transaction,
                )
            )
    return probes

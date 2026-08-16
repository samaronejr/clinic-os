"""Replay the immutable stale action plan through durable outer prefixes."""

from __future__ import annotations

from typing import TYPE_CHECKING, Never, cast

from ops.testing.isolation_common import IsolationError, JsonObject, JsonValue
from ops.testing.isolation_stale_execution import (
    StaleExecutionAdapters,
    execute_stale_action,
)
from ops.testing.isolation_stale_journal import replace_recovery_journal
from ops.testing.isolation_stale_records import (
    complete_stale_action,
    mark_stale_resources_absent,
)
from ops.testing.isolation_stale_runner_recovery import (
    begin_stale_runner_removal,
    complete_stale_runner_removal,
    has_runner_recovery,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from ops.testing.isolation_stale_actions import StaleActionPlan

type Checkpoint = Callable[[str, JsonObject], None]


def complete_stale_physical_actions(
    path: Path,
    journal: JsonObject,
    plan: StaleActionPlan | None,
    emit: Checkpoint,
    adapters: StaleExecutionAdapters | None,
) -> JsonObject:
    """Persist every runner intent and exact physical action in plan order."""
    if journal.get("state") not in {"prepared", "cleaning"}:
        return journal
    if plan is None:
        _fail("physical stale recovery lost its prior action identities")
    actions = _objects(journal.get("resource_actions"), "resource actions")
    completed = _strings(journal.get("completed_action_ids"), "completed actions")
    if not actions:
        journal = mark_stale_resources_absent(journal)
        replace_recovery_journal(path, journal)
        return journal
    while len(completed) < len(actions):
        index = len(completed)
        action_id = _text(actions[index].get("action_id"), "action ID")
        runner_action = has_runner_recovery(journal, action_id)
        if runner_action:
            journal = begin_stale_runner_removal(journal, action_id)
            replace_recovery_journal(path, journal)
            emit("runner-remove-intent", journal)
        execute_stale_action(
            actions[index],
            plan.identities[index],
            previous_boot_id=_text(journal.get("previous_boot_id"), "previous boot"),
            current_boot_id=_text(journal.get("current_boot_id"), "current boot"),
            adapters=adapters,
        )
        emit("resource-action-applied", journal)
        journal = (
            complete_stale_runner_removal(journal, action_id)
            if runner_action
            else complete_stale_action(journal, action_id)
        )
        replace_recovery_journal(path, journal)
        if runner_action:
            emit("runner-removed", journal)
        emit("resource-action-completed", journal)
        completed = _strings(journal.get("completed_action_ids"), "completed actions")
    return journal


def _objects(value: JsonValue, context: str) -> list[JsonObject]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        _fail(f"{context} must be an object array")
    return cast("list[JsonObject]", value)


def _strings(value: JsonValue, context: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        _fail(f"{context} must be a string array")
    return cast("list[str]", value)


def _text(value: JsonValue, context: str) -> str:
    if not isinstance(value, str):
        _fail(f"{context} must be a string")
    return value


def _fail(message: str) -> Never:
    raise IsolationError(message)

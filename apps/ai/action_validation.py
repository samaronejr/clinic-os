"""Reloaded action facts shared by admission, approval and the send boundary."""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING

from config.settings.contracts import SYNTHETIC_DATA_MODE
from django.conf import settings
from django.db import connections

from apps.ai.action_contracts import (
    ActionAccessDeniedError,
    ActionRefusedError,
    Refusal,
)
from apps.ai.action_payload import (
    JSON,
    ActionPayload,
    closed,
    identifier,
    instant,
    text,
)
from apps.ai.agent_policies import TOOL_SCHEMAS
from apps.ai.contracts import Purpose
from apps.ai.models import AgentPolicyVersion, AgentRun
from apps.ai.prompt_registry import load_prompt
from apps.workflows.models import WorkflowDefinitionVersion

if TYPE_CHECKING:
    from uuid import UUID


def database_time(*, using: str = "default") -> datetime:
    """Use the database wall clock, not transaction-start time or a worker clock."""
    with connections[using].cursor() as cursor:
        cursor.execute("SELECT clock_timestamp()")
        row = cursor.fetchone()
    if row is None or not isinstance(row[0], datetime):
        raise ActionAccessDeniedError
    return row[0]


def authority_snapshot(*, clinic_id: UUID, permission: str) -> dict[str, JSON]:
    """Snapshot current canonical authority without accepting a caller identity."""
    with connections["default"].cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.agent_snapshot(%s,%s)", [clinic_id, permission]
        )
        row = cursor.fetchone()
    if row is None or row[0] is None:
        raise ActionAccessDeniedError
    value: JSON = json.loads(row[0]) if isinstance(row[0], str) else row[0]
    return closed(value, {"user_id", "permission", "memberships"})


def source_snapshot(*, clinic_id: UUID, source_id: UUID) -> dict[str, JSON]:
    """Capture an actual authorized publication, not a model's version claim."""
    source = WorkflowDefinitionVersion.objects.filter(
        pk=source_id,
        clinic_id=clinic_id,
    ).first()
    if source is None:
        raise ActionAccessDeniedError
    return {
        "kind": "workflow_definition",
        "id": str(source.pk),
        "version": source.version,
        "revision": source.revision,
        "content_sha256": source.fingerprint,
    }


def _source_facts(
    *, clinic_id: UUID, source_id: UUID, proposal_id: UUID, using: str
) -> dict[str, JSON]:
    if using == "agent":
        with connections[using].cursor() as cursor:
            cursor.execute(
                "SELECT clinic_app.agent_source(%s,%s)", [proposal_id, source_id]
            )
            row = cursor.fetchone()
        if row is None or row[0] is None:
            raise ActionRefusedError(Refusal.SOURCE)
        value: JSON = json.loads(row[0]) if isinstance(row[0], str) else row[0]
        return closed(
            value, {"version", "revision", "content_sha256", "state", "current"}
        )
    source = WorkflowDefinitionVersion.objects.filter(
        pk=source_id,
        clinic_id=clinic_id,
    ).first()
    if source is None:
        raise ActionRefusedError(Refusal.SOURCE)
    return {
        "version": source.version,
        "revision": source.revision,
        "content_sha256": source.fingerprint,
        "state": source.state,
        "current": not WorkflowDefinitionVersion.objects.filter(
            clinic_id=clinic_id,
            key=source.key,
            state="published",
            version__gt=source.version,
        ).exists(),
    }


def current_controls(
    *, policy: AgentPolicyVersion, proposal_id: UUID, using: str
) -> dict[str, JSON]:
    """Read the same gateway switches and the current provider lifecycle."""
    del policy
    with connections[using].cursor() as cursor:
        cursor.execute("SELECT clinic_app.agent_controls(%s)", [proposal_id])
        row = cursor.fetchone()
    if row is None or row[0] is None:
        raise ActionAccessDeniedError
    value: JSON = json.loads(row[0]) if isinstance(row[0], str) else row[0]
    return closed(
        value, {"enabled", "policy_authority", "provider", "latest_policy_id"}
    )


def validate_current(
    *,
    payload: ActionPayload,
    policy: AgentPolicyVersion,
    proposal_id: UUID,
    using: str = "default",
) -> None:
    """Revalidate all bound sources, current policy, time and effect constraints."""
    data = payload.data()
    now = database_time(using=using)
    if now >= instant(data["expires_at"]):
        raise ActionRefusedError(Refusal.EXPIRED)
    if now < instant(data["not_before"]):
        raise ActionRefusedError(Refusal.EARLY)
    if data["clinic_id"] != str(policy.clinic_id) or data["organization_id"] != str(
        policy.organization_id
    ):
        raise ActionAccessDeniedError
    terms = closed(
        data["approval_policy"], {"policy_id", "version", "quorum", "autonomy"}
    )
    controls = current_controls(policy=policy, proposal_id=proposal_id, using=using)
    if (
        controls["latest_policy_id"] != str(policy.pk)
        or not policy.enabled
        or terms["policy_id"] != str(policy.pk)
        or terms["version"] != policy.version
        or text(terms["autonomy"]) > policy.autonomy_ceiling
        or terms["autonomy"] == "A0"
        or data["compensation_policy"] != policy.compensation_policy
    ):
        raise ActionRefusedError(Refusal.POLICY)
    stored_prompt = (
        AgentRun.objects.using(using)
        .filter(actionproposal__pk=proposal_id)
        .values_list("prompt_sha256", flat=True)
        .first()
    )
    if stored_prompt != load_prompt(Purpose(policy.capability))[1]:
        raise ActionRefusedError(Refusal.POLICY)
    _validate_effect(data, policy, terms)
    _validate_provider(data, controls)
    _validate_sources(data=data, policy=policy, proposal_id=proposal_id, using=using)


def _validate_effect(
    data: dict[str, JSON], policy: AgentPolicyVersion, terms: dict[str, JSON]
) -> None:
    operation = closed(data["operation"], {"name", "version"})
    name = operation["name"]
    if (
        not isinstance(name, str)
        or policy.tool_allowlist.get(name) != TOOL_SCHEMAS.get(name)
        or name not in policy.tool_allowlist
        or (name == "workflows.review_task" and terms["autonomy"] != "A1")
        or (
            name == "comms.synthetic_reminder" and terms["autonomy"] not in ("A2", "A3")
        )
        or (terms["autonomy"] == "A2" and terms["quorum"] != 0)
        or (terms["autonomy"] != "A2" and terms["quorum"] not in (1, 2))
    ):
        raise ActionRefusedError(Refusal.TOOL)
    # No registered command accepts clinical, monetary or real-address effects.
    # Adding one requires its own deterministic validator, not an allowlist edit.
    if (
        data["patient_id"] is not None
        or data["document"] is not None
        or data["recipients"] != []
        or data["amounts"]
        != {
            "currency": "BRL",
            "centavos": "0",
            "direction": "none",
            "allocations": [],
        }
    ):
        raise ActionRefusedError(Refusal.TOOL)
    snapshots = data["approver_authority_snapshots"]
    allowed = (
        {"tasks.assign"}
        if terms["autonomy"] == "A1"
        else {
            "automation.admin",
            "automation.organization",
        }
    )
    if not isinstance(snapshots, list) or (terms["autonomy"] == "A2" and snapshots):
        raise ActionRefusedError(Refusal.QUORUM)
    users: set[str] = set()
    for raw in snapshots:
        snapshot = closed(raw, {"user_id", "memberships", "permission"})
        user = text(snapshot["user_id"])
        if snapshot["permission"] not in allowed or user in users:
            raise ActionRefusedError(Refusal.AUTHORITY)
        users.add(user)


def _validate_provider(data: dict[str, JSON], controls: dict[str, JSON]) -> None:
    if (
        settings.CLINIC_DATA_MODE != SYNTHETIC_DATA_MODE
        or controls["enabled"] is not True
    ):
        raise ActionRefusedError(Refusal.DISABLED)
    if controls["policy_authority"] is not True:
        raise ActionRefusedError(Refusal.AUTHORITY)
    provider = controls["provider"]
    if (
        not isinstance(provider, dict)
        or provider.get("provider") != "synthetic"
        or provider.get("environment") != "synthetic"
        or provider.get("state") in ("degraded", "revoked")
        or data["provider"]
        != {
            key: provider.get(key)
            for key in ("capability_version", "account", "environment", "api_version")
        }
    ):
        raise ActionRefusedError(Refusal.POLICY)


def _validate_sources(
    *, data: dict[str, JSON], policy: AgentPolicyVersion, proposal_id: UUID, using: str
) -> None:
    subject = closed(data["subject"], {"kind", "id", "expected_state", "revision"})
    sources = data["source_versions"]
    if not isinstance(sources, list) or not sources:
        raise ActionRefusedError(Refusal.SOURCE)
    found_subject = False
    for raw in sources:
        source = closed(raw, {"kind", "id", "version", "revision", "content_sha256"})
        if source["kind"] != "workflow_definition":
            raise ActionRefusedError(Refusal.SOURCE)
        facts = _source_facts(
            clinic_id=policy.clinic_id,
            source_id=identifier(source["id"]),
            proposal_id=proposal_id,
            using=using,
        )
        if (
            facts["current"] is not True
            or facts["state"] != "published"
            or any(
                source[key] != facts[key]
                for key in ("version", "revision", "content_sha256")
            )
        ):
            raise ActionRefusedError(Refusal.SOURCE)
        if source["id"] == subject["id"] and source["kind"] == subject["kind"]:
            found_subject = (
                subject["expected_state"] == facts["state"]
                and subject["revision"] == facts["revision"]
            )
    if not found_subject:
        raise ActionRefusedError(Refusal.SOURCE)

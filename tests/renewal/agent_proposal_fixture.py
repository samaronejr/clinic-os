"""Owner-only synthetic setup for the real browser exact-effect approval flow."""

from __future__ import annotations

import json
import os
import secrets
import sys
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import django


def seed(actor: UUID, clinic: UUID, organization: UUID) -> tuple[UUID, int]:
    """Use production domain commands; only role and secret setup is owner-side."""
    from apps.ai.action_payload import parse_payload  # noqa: PLC0415
    from apps.ai.action_validation import (  # noqa: PLC0415
        authority_snapshot,
        database_time,
        source_snapshot,
    )
    from apps.ai.agent_policies import (  # noqa: PLC0415
        PolicyTerms,
        publish_agent_policy,
    )
    from apps.ai.controls import set_organization_policy  # noqa: PLC0415
    from apps.ai.models import AgentPolicyVersion  # noqa: PLC0415
    from apps.ai.owner import set_global_switch  # noqa: PLC0415
    from apps.ai.proposals import propose_action  # noqa: PLC0415
    from apps.identity.clinic_configuration import (  # noqa: PLC0415
        ConfigurationContent,
        latest_configuration,
        publish_configuration,
    )
    from apps.identity.models import UserClinicRole  # noqa: PLC0415
    from apps.providers.lifecycle import VersionInput, propose_version  # noqa: PLC0415
    from apps.tenancy.db import tenant_context  # noqa: PLC0415
    from apps.workflows.services import publish_definition  # noqa: PLC0415

    from ai.support import model, policy  # noqa: PLC0415
    from ai.test_action_payload import payload_json  # noqa: PLC0415
    from identity.permission_support import owner_context  # noqa: PLC0415
    from patient_service_support import runtime_role  # noqa: PLC0415

    secret = Path(os.environ["CLINIC_SECRET_DIR"]) / "agent-attestation-v1.secret"
    if not secret.exists():
        descriptor = os.open(secret, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(secrets.token_hex(32))
    with owner_context(organization):
        UserClinicRole.objects.filter(user_id=actor).exclude(
            workflow_bundle_version=2,
        ).update(workflow_bundle_version=2)
    account = "synthetic-" + uuid4().hex
    for capability in ("llm_inference", "email"):
        version = propose_version(
            capability,
            version_input=VersionInput(
                provider="synthetic",
                account=account,
                environment="synthetic",
                api_version="v1",
                region="synthetic",
                retention_terms="synthetic-no-retention",
            ),
        )
    set_global_switch(enabled=True, approved_regions=("br-synthetic",))
    model()
    with runtime_role(), tenant_context(actor, organization):
        configuration = latest_configuration(clinic)
        if configuration is None:
            configuration = publish_configuration(
                clinic_id=clinic,
                expected_version=0,
                content=ConfigurationContent("Synthetic agent clinic"),
            )
        set_organization_policy(
            clinic_id=clinic, expected_version=configuration.version, policy=policy()
        )
        previous = (
            AgentPolicyVersion.objects.filter(clinic_id=clinic, capability="front_desk")
            .order_by("-version")
            .first()
        )
        capability_policy = publish_agent_policy(
            clinic_id=clinic,
            expected_version=previous.version if previous else 0,
            terms=PolicyTerms(
                "front_desk", "A1", ("workflows.review_task",), enabled=True
            ),
        )
        source = publish_definition(
            clinic_id=clinic,
            key="synthetic-browser-agent-" + uuid4().hex,
            steps=[{"handler": "timer", "seconds": 1}],
            idempotency_key=uuid4(),
        )
        now = database_time()
        data = payload_json()
        data.update(
            {
                "action_id": str(uuid4()),
                "organization_id": str(organization),
                "clinic_id": str(clinic),
                "idempotency_key": str(uuid4()),
                "operation": {"name": "workflows.review_task", "version": 1},
                "subject": {
                    "kind": "workflow_definition",
                    "id": str(source.pk),
                    "expected_state": "published",
                    "revision": source.revision,
                },
                "source_versions": [
                    source_snapshot(clinic_id=clinic, source_id=source.pk)
                ],
                "provider": {
                    "capability_version": str(version.pk),
                    "account": account,
                    "environment": "synthetic",
                    "api_version": "v1",
                },
                "parameters": {
                    "due_at": (now + timedelta(days=1))
                    .isoformat()
                    .replace("+00:00", "Z")
                },
                "not_before": (now - timedelta(seconds=1))
                .isoformat()
                .replace("+00:00", "Z"),
                "expires_at": (now + timedelta(hours=1))
                .isoformat()
                .replace("+00:00", "Z"),
                "approver_authority_snapshots": [
                    authority_snapshot(clinic_id=clinic, permission="tasks.assign")
                ],
                "approval_policy": {
                    "policy_id": str(capability_policy.pk),
                    "version": capability_policy.version,
                    "quorum": 1,
                    "autonomy": "A1",
                },
                "compensation_policy": {"mode": "manual", "version": 1},
            }
        )
        proposal = propose_action(
            clinic_id=clinic, payload=parse_payload(json.dumps(data))
        )
        assert proposal.state == "validated", proposal.reason_code
        return proposal.pk, source.revision


def main() -> int:
    """The browser runner supplies the isolated owner connection and secret root."""
    django.setup()
    actor, clinic, organization = (UUID(value) for value in sys.argv[1:])
    proposal, revision = seed(actor, clinic, organization)
    sys.stdout.write(
        json.dumps({"proposal_id": str(proposal), "source_revision": revision}) + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

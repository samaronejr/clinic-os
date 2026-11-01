"""Owner-only synthetic setup and runtime dispatch for the extraction browser."""

from __future__ import annotations

import json
import sys
from uuid import UUID, uuid4

from apps.ai.contracts import Purpose
from apps.ai.controls import set_organization_policy
from apps.ai.models import ExtractionJob
from apps.ai.owner import set_global_switch
from apps.ai.policy import OrganizationPolicy
from apps.audit.models import AuditEvent
from apps.comms.models import IntegrationOperation
from apps.core.integration import execute_operation
from apps.identity.clinic_configuration import (
    ConfigurationContent,
    latest_configuration,
    publish_configuration,
)
from apps.identity.models import Clinic, User, UserClinicRole
from apps.orders.models import ResultAcknowledgment, ResultObservation
from apps.providers.lifecycle import VersionInput, propose_version
from apps.tenancy.db import tenant_context
from django.db import connection

from ai.support import model
from identity.permission_support import owner_context
from patient_service_support import runtime_role


def configure(*, organization: UUID, clinic: UUID) -> None:
    """Set only synthetic model terms and the existing organization opt-in."""
    with owner_context(organization):
        administrator = User.objects.create(
            username="synthetic-extraction-owner-" + uuid4().hex,
            is_active=True,
        )
        for clinic_id in Clinic.objects.filter(
            organization_id=organization
        ).values_list("pk", flat=True):
            UserClinicRole.objects.create(
                organization_id=organization,
                clinic_id=clinic_id,
                user=administrator,
                role=UserClinicRole.Role.OWNER,
            )
    propose_version(
        "llm_inference",
        version_input=VersionInput(
            provider="synthetic",
            account="synthetic-extraction-" + uuid4().hex,
            environment="synthetic",
            region="synthetic",
            retention_terms="synthetic-no-retention",
        ),
    )
    set_global_switch(enabled=True, approved_regions=("br-synthetic",))
    model(organization=organization, clinic=clinic, purpose=Purpose.EXTRACTION)
    with runtime_role(), tenant_context(administrator.pk, organization):
        configuration = latest_configuration(clinic)
        if configuration is None:
            configuration = publish_configuration(
                clinic_id=clinic,
                expected_version=0,
                content=ConfigurationContent("Synthetic extraction clinic"),
            )
        set_organization_policy(
            clinic_id=clinic,
            expected_version=configuration.version,
            policy=OrganizationPolicy(
                enabled=True,
                purposes=(Purpose.EXTRACTION.value,),
                monthly_budget_micros=10_000_000,
                purpose_budgets=((Purpose.EXTRACTION.value, 10_000_000),),
            ),
        )


def main() -> None:
    """Called only after django.setup with the runner's explicit owner URL."""
    data = json.loads(sys.stdin.read())
    organization, clinic = UUID(data["organization"]), UUID(data["clinic_a"])
    if data["action"] == "configure":
        configure(organization=organization, clinic=clinic)
        receipt: object = {"synthetic": True}
    elif data["action"] == "sinks":
        with owner_context(organization):
            audit = list(AuditEvent.objects.values())
            outbox = list(IntegrationOperation.objects.values())
        sentinel = data["sentinel"]
        assert audit
        assert outbox
        assert sentinel.startswith("SINTETICO-")
        receipt = {
            "audit_clean": sentinel not in json.dumps(audit, default=str),
            "outbox_clean": sentinel not in json.dumps(outbox, default=str),
            "last_error_clean": all(
                sentinel not in row["last_error"] for row in outbox
            ),
            "audit_rows": len(audit),
            "outbox_rows": len(outbox),
        }
    else:
        with owner_context(organization):
            job = ExtractionJob.objects.get(pk=UUID(data["job_id"]), clinic_id=clinic)
            operation = job.invocation.operation_id
        if data["action"] == "dispatch":
            assert operation is not None
            with runtime_role():
                with connection.cursor() as cursor:
                    cursor.execute("SELECT current_user")
                    principal = cursor.fetchone()
                assert principal is not None
                receipt = {
                    "state": execute_operation(operation_id=operation),
                    "runtime_role": principal[0],
                }
        else:
            assert data["action"] == "inspect"
            with (
                runtime_role(),
                tenant_context(UUID(data["physician_a_id"]), organization),
            ):
                observations = list(
                    ResultObservation.objects.filter(version_id=job.result_version_id)
                )
                receipt = {
                    "count": len(observations),
                    "imported": all(
                        row.source["provenance"] == "imported" for row in observations
                    ),
                    "confirmer": all(
                        row.source["confirmer"] == data["physician_a_id"]
                        for row in observations
                    ),
                    "anchored": all(
                        row.source["page"] == 1 and len(row.source["bbox"]) == 4
                        for row in observations
                    ),
                    "acknowledged": ResultAcknowledgment.objects.filter(
                        version_id=job.result_version_id
                    ).exists(),
                }
    sys.stdout.write(json.dumps(receipt, sort_keys=True) + "\n")

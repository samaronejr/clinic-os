"""Owner-only model lifecycle and the global stop; no external gate approvals."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from django.db import connection, transaction
from django.db.models import Max
from django.utils import timezone

from apps.ai.contracts import (
    MAX_MODEL_TEXT,
    MAX_TIMEOUT_SECONDS,
    REGION_CLASSES,
    GatewayInputError,
    Price,
    Purpose,
)
from apps.ai.models import AICircuit, GatewaySwitch, ModelConfiguration
from apps.audit.services import record_phase1_event
from apps.core.integration import hold_subject_mutation_key
from apps.identity.management.base import assert_owner_database_role
from apps.identity.models import Clinic

if TYPE_CHECKING:
    from collections.abc import Sequence


@dataclass(frozen=True, slots=True)
class ModelTerms:
    """A versioned owner review of synthetic inference terms, not an EG record."""

    purpose: Purpose
    provider: Literal["synthetic", "recorded_fixture"]
    model_id: str
    region: str
    retention_terms: str
    training_terms: str
    price: Price
    route: Literal["primary", "fallback"] = "primary"
    timeout_seconds: int = 30


@transaction.atomic
def propose_model(*, terms: ModelTerms) -> ModelConfiguration:
    """Create a new draft under owner authority; approved versions never mutate."""
    assert_owner_database_role()
    if (
        terms.region not in REGION_CLASSES
        or not terms.retention_terms
        or not terms.training_terms
        or not terms.model_id
        or len(terms.model_id) > MAX_MODEL_TEXT
        or terms.provider not in {"synthetic", "recorded_fixture"}
        or terms.route not in {"primary", "fallback"}
        or type(terms.timeout_seconds) is not int
        or not 1 <= terms.timeout_seconds <= MAX_TIMEOUT_SECONDS
    ):
        raise GatewayInputError
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            [f"ai-model:{terms.purpose.value}"],
        )
    version = ModelConfiguration.objects.filter(purpose=terms.purpose.value).aggregate(
        latest=Max("version")
    )["latest"]
    configuration = ModelConfiguration.objects.create(
        purpose=terms.purpose.value,
        capability_key=(
            "asr" if terms.purpose is Purpose.SCRIBE_ASR else "llm_inference"
        ),
        provider=terms.provider,
        model_id=terms.model_id,
        region=terms.region,
        jurisdiction_class=REGION_CLASSES[terms.region],
        endpoint=(
            "https://ai.synthetic.invalid/v1/inference"
            if terms.provider == "synthetic"
            else "https://recorded.synthetic.invalid/v1/inference"
        ),
        retention_terms=terms.retention_terms,
        training_terms=terms.training_terms,
        input_micros=terms.price.input_micros,
        output_micros=terms.price.output_micros,
        cached_micros=terms.price.cached_micros,
        audio_micros=terms.price.audio_micros,
        version=(version or 0) + 1,
        route=terms.route,
        timeout_seconds=terms.timeout_seconds,
    )
    record_phase1_event(
        "ai.model.drafted", clinic_id=UUID(int=0), affected_record_id=configuration.pk
    )
    return configuration


@transaction.atomic
def transition_model(
    *,
    configuration_id: UUID,
    state: Literal["approved", "active", "retired"],
    approval_ref: str = "",
) -> ModelConfiguration:
    """Advance the DB-enforced model lifecycle, never a provider lifecycle."""
    assert_owner_database_role()
    configuration = ModelConfiguration.objects.select_for_update().get(
        pk=configuration_id
    )
    if (
        state not in {"approved", "active", "retired"}
        or len(approval_ref) > MAX_MODEL_TEXT
        or (state == "approved" and not approval_ref)
    ):
        raise GatewayInputError
    configuration.state = state
    if approval_ref:
        configuration.approval_ref = approval_ref
    configuration.save(update_fields=("state", "approval_ref"))
    record_phase1_event(
        f"ai.model.{state}", clinic_id=UUID(int=0), affected_record_id=configuration.pk
    )
    return configuration


@transaction.atomic
def set_global_switch(*, enabled: bool, approved_regions: Sequence[str]) -> None:
    """Default-on kill switch; even enabled synthetic routing never authorizes live."""
    assert_owner_database_role()
    if type(enabled) is not bool or (
        len(set(approved_regions)) != len(approved_regions)
        or not set(approved_regions) <= set(REGION_CLASSES)
    ):
        raise GatewayInputError
    GatewaySwitch.objects.update_or_create(
        pk=1, defaults={"enabled": enabled, "approved_regions": list(approved_regions)}
    )
    record_phase1_event(
        "ai.global_switch.changed",
        clinic_id=UUID(int=0),
        affected_record_id=UUID(int=0),
    )


@transaction.atomic
def reset_circuit(*, clinic_id: UUID, configuration_id: UUID) -> int:
    """Close a retained circuit under owner database and explicit tenant scope."""
    assert_owner_database_role()
    clinic = Clinic.objects.filter(pk=clinic_id).first()
    if clinic is None:
        raise GatewayInputError
    hold_subject_mutation_key(f"ai-organization:{clinic.organization_id}")
    circuit = (
        AICircuit.objects.select_for_update()
        .filter(
            configuration_id=configuration_id,
            organization_id=clinic.organization_id,
            closed_at__isnull=True,
        )
        .first()
    )
    if circuit is None:
        return 0
    circuit.closed_at = timezone.now()
    circuit.save(update_fields=("closed_at",))
    record_phase1_event(
        "ai.circuit.closed", clinic_id=clinic_id, affected_record_id=circuit.pk
    )
    return 1

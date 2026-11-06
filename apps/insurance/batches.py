"""Pinned batches and explicit synthetic submission/acknowledgment receipts."""

from __future__ import annotations

from hashlib import sha256
from typing import TYPE_CHECKING, cast
from uuid import uuid5

from config.settings.contracts import resolved_data_mode
from django.conf import settings
from django.utils import timezone

from apps.insurance.access import audit, clinic_write, fingerprint, replay, scoped
from apps.insurance.errors import (
    InsuranceConflictError,
    InsuranceInputError,
    InsuranceUnavailableError,
)
from apps.insurance.guides import stored_facts
from apps.insurance.limits import MAX_BATCH_GUIDES
from apps.insurance.models import Batch, BatchGuide, Guide, PayerContract, Submission
from apps.insurance.tiss import generate_batch, validate_xml
from apps.insurance.tiss_types import BatchFacts
from apps.insurance.transitions import transition

if TYPE_CHECKING:
    from collections.abc import Sequence
    from uuid import UUID


def require_rehearsal() -> None:
    """No live operator leg exists; even activated capabilities cannot bypass this."""
    if not settings.INSURANCE_SYNTHETIC_ENABLED or resolved_data_mode() != "synthetic":
        raise InsuranceUnavailableError


def create_batch(
    *,
    clinic_id: UUID,
    contract_id: UUID,
    guide_ids: Sequence[UUID],
    command_id: UUID,
) -> Batch:
    """Freeze actual bytes, component pins and ordered guide versions at creation."""
    if not 1 <= len(guide_ids) <= MAX_BATCH_GUIDES or len(set(guide_ids)) != len(
        guide_ids
    ):
        raise InsuranceInputError
    digest = fingerprint([str(contract_id), [str(value) for value in guide_ids]])
    with clinic_write(clinic_id=clinic_id) as clinic:
        scoped(model=PayerContract, clinic_id=clinic_id, identifier=contract_id)
        existing = replay(
            model=Batch, clinic_id=clinic_id, command_id=command_id, digest=digest
        )
        if existing is not None:
            return existing
        guides = [
            scoped(model=Guide, clinic_id=clinic_id, identifier=value)
            for value in guide_ids
        ]
        if any(
            guide.contract_id != contract_id or guide.state != "validated"
            for guide in guides
        ):
            raise InsuranceConflictError
        number = str(Batch.objects.filter(clinic_id=clinic_id).count() + 1)
        versions = dict(settings.TISS_COMPONENT_VERSIONS)
        xml = generate_batch(
            facts=BatchFacts(
                number, timezone.now(), tuple(stored_facts(g) for g in guides)
            ),
            versions=versions,
        )
        row = Batch.objects.create(
            organization_id=clinic.organization_id,
            clinic_id=clinic_id,
            command_id=command_id,
            fingerprint=digest,
            contract_id=contract_id,
            number=number,
            guide_ids=[str(value) for value in guide_ids],
            versions=versions,
            xml=xml.decode("utf-8"),
            content_sha256=sha256(xml).hexdigest(),
        )
        for guide in guides:
            BatchGuide.objects.create(
                organization_id=clinic.organization_id,
                clinic_id=clinic_id,
                command_id=uuid5(command_id, str(guide.pk)),
                fingerprint=fingerprint([str(row.pk), str(guide.pk)]),
                batch=row,
                guide=guide,
            )
        audit(clinic_id=clinic_id, row=row, event="insurance.batch.draft")
        return row


def batch_bytes(batch: Batch) -> bytes:
    """Verify exact stored bytes before export, seal, send or reconciliation."""
    xml = cast("str", batch.xml).encode("utf-8")
    if sha256(xml).hexdigest() != batch.content_sha256:
        raise InsuranceConflictError
    validate_xml(xml=xml, versions=cast("dict[str, str]", batch.versions))
    return xml


def validate_batch(*, clinic_id: UUID, batch_id: UUID, command_id: UUID) -> Batch:
    """Retain validation separately from payer receipt."""
    with clinic_write(clinic_id=clinic_id) as clinic:
        row = scoped(model=Batch, clinic_id=clinic_id, identifier=batch_id)
        batch_bytes(row)
        transition(clinic=clinic, row=row, target="validated", command_id=command_id)
        return row


def seal_batch(*, clinic_id: UUID, batch_id: UUID, command_id: UUID) -> Batch:
    """Seal all included guide versions and the exact validated batch atomically."""
    with clinic_write(clinic_id=clinic_id) as clinic:
        row = scoped(model=Batch, clinic_id=clinic_id, identifier=batch_id)
        batch_bytes(row)
        changed = transition(
            clinic=clinic, row=row, target="sealed", command_id=command_id
        )
        if changed:
            for link in BatchGuide.objects.filter(batch=row, clinic_id=clinic_id):
                guide = scoped(
                    model=Guide, clinic_id=clinic_id, identifier=link.guide_id
                )
                transition(
                    clinic=clinic,
                    row=guide,
                    target="sealed",
                    command_id=uuid5(command_id, str(guide.pk)),
                )
        return row


def prepare_submission(
    *, clinic_id: UUID, batch_id: UUID, channel: str, command_id: UUID
) -> Submission:
    """Prepare a portal export or local webservice rehearsal."""
    require_rehearsal()
    if channel not in {"portal", "synthetic_webservice"}:
        raise InsuranceInputError
    digest = fingerprint([str(batch_id), channel])
    with clinic_write(clinic_id=clinic_id) as clinic:
        row = scoped(model=Batch, clinic_id=clinic_id, identifier=batch_id)
        existing = replay(
            model=Submission, clinic_id=clinic_id, command_id=command_id, digest=digest
        )
        if existing is not None:
            return existing
        contract = scoped(
            model=PayerContract, clinic_id=clinic_id, identifier=row.contract_id
        )
        if channel == "synthetic_webservice" and not contract.webservice:
            raise InsuranceUnavailableError
        batch_bytes(row)
        transition(
            clinic=clinic,
            row=row,
            target="submitting",
            command_id=uuid5(command_id, "submitting"),
        )
        submission = Submission.objects.create(
            organization_id=clinic.organization_id,
            clinic_id=clinic_id,
            command_id=command_id,
            fingerprint=digest,
            batch=row,
            channel=channel,
            content_sha256=row.content_sha256,
        )
        audit(
            clinic_id=clinic_id, row=submission, event="insurance.submission.prepared"
        )
        return submission


def export_submission(*, clinic_id: UUID, submission_id: UUID) -> bytes:
    """Return only the sealed bytes; exporting a file is not payer acknowledgment."""
    require_rehearsal()
    with clinic_write(clinic_id=clinic_id):
        row = scoped(model=Submission, clinic_id=clinic_id, identifier=submission_id)
        batch = scoped(model=Batch, clinic_id=clinic_id, identifier=row.batch_id)
        xml = batch_bytes(batch)
        audit(clinic_id=clinic_id, row=row, event="insurance.submission.exported")
        return xml


def submission_outcome(
    *, clinic_id: UUID, batch_id: UUID, outcome: str, command_id: UUID
) -> Batch:
    """Unknown delivery stays unknown; it does not trigger another submission."""
    require_rehearsal()
    if outcome not in {"submitted", "unknown", "acknowledged"}:
        raise InsuranceInputError
    with clinic_write(clinic_id=clinic_id) as clinic:
        row = scoped(model=Batch, clinic_id=clinic_id, identifier=batch_id)
        transition(clinic=clinic, row=row, target=outcome, command_id=command_id)
        return row

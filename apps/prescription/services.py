"""Author-only synthetic drafts; real prescription issuance remains unavailable."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from hashlib import sha256
from typing import TYPE_CHECKING, Any, NoReturn, TypedDict, Unpack

import rfc8785
from django.db import connection, transaction
from django.utils import timezone

from apps.audit.canonical import AuditEventInput
from apps.audit.services import record_event
from apps.ehr.models import Encounter
from apps.ehr.services import (
    ClinicalAccessDeniedError,
    ClinicalConflictError,
    _denied,
    record_denial,
)
from apps.identity.current_context import (
    CurrentActorError,
    current_actor_username,
    require_current_actor_clinic_roles,
)
from apps.identity.models import Clinic, UserClinicRole
from apps.intake.models import Patient
from apps.prescription.document_rendering import (
    DOCUMENT_INPUT_VERSION,
    RENDER_PROFILE,
    RENDERER_ID,
    DocumentRenderer,
    DocumentRenderError,
    default_renderer,
    validate_pdf_structure,
)
from apps.prescription.models import (
    PrescriptionDocument,
    PrescriptionDraft,
    PrescriptionItem,
)
from apps.prescription.policy import ITEM_LIMITS, validate_category, validate_items

if TYPE_CHECKING:
    from uuid import UUID


class EncounterAssertions(TypedDict):
    """Required untrusted scope assertions, never caller-supplied authority."""

    encounter_id: UUID
    patient_id: UUID
    issuer_id: UUID


def issue_prescription() -> NoReturn:
    """Issue a prescription when the prescription domain is implemented."""
    message = "Phase >=1"
    raise NotImplementedError(message)


def authorize_encounter(*, clinic_id: UUID, encounter_id: UUID) -> Encounter:
    """Resolve canonical role and assignment; caller identity is never authority."""
    actor = require_current_actor_clinic_roles(
        clinic_id, (UserClinicRole.Role.PHYSICIAN,)
    )
    encounter = (
        Encounter.objects.select_related("appointment", "patient")
        .filter(pk=encounter_id, clinic_id=clinic_id)
        .first()
    )
    if encounter is None:
        raise ClinicalAccessDeniedError
    if (
        encounter.physician_id != actor
        or encounter.appointment.practitioner_id != actor
    ):
        record_denial(clinic_id, encounter.pk, "not_assigned")
        raise ClinicalAccessDeniedError
    return encounter


def _event(draft: PrescriptionDraft, action: str) -> None:
    record_event(
        AuditEventInput(
            event_type=f"prescription.draft.{action}",
            component_id="clinic-os-web",
            component_ip=None,
            affected_record_type="prescription.draft",
            affected_record_id=str(draft.pk),
            occurred_at_utc=timezone.now(),
        ),
        payload={"clinic_id": str(draft.clinic_id), "object_verb": action},
    )


def _scope(encounter: Encounter, patient_id: UUID, issuer_id: UUID) -> None:
    if encounter.patient_id != patient_id or encounter.physician_id != issuer_id:
        record_denial(encounter.clinic_id, encounter.pk, "not_assigned")
        raise ClinicalAccessDeniedError


def create_draft(
    *,
    clinic_id: UUID,
    encounter_id: UUID,
    patient_id: UUID,
    issuer_id: UUID,
    category: str,
) -> PrescriptionDraft:
    """Create or resume a scoped synthetic draft without replacing its content."""
    encounter = authorize_encounter(clinic_id=clinic_id, encounter_id=encounter_id)
    _scope(encounter, patient_id, issuer_id)
    contract = validate_category(category)
    with transaction.atomic():
        encounter = Encounter.objects.select_for_update().get(pk=encounter.pk)
        existing = PrescriptionDraft.objects.filter(encounter=encounter).first()
        if existing is not None:
            if existing.state != "draft":
                reason = "precondition_failed"
                raise ClinicalConflictError(reason)
            _event(existing, "viewed")
            return existing
        if encounter.state != "open":
            reason = "encounter_closed"
            raise ClinicalConflictError(reason)
        draft = PrescriptionDraft.objects.create(
            organization_id=encounter.organization_id,
            clinic_id=clinic_id,
            encounter=encounter,
            patient_id=patient_id,
            issuer_id=issuer_id,
            category=category,
            contract_version=contract,
        )
        _event(draft, "created")
        return draft


def view_draft(
    *,
    clinic_id: UUID,
    draft_id: UUID,
    encounter_id: UUID,
    patient_id: UUID,
    issuer_id: UUID,
) -> PrescriptionDraft:
    """Resume only the exact encounter/patient/issuer binding under FORCE RLS."""
    encounter = authorize_encounter(clinic_id=clinic_id, encounter_id=encounter_id)
    _scope(encounter, patient_id, issuer_id)
    draft = PrescriptionDraft.objects.filter(
        pk=draft_id,
        encounter=encounter,
        patient_id=patient_id,
        issuer_id=issuer_id,
        state="draft",
    ).first()
    if draft is None:
        raise ClinicalAccessDeniedError
    validate_category(draft.category)
    _event(draft, "viewed")
    return draft


def save_draft(
    *,
    clinic_id: UUID,
    draft_id: UUID,
    category: str,
    expected_version: int,
    items: list[dict[str, str]],
    **scope: Unpack[EncounterAssertions],
) -> PrescriptionDraft:
    """Lock, compare and append a complete snapshot; failed writes change nothing."""
    authorized = view_draft(clinic_id=clinic_id, draft_id=draft_id, **scope)
    validate_category(category)
    validate_items(items)
    with transaction.atomic():
        draft = _locked_draft(authorized.pk, expected_version)
        draft.version += 1
        draft.save(update_fields=("version", "updated_at"))
        PrescriptionItem.objects.bulk_create(
            [
                PrescriptionItem(
                    organization_id=draft.organization_id,
                    draft=draft,
                    version=draft.version,
                    position=position,
                    **item,
                )
                for position, item in enumerate(items, start=1)
            ]
        )
        _event(draft, "saved")
        return draft


def _locked_draft(draft_id: UUID, expected_version: int) -> PrescriptionDraft:
    draft = PrescriptionDraft.objects.select_for_update().filter(pk=draft_id).first()
    if draft is None or draft.state != "draft" or draft.version != expected_version:
        reason = "stale_revision"
        raise ClinicalConflictError(reason)
    return draft


VERIFICATION_BASE_URL = "https://verify.clinic-os.invalid/v/"
QR_HANDLE_BYTES = 32


class DocumentStorageError(Exception):
    """Report stored artifact bytes that fail their recorded digest."""


@dataclass(frozen=True, kw_only=True)
class DocumentDownload:
    """Fully materialized, authorized artifact bytes for a bounded response."""

    data: bytes
    content_type: str
    file_name: str


@dataclass(frozen=True, kw_only=True)
class DocumentIntegrity:
    """The digest verdict for one stored artifact; never a repair."""

    ok: bool
    reason_code: str
    input_digest: str
    pdf_digest: str


def _document_event(document: PrescriptionDocument, action: str) -> None:
    record_event(
        AuditEventInput(
            event_type=f"prescription.document.{action}",
            component_id="clinic-os-web",
            component_ip=None,
            affected_record_type="prescription.document",
            affected_record_id=str(document.pk),
            occurred_at_utc=timezone.now(),
        ),
        payload={"clinic_id": str(document.clinic_id), "object_verb": action},
    )


def _frozen_input(
    draft: PrescriptionDraft,
    *,
    verification_url: str,
    issued_at: str,
) -> dict[str, Any]:
    """Snapshot every rendered field; the digest covers exactly this mapping."""
    patient = Patient.objects.filter(pk=draft.patient_id).first()
    clinic = Clinic.objects.filter(pk=draft.clinic_id).first()
    if patient is None or clinic is None:
        raise ClinicalAccessDeniedError
    return {
        "v": DOCUMENT_INPUT_VERSION,
        "draft_id": str(draft.pk),
        "document_version": draft.version,
        "contract_version": draft.contract_version,
        "category": draft.category,
        "issuer_label": current_actor_username(),
        "patient_label": patient.full_name,
        "clinic_label": clinic.name,
        "issued_at": issued_at,
        "verification_url": verification_url,
        "items": draft_items(draft),
    }


def _input_digest(frozen: dict[str, Any]) -> str:
    """Digest the canonical frozen input minus the digest field itself."""
    content = {key: value for key, value in frozen.items() if key != "input_digest"}
    return sha256(rfc8785.dumps(content)).hexdigest()


def _render_params(verification_url: str) -> dict[str, Any]:
    """Record the exact render contract bound to the produced bytes."""
    return {
        "renderer": RENDERER_ID,
        "profile": RENDER_PROFILE,
        "page": "a4",
        "font": "helvetica-11",
        "qr": {"ecc": "M", "border": 4, "module_pt": 3},
        "verification_url": verification_url,
    }


def _validate_rendered(pdf_bytes: bytes, pdf_digest: str) -> None:
    """Reject malformed or mislabeled output before any artifact exists."""
    validate_pdf_structure(pdf_bytes)
    if sha256(pdf_bytes).hexdigest() != pdf_digest:
        raise DocumentRenderError


def render_document(
    *,
    clinic_id: UUID,
    draft_id: UUID,
    expected_version: int,
    renderer: DocumentRenderer | None = None,
    **scope: Unpack[EncounterAssertions],
) -> PrescriptionDocument:
    """Freeze the exact draft version into one immutable rendered artifact.

    The draft row stays locked for the whole operation, so the rendered
    version cannot change underneath. The document version, render
    parameters, frozen input and both digests are bound in the same row
    before any signing lifecycle exists. Rendering or dependency failures
    write nothing.
    """
    view_draft(
        clinic_id=clinic_id,
        draft_id=draft_id,
        encounter_id=scope["encounter_id"],
        patient_id=scope["patient_id"],
        issuer_id=scope["issuer_id"],
    )
    engine = renderer if renderer is not None else default_renderer()
    with transaction.atomic():
        draft = _locked_draft(draft_id, expected_version)
        if PrescriptionDocument.objects.filter(
            draft=draft, document_version=draft.version
        ).exists():
            reason = "already_rendered"
            raise ClinicalConflictError(reason)
        handle = secrets.token_urlsafe(QR_HANDLE_BYTES)
        verification_url = f"{VERIFICATION_BASE_URL}{handle}"
        issued_at = timezone.now().isoformat()
        frozen = _frozen_input(
            draft,
            verification_url=verification_url,
            issued_at=issued_at,
        )
        frozen["input_digest"] = _input_digest(frozen)
        rendered = engine.render(frozen)
        _validate_rendered(rendered.pdf_bytes, rendered.pdf_digest)
        document = PrescriptionDocument.objects.create(
            organization_id=draft.organization_id,
            draft=draft,
            encounter_id=draft.encounter_id,
            clinic_id=clinic_id,
            patient_id=draft.patient_id,
            issuer_id=draft.issuer_id,
            document_version=draft.version,
            state=PrescriptionDocument.State.RENDERED,
            render_params=_render_params(verification_url),
            frozen_input=frozen,
            input_digest=frozen["input_digest"],
            pdf_digest=rendered.pdf_digest,
            pdf_bytes=rendered.pdf_bytes,
            qr_handle=handle,
        )
        _document_event(document, "rendered")
        return document


def _authorize_document_care(clinic_id: UUID, encounter_id: UUID) -> None:
    """Recheck the clinical read predicate for the artifact's encounter."""
    encounter = Encounter.objects.filter(pk=encounter_id, clinic_id=clinic_id).first()
    if encounter is None:
        raise ClinicalAccessDeniedError
    try:
        require_current_actor_clinic_roles(clinic_id, (UserClinicRole.Role.PHYSICIAN,))
    except CurrentActorError:
        _denied(clinic_id, encounter_id, "role_denied")
    with connection.cursor() as cursor:
        cursor.execute("SELECT clinic_app.ehr_care(%s)", [encounter_id])
        care = cursor.fetchone()
    if not care or not care[0]:
        _denied(clinic_id, encounter_id, "no_care_relationship")


def download_document(*, clinic_id: UUID, document_id: UUID) -> DocumentDownload:
    """Authorize by role and care relationship, then serve exact bytes.

    The QR handle is never accepted here: it is a public verification token,
    not download authority. Stored bytes are re-checked against the recorded
    digest before they leave the tenant boundary.
    """
    document = PrescriptionDocument.objects.filter(
        pk=document_id, clinic_id=clinic_id
    ).first()
    if document is None:
        raise ClinicalAccessDeniedError
    _authorize_document_care(clinic_id, document.encounter_id)
    data = bytes(document.pdf_bytes)
    if sha256(data).hexdigest() != document.pdf_digest:
        raise DocumentStorageError
    _document_event(document, "downloaded")
    return DocumentDownload(
        data=data,
        content_type="application/pdf",
        file_name=f"prescricao-{document.pk}.pdf",
    )


def verify_document_integrity(document: PrescriptionDocument) -> DocumentIntegrity:
    """Recompute both digests from stored content; mismatches never repair."""
    recomputed_input = _input_digest(document.frozen_input)
    recomputed_pdf = sha256(bytes(document.pdf_bytes)).hexdigest()
    if recomputed_input != document.input_digest:
        return DocumentIntegrity(
            ok=False,
            reason_code="input_digest_mismatch",
            input_digest=recomputed_input,
            pdf_digest=recomputed_pdf,
        )
    if recomputed_pdf != document.pdf_digest:
        return DocumentIntegrity(
            ok=False,
            reason_code="pdf_digest_mismatch",
            input_digest=recomputed_input,
            pdf_digest=recomputed_pdf,
        )
    return DocumentIntegrity(
        ok=True,
        reason_code="verified",
        input_digest=recomputed_input,
        pdf_digest=recomputed_pdf,
    )


def draft_items(draft: PrescriptionDraft) -> list[dict[str, str]]:
    """Return the current author-visible snapshot, never discarded content."""
    return list(
        PrescriptionItem.objects.filter(draft=draft, version=draft.version).values(
            *ITEM_LIMITS
        )
    )


def discard_draft(
    *,
    clinic_id: UUID,
    draft_id: UUID,
    expected_version: int,
    **scope: Unpack[EncounterAssertions],
) -> None:
    """Retain an unwanted draft as terminal metadata, allowing encounter closure."""
    authorized = view_draft(clinic_id=clinic_id, draft_id=draft_id, **scope)
    with transaction.atomic():
        draft = _locked_draft(authorized.pk, expected_version)
        draft.state = "discarded"
        draft.version += 1
        draft.save(update_fields=("state", "version", "updated_at"))
        _event(draft, "discarded")

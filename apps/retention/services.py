"""Retention policies, legal holds, releases and controlled exports.

The deferred purge entrypoint stays unimplemented: no automatic disposal
exists until an approved disposal pipeline is built, and even then every
record class requires an approved policy and zero active holds. Exports are
online access-controlled operations that produce a manifest/digest package
of released versions only; staff secrets, audit internals and other-clinic
data never enter the package.
"""

from __future__ import annotations

import io
import json
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from typing import TYPE_CHECKING, NoReturn, cast
from uuid import uuid4

import rfc8785
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from apps.audit.canonical import AuditEventInput
from apps.audit.services import (
    AuditTrustedContext,
    _content_hash,
    _normalize_payload,
    record_event,
    record_phase1_event,
)
from apps.ehr.models import ClinicalDocumentVersion
from apps.ehr.services import (
    ClinicalAccessDeniedError,
    ClinicalConflictError,
    _appointment,
    _assigned,
    record_denial,
)
from apps.identity.current_context import (
    CurrentActorError,
    current_actor_id,
    require_current_actor_clinic_roles,
)
from apps.identity.models import Clinic, UserClinicRole
from apps.retention.models import (
    RECORD_CLASSES,
    LegalHold,
    RecordExport,
    RecordRelease,
    RetentionPolicy,
)
from apps.tenancy.envelope import reveal

if TYPE_CHECKING:
    from uuid import UUID

type JsonValue = (
    bool | int | str | float | None | Sequence[JsonValue] | Mapping[str, JsonValue]
)
MAX_TEXT = 255
MAX_EXPORT_RECORDS = 500
MANIFEST_NAME = "manifest.json"
ZIP_DATE = (1980, 1, 1, 0, 0, 0)
POLICY_ROLES = (UserClinicRole.Role.OWNER, UserClinicRole.Role.CLINIC_ADMIN)
STAFF_ROLES = (
    UserClinicRole.Role.PHYSICIAN,
    UserClinicRole.Role.RECEPTIONIST,
    UserClinicRole.Role.OWNER,
    UserClinicRole.Role.CLINIC_ADMIN,
)


class RetentionAccessDeniedError(Exception):
    """Non-enumerating retention boundary failure."""


class RetentionConflictError(Exception):
    """A fixed lifecycle conflict; no caller content appears in its message."""

    def __init__(self, reason_code: str) -> None:
        """Retain the machine-consumed conflict reason."""
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(frozen=True, kw_only=True)
class DisposalDecision:
    """The fail-closed disposal verdict for one record; never a deletion."""

    allowed: bool
    reason_code: str
    eligible_at: datetime | None


@dataclass(frozen=True, kw_only=True)
class ReleasedRecord:
    """One released version's content and provenance for the export package."""

    release_id: UUID
    version_id: UUID
    document_id: UUID
    encounter_id: UUID
    version: int
    state: str
    content_digest: str
    finalized_at: datetime
    amendment_of_version: int | None
    author_label: str
    subjective: str
    objective: str
    assessment: str
    plan: str
    released_at: datetime


@dataclass(frozen=True, kw_only=True)
class ExportPackage:
    """A fully materialized export plus its stored receipt."""

    export: RecordExport
    data: bytes
    file_name: str


@dataclass(frozen=True, kw_only=True)
class ExportVerification:
    """The structural and digest verdict for one export package."""

    ok: bool
    reason_code: str
    manifest_digest: str


def apply_retention_policy() -> NoReturn:
    """Apply a retention policy when the retention domain is implemented."""
    message = "Phase >=1"
    raise NotImplementedError(message)


def _denied(
    clinic_id: UUID,
    record_id: UUID,
    reason: str,
    *,
    record_type: str = "retention.record",
) -> NoReturn:
    """Append the fixed metadata-only denial event, then fail closed."""
    record_event(
        AuditEventInput(
            event_type="retention.access.denied",
            component_id="clinic-os-web",
            component_ip=None,
            affected_record_type=record_type,
            affected_record_id=str(record_id),
            occurred_at_utc=timezone.now(),
        ),
        payload={"clinic_id": str(clinic_id), "reason_code": reason},
    )
    raise RetentionAccessDeniedError


def _record_retention_event(
    event_type: str,
    *,
    clinic_id: UUID,
    record_type: str,
    record_id: UUID,
    verb: str,
) -> int:
    """Append one fixed retention event with metadata-only payload."""
    return record_event(
        AuditEventInput(
            event_type=event_type,
            component_id="clinic-os-web",
            component_ip=None,
            affected_record_type=record_type,
            affected_record_id=str(record_id),
            occurred_at_utc=timezone.now(),
        ),
        payload={"clinic_id": str(clinic_id), "object_verb": verb},
    )


def _clinic(clinic_id: UUID) -> Clinic:
    clinic = Clinic.objects.filter(pk=clinic_id).first()
    if clinic is None:
        raise RetentionAccessDeniedError
    return clinic


def _record_scope(record_class: str, record_id: UUID) -> tuple[UUID, UUID, datetime]:
    """Resolve the owning clinic, organization and creation time, or deny."""
    if record_class not in RECORD_CLASSES:
        raise RetentionAccessDeniedError
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT * FROM clinic_app.retention_record_scope(%s, %s)",
            [record_class, str(record_id)],
        )
        row = cursor.fetchone()
    if row is None or not isinstance(row[2], datetime):
        raise RetentionAccessDeniedError
    return row[0], row[1], row[2]


def _require_manager(clinic_id: UUID, record_id: UUID) -> UUID:
    try:
        return require_current_actor_clinic_roles(clinic_id, POLICY_ROLES)
    except CurrentActorError:
        _denied(clinic_id, record_id, "role_denied")


def propose_policy(
    *, clinic_id: UUID, record_class: str, retention_days: int | None
) -> RetentionPolicy:
    """Propose the next policy version; nothing is approved implicitly."""
    clinic = _clinic(clinic_id)
    actor = _require_manager(clinic_id, clinic_id)
    if record_class not in RECORD_CLASSES or (
        retention_days is not None
        and (type(retention_days) is not int or retention_days < 0)
    ):
        msg = "Classe de registro ou prazo de retenção inválido."
        raise ValidationError(msg)
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            [f"retention-policy:{clinic_id}:{record_class}"],
        )
        previous = (
            RetentionPolicy.objects.filter(
                clinic_id=clinic_id, record_class=record_class
            )
            .order_by("-version")
            .first()
        )
        policy = RetentionPolicy.objects.create(
            organization_id=clinic.organization_id,
            clinic_id=clinic_id,
            record_class=record_class,
            version=previous.version + 1 if previous else 1,
            retention_days=retention_days,
            proposed_by_id=actor,
        )
        _record_retention_event(
            "retention.policy.proposed",
            clinic_id=clinic_id,
            record_type="retention.retention_policy",
            record_id=policy.pk,
            verb="proposed",
        )
        return policy


def approve_policy(*, clinic_id: UUID, policy_id: UUID) -> RetentionPolicy:
    """Approve one proposed version, retiring the previously approved row."""
    _require_manager(clinic_id, policy_id)
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            [f"retention-policy-approve:{clinic_id}"],
        )
        policy = (
            RetentionPolicy.objects.select_for_update()
            .filter(pk=policy_id, clinic_id=clinic_id)
            .first()
        )
        if policy is None:
            _denied(clinic_id, policy_id, "not_found")
        if policy.state != RetentionPolicy.State.PROPOSED:
            msg = "precondition_failed"
            raise RetentionConflictError(msg)
        previous = (
            RetentionPolicy.objects.select_for_update()
            .filter(
                clinic_id=clinic_id,
                record_class=policy.record_class,
                state=RetentionPolicy.State.APPROVED,
            )
            .first()
        )
        actor = current_actor_id()
        now = timezone.now()
        if previous is not None:
            previous.state = RetentionPolicy.State.RETIRED
            previous.retired_by_id = actor
            previous.retired_at = now
            previous.save(update_fields=("state", "retired_by", "retired_at"))
            _record_retention_event(
                "retention.policy.retired",
                clinic_id=clinic_id,
                record_type="retention.retention_policy",
                record_id=previous.pk,
                verb="retired",
            )
        policy.state = RetentionPolicy.State.APPROVED
        policy.approved_by_id = actor
        policy.approved_at = now
        policy.save(update_fields=("state", "approved_by", "approved_at"))
        _record_retention_event(
            "retention.policy.approved",
            clinic_id=clinic_id,
            record_type="retention.retention_policy",
            record_id=policy.pk,
            verb="approved",
        )
        return policy


def retire_policy(*, clinic_id: UUID, policy_id: UUID) -> RetentionPolicy:
    """Retire one proposed or approved policy; disposal then fails closed."""
    _require_manager(clinic_id, policy_id)
    with transaction.atomic():
        policy = (
            RetentionPolicy.objects.select_for_update()
            .filter(pk=policy_id, clinic_id=clinic_id)
            .first()
        )
        if policy is None:
            _denied(clinic_id, policy_id, "not_found")
        if policy.state == RetentionPolicy.State.RETIRED:
            return policy
        policy.state = RetentionPolicy.State.RETIRED
        policy.retired_by_id = current_actor_id()
        policy.retired_at = timezone.now()
        policy.save(update_fields=("state", "retired_by", "retired_at"))
        _record_retention_event(
            "retention.policy.retired",
            clinic_id=clinic_id,
            record_type="retention.retention_policy",
            record_id=policy.pk,
            verb="retired",
        )
        return policy


def place_hold(
    *, clinic_id: UUID, record_class: str, record_id: UUID, authority: str, reason: str
) -> LegalHold:
    """Place one authority-bound hold; an identical active hold is returned."""
    _require_manager(clinic_id, record_id)
    authority = authority.strip()
    reason = reason.strip()
    if (
        not authority
        or not reason
        or len(authority) > MAX_TEXT
        or len(reason) > MAX_TEXT
    ):
        msg = "Informe a autoridade e o motivo da guarda legal."
        raise ValidationError(msg)
    scope_clinic, organization_id, _ = _record_scope(record_class, record_id)
    if scope_clinic != clinic_id:
        raise RetentionAccessDeniedError
    existing = LegalHold.objects.filter(
        clinic_id=clinic_id,
        record_class=record_class,
        record_id=record_id,
        authority=authority,
        reason=reason,
        released_at__isnull=True,
    ).first()
    if existing is not None:
        return existing
    with transaction.atomic():
        hold = LegalHold.objects.create(
            organization_id=organization_id,
            clinic_id=clinic_id,
            record_class=record_class,
            record_id=record_id,
            authority=authority,
            reason=reason,
            placed_by_id=current_actor_id(),
        )
        _record_retention_event(
            "retention.hold.placed",
            clinic_id=clinic_id,
            record_type="retention.legal_hold",
            record_id=hold.pk,
            verb="placed",
        )
        return hold


def release_hold(
    *, clinic_id: UUID, hold_id: UUID, authority: str, reason: str
) -> LegalHold:
    """Record the release authority and reason; an active hold ends exactly once."""
    _require_manager(clinic_id, hold_id)
    authority = authority.strip()
    reason = reason.strip()
    if (
        not authority
        or not reason
        or len(authority) > MAX_TEXT
        or len(reason) > MAX_TEXT
    ):
        msg = "Informe a autoridade e o motivo da liberação da guarda."
        raise ValidationError(msg)
    with transaction.atomic():
        hold = (
            LegalHold.objects.select_for_update()
            .filter(pk=hold_id, clinic_id=clinic_id)
            .first()
        )
        if hold is None:
            _denied(clinic_id, hold_id, "not_found")
        if hold.released_at is not None:
            return hold
        hold.released_by_id = current_actor_id()
        hold.released_at = timezone.now()
        hold.release_authority = authority
        hold.release_reason = reason
        hold.save(
            update_fields=(
                "released_by",
                "released_at",
                "release_authority",
                "release_reason",
            )
        )
        _record_retention_event(
            "retention.hold.released",
            clinic_id=clinic_id,
            record_type="retention.legal_hold",
            record_id=hold.pk,
            verb="released",
        )
        return hold


def record_disposition(
    *, clinic_id: UUID, record_class: str, record_id: UUID
) -> DisposalDecision:
    """Evaluate disposal eligibility; the answer is never destructive."""
    _require_manager(clinic_id, record_id)
    scope_clinic, _, created_at = _record_scope(record_class, record_id)
    if scope_clinic != clinic_id:
        raise RetentionAccessDeniedError
    if LegalHold.objects.filter(
        clinic_id=clinic_id,
        record_class=record_class,
        record_id=record_id,
        released_at__isnull=True,
    ).exists():
        return DisposalDecision(allowed=False, reason_code="held", eligible_at=None)
    policy = (
        RetentionPolicy.objects.filter(
            clinic_id=clinic_id,
            record_class=record_class,
            state=RetentionPolicy.State.APPROVED,
        )
        .order_by("-version")
        .first()
    )
    if policy is None:
        return DisposalDecision(
            allowed=False, reason_code="no_approved_policy", eligible_at=None
        )
    if policy.retention_days is None:
        return DisposalDecision(
            allowed=False, reason_code="indefinite_retention", eligible_at=None
        )
    eligible_at = created_at + timedelta(days=policy.retention_days)
    if eligible_at > timezone.now():
        return DisposalDecision(
            allowed=False,
            reason_code="retention_not_elapsed",
            eligible_at=eligible_at,
        )
    return DisposalDecision(
        allowed=True, reason_code="eligible", eligible_at=eligible_at
    )


def request_disposal(
    *, clinic_id: UUID, record_class: str, record_id: UUID
) -> DisposalDecision:
    """Evaluate one explicit disposal request; denials are audited, data kept.

    Even an ``eligible`` verdict performs no deletion: the runtime role holds
    no DELETE grant and the immutable triggers reject removal, so disposal
    stays a decision record until an approved pipeline exists.
    """
    decision = record_disposition(
        clinic_id=clinic_id, record_class=record_class, record_id=record_id
    )
    if decision.allowed:
        _record_retention_event(
            "retention.disposal.evaluated",
            clinic_id=clinic_id,
            record_type=record_class,
            record_id=record_id,
            verb="evaluated",
        )
        return decision
    _denied(
        clinic_id,
        record_id,
        decision.reason_code,
        record_type=record_class,
    )


def release_version(*, clinic_id: UUID, version_id: UUID) -> RecordRelease:
    """Release one exact finalized or superseded version to its patient."""
    version = (
        ClinicalDocumentVersion.objects.filter(
            pk=version_id, document__encounter__clinic_id=clinic_id
        )
        .select_related("document__encounter")
        .first()
    )
    if version is None:
        raise ClinicalAccessDeniedError
    encounter = version.document.encounter
    actor = _assigned(_appointment(clinic_id, encounter.appointment_id))
    if version.state not in ("finalized", "superseded"):
        msg = "precondition_failed"
        raise ClinicalConflictError(msg)
    existing = RecordRelease.objects.filter(
        version_id=version.pk, revoked_at__isnull=True
    ).first()
    if existing is not None:
        return existing
    with transaction.atomic():
        try:
            with transaction.atomic():
                release = RecordRelease.objects.create(
                    organization_id=encounter.organization_id,
                    clinic_id=clinic_id,
                    patient_id=encounter.patient_id,
                    version_id=version.pk,
                    released_by_id=actor,
                )
        except IntegrityError:
            # A concurrent release won the partial unique index; return it.
            return RecordRelease.objects.get(
                version_id=version.pk, revoked_at__isnull=True
            )
        _record_retention_event(
            "ehr.document.released",
            clinic_id=clinic_id,
            record_type="retention.record_release",
            record_id=release.pk,
            verb="released",
        )
        return release


def revoke_release(*, clinic_id: UUID, release_id: UUID) -> RecordRelease:
    """Revoke one release; unknown ids are non-enumerating, repeats are no-ops."""
    release = RecordRelease.objects.filter(pk=release_id, clinic_id=clinic_id).first()
    if release is None:
        # The clinical contract's denial vocabulary applies to releases.
        record_denial(clinic_id, release_id, "not_found")
        raise RetentionAccessDeniedError
    version = (
        ClinicalDocumentVersion.objects.filter(pk=release.version_id)
        .select_related("document__encounter")
        .first()
    )
    if version is None:
        raise ClinicalAccessDeniedError
    _assigned(_appointment(clinic_id, version.document.encounter.appointment_id))
    with transaction.atomic():
        locked = (
            RecordRelease.objects.select_for_update()
            .filter(pk=release.pk, revoked_at__isnull=True)
            .first()
        )
        if locked is None:
            return RecordRelease.objects.get(pk=release.pk)
        locked.revoked_by_id = current_actor_id()
        locked.revoked_at = timezone.now()
        locked.save(update_fields=("revoked_by", "revoked_at"))
        _record_retention_event(
            "ehr.document.release_revoked",
            clinic_id=clinic_id,
            record_type="retention.record_release",
            record_id=locked.pk,
            verb="release_revoked",
        )
        return locked


def _author_label(user_id: UUID) -> str:
    """Resolve one author's display label through the trusted resolver."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT clinic_app.retention_author_label(%s)", [str(user_id)])
        row = cursor.fetchone()
    if row is None or row[0] is None:
        raise RetentionAccessDeniedError
    return str(row[0])


def _released_for_patient_staff(
    clinic_id: UUID, patient_id: UUID
) -> list[ReleasedRecord]:
    """Collect released versions through the runtime role's own read policy.

    Every version whose clinical content is collected appends the contract's
    metadata-only ``ehr.record.viewed`` event under the actual staff actor,
    exactly like the patient read path.
    """
    releases = list(
        RecordRelease.objects.filter(
            clinic_id=clinic_id, patient_id=patient_id, revoked_at__isnull=True
        ).order_by("created_at", "pk")
    )
    versions = {
        row.pk: row
        for row in ClinicalDocumentVersion.objects.filter(
            pk__in=[release.version_id for release in releases]
        ).select_related("document__encounter", "amendment_of")
    }
    records: list[ReleasedRecord] = []
    for release in releases:
        version = versions.get(release.version_id)
        if version is None:
            # A release whose version is not readable never enters a package.
            continue
        if version.finalized_at is None:
            continue
        base = version.amendment_of
        records.append(
            ReleasedRecord(
                release_id=release.pk,
                version_id=version.pk,
                document_id=version.document_id,
                encounter_id=version.document.encounter_id,
                version=version.version,
                state=version.state,
                content_digest=version.content_digest,
                finalized_at=version.finalized_at,
                amendment_of_version=base.version if base is not None else None,
                author_label=_author_label(version.author_id),
                subjective=version.subjective,
                objective=version.objective,
                assessment=version.assessment,
                plan=version.plan,
                released_at=release.created_at,
            )
        )
    for record in records:
        record_phase1_event(
            "ehr.record.viewed",
            clinic_id=clinic_id,
            affected_record_id=record.version_id,
        )
    return records


def _session_scope() -> tuple[UUID, UUID, UUID, UUID]:
    """Return the live records session binding or deny the patient boundary."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT * FROM clinic_app.retention_records_session()")
        row = cursor.fetchone()
    if row is None:
        raise RetentionAccessDeniedError
    return row[0], row[1], row[2], row[3]


def _record_patient_view(
    *, session_id: UUID, organization_id: UUID, clinic_id: UUID, version_id: UUID
) -> None:
    """Append the contract's clinical read event for one released version.

    Patient sessions carry no staff actor or tenant GUC, so the append goes
    through the session-authorized resolver: the database re-validates the
    live session and the release binding, stores the session id as the
    actor and chains the event. The payload stays metadata-only.
    """
    event = AuditEventInput(
        event_type="ehr.record.viewed",
        component_id="clinic-os-web",
        component_ip=None,
        affected_record_type="ehr.document_version",
        affected_record_id=str(version_id),
        occurred_at_utc=timezone.now(),
    )
    payload = _normalize_payload({"clinic_id": str(clinic_id), "object_verb": "viewed"})
    content_hash = _content_hash(
        event,
        AuditTrustedContext(organization_id=organization_id, actor_user_id=session_id),
        payload,
    )
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.retention_record_viewed("
            "%s::uuid, %s::timestamptz, %s::bytea)",
            [str(version_id), event.occurred_at_utc, content_hash],
        )
        appended = cursor.fetchone()
    if appended is None:
        raise RetentionAccessDeniedError


def patient_released_records() -> list[ReleasedRecord]:
    """List the session patient's released versions through the resolver."""
    session_id, organization_id, clinic_id, _ = _session_scope()
    with connection.cursor() as cursor:
        cursor.execute("SELECT * FROM clinic_app.retention_patient_releases()")
        rows = cursor.fetchall()
    records = []
    for row in rows:
        # The resolver returns the tenant envelope; the live patient session
        # authorizes decryption through the protected boundary.
        content = json.loads(
            reveal(
                purpose="ehr.clinicaldocumentversion.content",
                envelope=bytes(row[10]),
            ).decode("utf-8")
        )
        records.append(
            ReleasedRecord(
                release_id=row[0],
                version_id=row[1],
                document_id=row[2],
                encounter_id=row[3],
                version=row[4],
                state=row[5],
                content_digest=row[6],
                finalized_at=row[7],
                amendment_of_version=row[8],
                author_label=row[9],
                subjective=str(content.get("subjective", "")),
                objective=str(content.get("objective", "")),
                assessment=str(content.get("assessment", "")),
                plan=str(content.get("plan", "")),
                released_at=row[11],
            )
        )
    for record in records:
        _record_patient_view(
            session_id=session_id,
            organization_id=organization_id,
            clinic_id=clinic_id,
            version_id=record.version_id,
        )
    return records


def _record_document(record: ReleasedRecord) -> dict[str, JsonValue]:
    """Serialize one released version exactly; digests cover these bytes."""
    return {
        "record_class": "ehr.document_version",
        "version_id": str(record.version_id),
        "document_id": str(record.document_id),
        "encounter_id": str(record.encounter_id),
        "version": record.version,
        "state": record.state,
        "content_digest": record.content_digest,
        "finalized_at": record.finalized_at.isoformat(),
        "amendment_of_version": record.amendment_of_version,
        "author_label": record.author_label,
        "released_at": record.released_at.isoformat(),
        "release_id": str(record.release_id),
        "content": {
            "subjective": record.subjective,
            "objective": record.objective,
            "assessment": record.assessment,
            "plan": record.plan,
        },
    }


def _build_package(
    *,
    export_id: UUID,
    clinic_id: UUID,
    patient_id: UUID,
    kind: str,
    records: list[ReleasedRecord],
) -> tuple[bytes, dict[str, JsonValue], str]:
    """Build the deterministic zip and its manifest/digest in memory."""
    files: list[tuple[str, bytes]] = []
    entries: list[dict[str, JsonValue]] = []
    for record in records:
        name = f"records/{record.version_id}.json"
        data = rfc8785.dumps(_record_document(record))
        files.append((name, data))
        entries.append(
            {
                "path": name,
                "sha256": sha256(data).hexdigest(),
                "bytes": len(data),
                "record_class": "ehr.document_version",
                "record_id": str(record.version_id),
                "version": record.version,
                "state": record.state,
                "content_digest": record.content_digest,
                "release_id": str(record.release_id),
            }
        )
    manifest: dict[str, JsonValue] = {
        "v": "clinic-record-export-v1",
        "export_id": str(export_id),
        "kind": kind,
        "clinic_id": str(clinic_id),
        "patient_id": str(patient_id),
        "created_at": timezone.now().isoformat(),
        "record_count": len(records),
        "files": entries,
        "excluded": [
            "draft_or_unreleased_versions",
            "discarded_content",
            "attachments",
            "staff_secrets",
            "audit_internals",
            "other_clinic_data",
        ],
    }
    manifest["manifest_sha256"] = sha256(rfc8785.dumps(manifest)).hexdigest()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in [(MANIFEST_NAME, rfc8785.dumps(manifest)), *files]:
            info = zipfile.ZipInfo(name, date_time=ZIP_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            archive.writestr(info, data)
    return buffer.getvalue(), manifest, str(manifest["manifest_sha256"])


def export_staff_records(*, clinic_id: UUID, patient_id: UUID) -> ExportPackage:
    """Export one patient's released records for a care-bound physician."""
    try:
        actor = require_current_actor_clinic_roles(
            clinic_id, (UserClinicRole.Role.PHYSICIAN,)
        )
    except CurrentActorError:
        _denied(clinic_id, patient_id, "role_denied", record_type="intake.patient")
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.retention_care(%s, %s)",
            [str(clinic_id), str(patient_id)],
        )
        care = cursor.fetchone()
    if not care or not care[0]:
        _denied(
            clinic_id,
            patient_id,
            "no_care_relationship",
            record_type="intake.patient",
        )
    clinic = _clinic(clinic_id)
    records = _released_for_patient_staff(clinic_id, patient_id)
    if len(records) > MAX_EXPORT_RECORDS:
        msg = "export_too_large"
        raise RetentionConflictError(msg)
    export_id = uuid4()
    data, manifest, digest = _build_package(
        export_id=export_id,
        clinic_id=clinic_id,
        patient_id=patient_id,
        kind="staff",
        records=records,
    )
    with transaction.atomic():
        export = RecordExport.objects.create(
            id=export_id,
            organization_id=clinic.organization_id,
            clinic_id=clinic_id,
            patient_id=patient_id,
            kind=RecordExport.Kind.STAFF,
            requested_by_id=actor,
            record_count=len(records),
            manifest=manifest,
            manifest_digest=digest,
        )
        _record_retention_event(
            "retention.export.created",
            clinic_id=clinic_id,
            record_type="retention.record_export",
            record_id=export.pk,
            verb="created",
        )
    return ExportPackage(
        export=export,
        data=data,
        file_name=f"registros-{export.pk}.zip",
    )


def export_patient_records() -> ExportPackage:
    """Export the session patient's released records as a database receipt."""
    session_id, organization_id, clinic_id, patient_id = _session_scope()
    records = patient_released_records()
    if len(records) > MAX_EXPORT_RECORDS:
        msg = "export_too_large"
        raise RetentionConflictError(msg)
    export_id = uuid4()
    data, manifest, digest = _build_package(
        export_id=export_id,
        clinic_id=clinic_id,
        patient_id=patient_id,
        kind="patient",
        records=records,
    )
    with transaction.atomic():
        export = RecordExport.objects.create(
            id=export_id,
            organization_id=organization_id,
            clinic_id=clinic_id,
            patient_id=patient_id,
            kind=RecordExport.Kind.PATIENT,
            patient_session_id=session_id,
            record_count=len(records),
            manifest=manifest,
            manifest_digest=digest,
        )
    return ExportPackage(
        export=export,
        data=data,
        file_name=f"meus-registros-{export.pk}.zip",
    )


class _PackageInvalidError(Exception):
    """Carry the fixed rejection reason for one malformed package."""

    def __init__(self, reason_code: str) -> None:
        """Retain the machine-consumed rejection reason."""
        self.reason_code = reason_code
        super().__init__(reason_code)


def _package_parts(
    data: bytes,
) -> tuple[Mapping[str, JsonValue], dict[str, str], dict[str, str]]:
    """Read the manifest, its declared digests and the actual file digests."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = archive.namelist()
            has_manifest = names.count(MANIFEST_NAME) == 1
            manifest: object = (
                json.loads(archive.read(MANIFEST_NAME)) if has_manifest else None
            )
            actual = {
                name: sha256(archive.read(name)).hexdigest()
                for name in names
                if name != MANIFEST_NAME
            }
    except (AttributeError, KeyError, TypeError, ValueError, zipfile.BadZipFile) as e:
        msg = "malformed_package"
        raise _PackageInvalidError(msg) from e
    if not has_manifest or not isinstance(manifest, dict):
        msg = "manifest_missing" if not has_manifest else "manifest_malformed"
        raise _PackageInvalidError(msg)
    files = manifest.get("files")
    if not isinstance(files, list):
        msg = "manifest_malformed"
        raise _PackageInvalidError(msg)
    expected: dict[str, str] = {}
    for entry in files:
        path = entry.get("path") if isinstance(entry, dict) else None
        digest = entry.get("sha256") if isinstance(entry, dict) else None
        if not isinstance(path, str) or not isinstance(digest, str):
            msg = "manifest_malformed"
            raise _PackageInvalidError(msg)
        expected[path] = digest
    return cast("Mapping[str, JsonValue]", manifest), expected, actual


def verify_export_package(data: bytes) -> ExportVerification:
    """Verify one package's manifest and file digests without any database."""
    try:
        manifest, expected, actual = _package_parts(data)
    except _PackageInvalidError as error:
        return ExportVerification(
            ok=False, reason_code=error.reason_code, manifest_digest=""
        )
    if set(actual) != set(expected):
        return ExportVerification(
            ok=False, reason_code="file_set_mismatch", manifest_digest=""
        )
    if actual != expected:
        return ExportVerification(
            ok=False, reason_code="digest_mismatch", manifest_digest=""
        )
    claimed = manifest.get("manifest_sha256")
    if not isinstance(claimed, str):
        return ExportVerification(
            ok=False, reason_code="manifest_malformed", manifest_digest=""
        )
    recomputed = sha256(
        rfc8785.dumps({k: v for k, v in manifest.items() if k != "manifest_sha256"})
    ).hexdigest()
    if recomputed != claimed:
        return ExportVerification(
            ok=False, reason_code="manifest_digest_mismatch", manifest_digest=claimed
        )
    return ExportVerification(ok=True, reason_code="verified", manifest_digest=claimed)


def verify_stored_export(
    *, clinic_id: UUID, export_id: UUID, data: bytes
) -> ExportVerification:
    """Verify one package against its stored receipt and manifest digest.

    Managers may verify any export; a physician may verify exports for a
    patient they hold a care relationship with. Every other actor is denied.
    """
    export = RecordExport.objects.filter(pk=export_id, clinic_id=clinic_id).first()
    if export is None:
        _denied(
            clinic_id, export_id, "not_found", record_type="retention.record_export"
        )
    try:
        require_current_actor_clinic_roles(clinic_id, POLICY_ROLES)
    except CurrentActorError:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT clinic_app.retention_care(%s, %s)",
                [str(clinic_id), str(export.patient_id)],
            )
            care = cursor.fetchone()
        if not care or not care[0]:
            _denied(
                clinic_id,
                export_id,
                "role_denied",
                record_type="retention.record_export",
            )
    verification = verify_export_package(data)
    if not verification.ok:
        return verification
    if verification.manifest_digest != export.manifest_digest:
        return ExportVerification(
            ok=False,
            reason_code="manifest_digest_mismatch",
            manifest_digest=verification.manifest_digest,
        )
    return verification

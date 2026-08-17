"""Transaction-bound practitioner availability creation service."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC
from typing import TYPE_CHECKING

import psycopg
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.audit.services import record_phase1_event
from apps.core.idempotency import create_fingerprint
from apps.identity.current_context import list_active_clinic_physicians
from apps.scheduling.access import authorized_manager_clinic
from apps.scheduling.locks import (
    acquire_advisory_locks,
    clinic_lock_key,
    user_lock_keys,
)
from apps.scheduling.models import AvailabilityBlock
from apps.scheduling.timezones import LOCAL_MINUTE_PATTERN, parse_local_minute

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

    from apps.identity.models import Clinic


class AvailabilityCreateInputError(ValueError):
    """Reject malformed availability input without reflecting it."""

    def __init__(self) -> None:
        """Expose one stable non-identifying validation message."""
        super().__init__("availability create input is invalid")


class AvailabilityPractitionerError(Exception):
    """Reject a target without an active exact physician assignment."""

    def __init__(self) -> None:
        """Expose one stable non-identifying target message."""
        super().__init__("availability practitioner unavailable")


class AvailabilityIdempotencyConflictError(Exception):
    """Reject reuse of an availability-create key for different input."""

    def __init__(self) -> None:
        """Expose one stable non-identifying conflict message."""
        super().__init__("availability idempotency conflict")


class AvailabilityOverlapError(Exception):
    """Reject an active interval that overlaps a practitioner promise."""

    def __init__(self) -> None:
        """Expose one stable non-identifying overlap message."""
        super().__init__("availability overlaps an active block")


@dataclass(frozen=True, slots=True)
class _CreateRequest:
    clinic_id: UUID
    practitioner_id: UUID
    start_local: str
    end_local: str
    idempotency_key: UUID


@dataclass(frozen=True, slots=True)
class _PreparedAvailability:
    start_at: datetime
    end_at: datetime
    fingerprint: bytes


def _utc_minute(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:00Z")


def _parse_interval(
    start_local: str,
    end_local: str,
    timezone_key: str,
) -> tuple[datetime, datetime]:
    try:
        start_at = parse_local_minute(start_local, timezone_key)
        end_at = parse_local_minute(end_local, timezone_key)
    except ValueError as error:
        raise AvailabilityCreateInputError from error
    return start_at, end_at


def _fingerprint(
    clinic_id: UUID,
    practitioner_id: UUID,
    start_at: datetime,
    end_at: datetime,
) -> bytes:
    return create_fingerprint(
        "availability",
        {
            "clinic_id": str(clinic_id),
            "end_utc": _utc_minute(end_at),
            "practitioner_id": str(practitioner_id),
            "start_utc": _utc_minute(start_at),
        },
    )


def _replay_for_key(
    organization_id: UUID,
    idempotency_key: UUID,
    fingerprint: bytes,
) -> AvailabilityBlock | None:
    block = AvailabilityBlock.objects.filter(
        organization_id=organization_id,
        idempotency_key=idempotency_key,
    ).first()
    if block is None:
        return None
    if bytes(block.create_fingerprint) != fingerprint:
        raise AvailabilityIdempotencyConflictError
    return block


def _constraint_name(error: IntegrityError) -> str | None:
    cause = error.__cause__
    if not isinstance(cause, psycopg.Error):
        return None
    return cause.diag.constraint_name


def _timezone_key(clinic: Clinic) -> str:
    timezone_key = clinic.timezone
    if not isinstance(timezone_key, str):
        raise AvailabilityCreateInputError
    return timezone_key


def _validate_syntax(start_local: str, end_local: str) -> None:
    if (
        not isinstance(start_local, str)
        or LOCAL_MINUTE_PATTERN.fullmatch(start_local) is None
        or not isinstance(end_local, str)
        or LOCAL_MINUTE_PATTERN.fullmatch(end_local) is None
    ):
        raise AvailabilityCreateInputError


def _existing_replay(
    organization_id: UUID,
    request: _CreateRequest,
    timezone_key: str,
) -> AvailabilityBlock | None:
    existing = AvailabilityBlock.objects.filter(
        organization_id=organization_id,
        idempotency_key=request.idempotency_key,
    ).first()
    if existing is None:
        return None
    start_at, end_at = _parse_interval(
        request.start_local,
        request.end_local,
        timezone_key,
    )
    fingerprint = _fingerprint(
        request.clinic_id,
        request.practitioner_id,
        start_at,
        end_at,
    )
    if bytes(existing.create_fingerprint) != fingerprint:
        raise AvailabilityIdempotencyConflictError
    return existing


def _validate_new_interval(
    start_local: str,
    end_local: str,
    start_at: datetime,
    end_at: datetime,
) -> None:
    if (
        start_local[:10] != end_local[:10]
        or end_at <= start_at
        or start_at <= timezone.now()
    ):
        raise AvailabilityCreateInputError


def _require_active_physician(clinic_id: UUID, practitioner_id: UUID) -> None:
    acquire_advisory_locks(user_lock_keys((practitioner_id,)))
    physicians = list_active_clinic_physicians(clinic_id)
    if practitioner_id not in {entry.user_id for entry in physicians}:
        raise AvailabilityPractitionerError


def _insert_or_replay(
    clinic: Clinic,
    request: _CreateRequest,
    prepared: _PreparedAvailability,
) -> tuple[AvailabilityBlock, bool]:
    try:
        with transaction.atomic():
            block = AvailabilityBlock.objects.create(
                organization_id=clinic.organization_id,
                clinic=clinic,
                practitioner_id=request.practitioner_id,
                start_at=prepared.start_at,
                end_at=prepared.end_at,
                idempotency_key=request.idempotency_key,
                create_fingerprint=prepared.fingerprint,
            )
    except IntegrityError as error:
        replay = _replay_for_key(
            clinic.organization_id,
            request.idempotency_key,
            prepared.fingerprint,
        )
        if replay is not None:
            return replay, False
        if (
            _constraint_name(error)
            == "scheduling_availability_active_practitioner_excl"
        ):
            raise AvailabilityOverlapError from error
        raise
    return block, True


def create_availability(
    *,
    clinic_id: UUID,
    practitioner_id: UUID,
    start_local: str,
    end_local: str,
    idempotency_key: UUID,
) -> AvailabilityBlock:
    """Create one future clinic-local availability interval."""
    _validate_syntax(start_local, end_local)
    request = _CreateRequest(
        clinic_id=clinic_id,
        practitioner_id=practitioner_id,
        start_local=start_local,
        end_local=end_local,
        idempotency_key=idempotency_key,
    )
    with transaction.atomic():
        clinic = authorized_manager_clinic(clinic_id)
        replay = _existing_replay(
            clinic.organization_id,
            request,
            _timezone_key(clinic),
        )
        if replay is not None:
            return replay
        acquire_advisory_locks((clinic_lock_key(clinic_id),))
        clinic = authorized_manager_clinic(clinic_id)
        start_at, end_at = _parse_interval(
            start_local,
            end_local,
            _timezone_key(clinic),
        )
        fingerprint = _fingerprint(clinic_id, practitioner_id, start_at, end_at)
        replay = _replay_for_key(
            clinic.organization_id,
            idempotency_key,
            fingerprint,
        )
        if replay is not None:
            return replay
        _validate_new_interval(start_local, end_local, start_at, end_at)
        _require_active_physician(clinic_id, practitioner_id)
        prepared = _PreparedAvailability(
            start_at=start_at,
            end_at=end_at,
            fingerprint=fingerprint,
        )
        block, created = _insert_or_replay(
            clinic,
            request,
            prepared,
        )
        if not created:
            return block
        record_phase1_event(
            "scheduling.availability.created",
            clinic_id=clinic_id,
            affected_record_id=block.pk,
        )
        return block

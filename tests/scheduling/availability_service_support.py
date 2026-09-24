from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from apps.core.idempotency import create_fingerprint
from apps.scheduling.services import create_availability

if TYPE_CHECKING:
    from datetime import datetime

    from apps.scheduling.models import AvailabilityBlock


@dataclass(frozen=True, slots=True)
class ExpectedBlock:
    organization_id: UUID
    clinic_id: UUID
    practitioner_id: UUID
    idempotency_key: UUID
    expected_start: datetime
    expected_end: datetime
    start_utc: str
    end_utc: str


def create_synthetic_block(
    clinic_id: UUID,
    practitioner_id: UUID,
    interval: tuple[str, str, str],
    idempotency_key: UUID | None = None,
) -> AvailabilityBlock:
    day, start, end = interval
    return create_availability(
        clinic_id=clinic_id,
        practitioner_id=practitioner_id,
        start_local=f"{day}T{start}",
        end_local=f"{day}T{end}",
        idempotency_key=idempotency_key or uuid4(),
    )


def assert_created_block(
    block: AvailabilityBlock,
    expected: ExpectedBlock,
) -> None:
    assert block.organization_id == expected.organization_id
    assert block.clinic_id == expected.clinic_id
    assert block.practitioner_id == expected.practitioner_id
    assert block.start_at == expected.expected_start
    assert block.end_at == expected.expected_end
    assert block.idempotency_key == expected.idempotency_key
    assert bytes(block.create_fingerprint) == create_fingerprint(
        "availability",
        {
            "clinic_id": str(expected.clinic_id),
            "end_utc": expected.end_utc,
            "practitioner_id": str(expected.practitioner_id),
            "start_utc": expected.start_utc,
        },
    )
    assert block.retired_at is None

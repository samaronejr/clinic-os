"""Equipment identity and additive calibration/maintenance evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING

from django.core.exceptions import ValidationError

from apps.inventory.access import INVALID, authority, execute, label, record, today
from apps.inventory.models import Calibration, Equipment, Maintenance

if TYPE_CHECKING:
    from uuid import UUID


@dataclass(frozen=True, slots=True)
class EquipmentStatus:
    """Deterministic status retains the exact evidence rows used in the decision."""

    calibration_id: UUID | None
    maintenance_id: UUID | None
    calibration_overdue: bool
    maintenance_overdue: bool
    usable: bool


def register_equipment(  # noqa: PLR0913 - pinned clinic-scoped service contract
    *,
    clinic_id: UUID,
    supplier_id: UUID,
    location_id: UUID,
    name: str,
    serial: str,
    command_id: UUID,
) -> Equipment:
    """Record a physical asset without assuming current calibration."""
    authority(clinic_id=clinic_id, permission="inventory.manage")
    label(name, maximum=160)
    label(serial, maximum=120)
    return execute(
        Equipment,
        clinic_id=clinic_id,
        command_id=command_id,
        operation="register_equipment",
        permission="inventory.manage",
        payload={
            "name": name,
            "serial": serial,
            "supplier": str(supplier_id),
            "location": str(location_id),
        },
        create=lambda _actor, tenant: Equipment.objects.create(
            organization_id=tenant,
            clinic_id=clinic_id,
            supplier_id=supplier_id,
            location_id=location_id,
            name=name,
            serial=serial,
            command_id=command_id,
        ),
    )


def evidence[T: (Calibration, Maintenance)](  # noqa: PLR0913
    model: type[T],
    *,
    clinic_id: UUID,
    equipment_id: UUID,
    performed_on: date,
    due_on: date,
    outcome: str,
    command_id: UUID,
    operation: str,
) -> T:
    """Validate dates at the boundary, then retain one immutable evidence row."""
    authority(clinic_id=clinic_id, permission="inventory.manage")
    if type(performed_on) is not date or type(due_on) is not date:
        raise ValidationError(INVALID)
    return execute(
        model,
        clinic_id=clinic_id,
        command_id=command_id,
        operation=operation,
        permission="inventory.manage",
        payload={
            "equipment": str(equipment_id),
            "performed_on": performed_on.isoformat(),
            "due_on": due_on.isoformat(),
            "outcome": outcome,
        },
        create=lambda _actor, tenant: model.objects.create(
            organization_id=tenant,
            clinic_id=clinic_id,
            equipment_id=equipment_id,
            performed_on=performed_on,
            due_on=due_on,
            outcome=outcome,
            command_id=command_id,
        ),
    )


def record_calibration(  # noqa: PLR0913 - pinned clinic-scoped service contract
    *,
    clinic_id: UUID,
    equipment_id: UUID,
    performed_on: date,
    due_on: date,
    outcome: str,
    command_id: UUID,
) -> Calibration:
    """Retain failed calibration until a later calibration succeeds."""
    return evidence(
        Calibration,
        clinic_id=clinic_id,
        equipment_id=equipment_id,
        performed_on=performed_on,
        due_on=due_on,
        outcome=outcome,
        command_id=command_id,
        operation="record_calibration",
    )


def record_maintenance(  # noqa: PLR0913 - pinned clinic-scoped service contract
    *,
    clinic_id: UUID,
    equipment_id: UUID,
    performed_on: date,
    due_on: date,
    outcome: str,
    command_id: UUID,
) -> Maintenance:
    """Maintenance never substitutes for calibration evidence."""
    return evidence(
        Maintenance,
        clinic_id=clinic_id,
        equipment_id=equipment_id,
        performed_on=performed_on,
        due_on=due_on,
        outcome=outcome,
        command_id=command_id,
        operation="record_maintenance",
    )


def equipment_status(*, clinic_id: UUID, equipment_id: UUID) -> EquipmentStatus:
    """Derive overdue/failed state using the clinic's authoritative local date."""
    authority(clinic_id=clinic_id, permission="inventory.read")
    record(Equipment, clinic_id=clinic_id, record_id=equipment_id)
    checked_on = today(clinic_id=clinic_id)
    calibration = (
        Calibration.objects.filter(clinic_id=clinic_id, equipment_id=equipment_id)
        .order_by("-performed_on", "-created_at", "-pk")
        .first()
    )
    maintenance = (
        Maintenance.objects.filter(clinic_id=clinic_id, equipment_id=equipment_id)
        .order_by("-performed_on", "-created_at", "-pk")
        .first()
    )
    calibration_due = calibration is None or calibration.due_on <= checked_on
    maintenance_due = maintenance is not None and maintenance.due_on <= checked_on
    authority(clinic_id=clinic_id, permission="inventory.read")
    return EquipmentStatus(
        calibration.pk if calibration else None,
        maintenance.pk if maintenance else None,
        calibration_due,
        maintenance_due,
        calibration is not None
        and calibration.outcome == "passed"
        and not calibration_due
        and not maintenance_due
        and (maintenance is None or maintenance.outcome == "completed"),
    )

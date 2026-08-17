"""Organization-scoped patient identity and clinic enrollment models."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, ClassVar, Final

from django.core.exceptions import ValidationError
from django.db import models

from apps.core.idempotency import PatientNameValueError, normalize_patient_name
from apps.identity.models import Clinic
from apps.tenancy.models import TenantScopedModel

if TYPE_CHECKING:
    from django.db.models.constraints import BaseConstraint

    class _NormalizedNameFieldBase(models.CharField[str, str]): ...

else:

    class _NormalizedNameFieldBase(models.CharField): ...


INVALID_NAME_MESSAGE: Final = "Enter a valid normalized patient name."


class NormalizedPatientNameField(_NormalizedNameFieldBase):
    """Normalize patient names on cleaning and every model save path."""

    def to_python(self, value: object) -> str:
        """Normalize textual input before validators execute."""
        converted = super().to_python(value)
        if not isinstance(converted, str):
            raise ValidationError(INVALID_NAME_MESSAGE, code="invalid_patient_name")
        return _normalize_name(converted)

    def pre_save(self, model_instance: models.Model, add: bool) -> str:
        """Normalize the stored name immediately before persistence."""
        del add
        value = getattr(model_instance, self.attname, None)
        if not isinstance(value, str):
            raise ValidationError(INVALID_NAME_MESSAGE, code="invalid_patient_name")
        normalized = _normalize_name(value)
        setattr(model_instance, self.attname, normalized)
        return normalized


def _normalize_name(value: str) -> str:
    try:
        return normalize_patient_name(value)
    except PatientNameValueError as error:
        raise ValidationError(
            INVALID_NAME_MESSAGE,
            code="invalid_patient_name",
        ) from error


class Patient(TenantScopedModel):
    """Minimal organization-level patient identity."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    full_name = NormalizedPatientNameField(max_length=255)
    birth_date = models.DateField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Expose the composite target without natural-person uniqueness."""

        constraints: ClassVar[list[BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("organization", "id"),
                name="intake_patient_org_id_uniq",
            )
        ]


class PatientClinicEnrollment(TenantScopedModel):
    """Bind one organization patient to one authorized clinic."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey(Clinic, on_delete=models.PROTECT)
    patient = models.ForeignKey(Patient, on_delete=models.PROTECT)
    idempotency_key = models.UUIDField()
    create_fingerprint = models.BinaryField(max_length=32, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Prevent duplicate enrollments and keys while preserving identity."""

        constraints: ClassVar[list[BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("organization", "clinic", "patient"),
                name="intake_enrollment_org_clinic_patient_uniq",
            ),
            models.UniqueConstraint(
                fields=("organization", "clinic", "id"),
                name="intake_enrollment_org_clinic_id_uniq",
            ),
            models.UniqueConstraint(
                fields=("organization", "idempotency_key"),
                name="intake_enrollment_org_idempotency_uniq",
            ),
        ]

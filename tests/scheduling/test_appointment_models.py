from importlib import import_module

from apps.intake.models import Patient
from apps.tenancy.models import TenantScopedModel
from django.db import models


def test_appointment_exposes_only_the_phase1a_schema_contract() -> None:
    module = import_module("apps.scheduling.models")
    appointment_model = getattr(module, "Appointment", None)
    assert appointment_model is not None
    assert issubclass(appointment_model, TenantScopedModel)
    assert {field.name for field in appointment_model._meta.fields} == {
        "cancelled_at",
        "cancellation_reason",
        "clinic",
        "create_fingerprint",
        "created_at",
        "end_at",
        "id",
        "idempotency_key",
        "organization",
        "patient",
        "practitioner",
        "start_at",
        "status",
        "updated_at",
    }
    assert appointment_model._meta.get_field("patient").remote_field.model is Patient
    for field_name in ("clinic", "patient", "practitioner"):
        field = appointment_model._meta.get_field(field_name)
        assert field.remote_field.on_delete is models.PROTECT
    assert appointment_model.Status.values == ["scheduled", "cancelled"]
    assert appointment_model.CancellationReason.values == [
        "patient_request",
        "clinic_request",
        "practitioner_unavailable",
        "duplicate",
        "other",
    ]
    assert appointment_model._meta.get_field("create_fingerprint").editable is False

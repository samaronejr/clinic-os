from importlib import import_module
from importlib.util import find_spec

from django.db import models


def test_patient_identity_contract_allows_natural_duplicates() -> None:
    module = (
        import_module("apps.intake.models") if find_spec("apps.intake.models") else None
    )

    assert module is not None
    patient = module.Patient
    enrollment = module.PatientClinicEnrollment

    assert {field.name for field in patient._meta.fields} == {
        "birth_date",
        "created_at",
        "full_name",
        "id",
        "organization",
    }
    assert [constraint.fields for constraint in patient._meta.constraints] == [
        ("organization", "id")
    ]
    name_field = patient._meta.get_field("full_name")
    assert name_field.clean("  A\N{COMBINING ACUTE ACCENT}na   Synthetic  ", None) == (
        "Ána Synthetic"
    )

    assert {field.name for field in enrollment._meta.fields} == {
        "clinic",
        "create_fingerprint",
        "created_at",
        "id",
        "idempotency_key",
        "organization",
        "patient",
    }
    assert enrollment._meta.get_field("clinic").remote_field.on_delete is models.PROTECT
    assert (
        enrollment._meta.get_field("patient").remote_field.on_delete is models.PROTECT
    )
    assert [constraint.fields for constraint in enrollment._meta.constraints] == [
        ("organization", "clinic", "patient"),
        ("organization", "clinic", "id"),
        ("organization", "idempotency_key"),
    ]

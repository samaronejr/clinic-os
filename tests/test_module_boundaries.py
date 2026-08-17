from collections.abc import Callable
from inspect import Parameter, signature
from typing import Final, NoReturn

import pytest
from apps.billing.services import create_invoice
from apps.comms.services import send_message
from apps.consent.services import record_consent
from apps.ehr.services import record_clinical_note
from apps.intake.services import create_patient, search_patients, submit_intake
from apps.interop.services import exchange_clinical_record
from apps.prescription.services import issue_prescription
from apps.retention.services import apply_retention_policy
from apps.scheduling.services import create_appointment
from apps.teleconsult.services import start_teleconsultation
from django.apps import apps as django_apps

FOUNDATION_APP_NAMES: Final = frozenset(
    {
        "django.contrib.admin",
        "django.contrib.auth",
        "django.contrib.contenttypes",
        "django.contrib.messages",
        "django.contrib.sessions",
        "django.contrib.staticfiles",
        "django_otp",
        "django_otp.plugins.otp_static",
        "django_otp.plugins.otp_totp",
        "rest_framework",
    }
)
DOMAIN_APP_CONFIG_PATHS: Final = frozenset(
    {
        "apps.audit.apps.AuditConfig",
        "apps.billing.apps.BillingConfig",
        "apps.comms.apps.CommsConfig",
        "apps.consent.apps.ConsentConfig",
        "apps.ehr.apps.EhrConfig",
        "apps.identity.apps.IdentityConfig",
        "apps.intake.apps.IntakeConfig",
        "apps.interop.apps.InteropConfig",
        "apps.prescription.apps.PrescriptionConfig",
        "apps.retention.apps.RetentionConfig",
        "apps.scheduling.apps.SchedulingConfig",
        "apps.teleconsult.apps.TeleconsultConfig",
        "apps.tenancy.apps.TenancyConfig",
    }
)
DEFERRED_SERVICE_ENTRYPOINTS: Final[tuple[Callable[[], NoReturn], ...]] = (
    create_appointment,
    submit_intake,
    record_clinical_note,
    start_teleconsultation,
    issue_prescription,
    record_consent,
    create_invoice,
    send_message,
    apply_retention_policy,
    exchange_clinical_record,
)


def test_existing_foundation_apps_remain_registered() -> None:
    # Given: Django has loaded the existing Phase 0 application registry
    # When: registered application names are collected
    registered_app_names = {
        app_config.name for app_config in django_apps.get_app_configs()
    }

    # Then: every required Django, DRF, and OTP application remains registered
    assert registered_app_names >= FOUNDATION_APP_NAMES


def test_domain_app_configs_are_registered() -> None:
    # Given: the 13 planned domain application configuration paths
    expected_configs_by_name = {
        config_path.rsplit(".apps.", maxsplit=1)[0]: config_path
        for config_path in DOMAIN_APP_CONFIG_PATHS
    }

    # When: the live Django application registry is inspected
    registered_configs_by_name = {
        app_config.name: (f"{type(app_config).__module__}.{type(app_config).__name__}")
        for app_config in django_apps.get_app_configs()
        if app_config.name.startswith("apps.")
    }

    # Then: every planned domain resolves through its explicit AppConfig class
    assert registered_configs_by_name == expected_configs_by_name


@pytest.mark.parametrize(
    "entrypoint",
    DEFERRED_SERVICE_ENTRYPOINTS,
    ids=(
        "scheduling.create_appointment",
        "intake.submit_intake",
        "ehr.record_clinical_note",
        "teleconsult.start_teleconsultation",
        "prescription.issue_prescription",
        "consent.record_consent",
        "billing.create_invoice",
        "comms.send_message",
        "retention.apply_retention_policy",
        "interop.exchange_clinical_record",
    ),
)
def test_deferred_service_entrypoint_raises_phase_boundary(
    entrypoint: Callable[[], NoReturn],
) -> None:
    # Given: a public service entrypoint for a deferred domain
    # When: the entrypoint is called before Phase >=1
    with pytest.raises(NotImplementedError) as exc_info:
        entrypoint()

    # Then: it fails with the exact shared phase-boundary exception
    assert type(exc_info.value) is NotImplementedError
    assert exc_info.value.args == ("Phase >=1",)


def test_intake_service_boundaries_derive_context_and_keep_search_body_only() -> None:
    create_parameters = signature(create_patient).parameters
    search_parameters = signature(search_patients).parameters

    assert list(create_parameters) == [
        "clinic_id",
        "full_name",
        "birth_date",
        "idempotency_key",
    ]
    assert list(search_parameters) == ["clinic_id", "query", "page", "birth_date"]
    assert all(
        parameter.kind is Parameter.KEYWORD_ONLY
        for parameter in (*create_parameters.values(), *search_parameters.values())
    )
    assert not {
        "actor",
        "actor_id",
        "organization",
        "organization_id",
        "request",
        "url",
        "patient_id",
        "enrollment_id",
    }.intersection(set(create_parameters) | set(search_parameters))

import ast
from collections.abc import Callable
from inspect import Parameter, signature
from pathlib import Path
from typing import Final, NoReturn

import pytest
from apps.billing.services import create_invoice
from apps.comms.services import reminder_send_eligible
from apps.consent.services import record_consent
from apps.ehr.services import record_clinical_note
from apps.intake.services import create_patient, search_patients, submit_intake
from apps.interop.services import exchange_clinical_record
from apps.prescription.services import create_draft as create_prescription_draft
from apps.prescription.services import issue_prescription
from apps.retention.services import apply_retention_policy
from apps.teleconsult.services import create_session
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
        "apps.core.apps.CoreConfig",
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
    issue_prescription,
    apply_retention_policy,
    exchange_clinical_record,
)
REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[2]
# ops/testing is a white-box harness rather than production runtime code.
PRODUCTION_SOURCE_ROOTS: Final = (
    REPOSITORY_ROOT / "apps",
    REPOSITORY_ROOT / "config",
    REPOSITORY_ROOT / "ops" / "container",
    REPOSITORY_ROOT / "ops" / "db",
)
INTERNAL_MODULE_PREFIXES: Final = ("apps.", "config.", "ops.")
INTERNAL_MODULE_ROOTS: Final = {"apps", "config", "ops"}


def _from_import_violations(path: Path, node: ast.ImportFrom) -> list[str]:
    module = node.module or "<relative>"
    if not node.level and not module.startswith(INTERNAL_MODULE_PREFIXES):
        return []
    return [
        f"{path}:{node.lineno} imports {alias.name} from {module}"
        for alias in node.names
        if alias.name.startswith("_")
    ]


def _module_import_violations(path: Path, node: ast.Import) -> list[str]:
    return [
        f"{path}:{node.lineno} imports private module {alias.name}"
        for alias in node.names
        if alias.name.split(".")[0] in INTERNAL_MODULE_ROOTS
        and any(part.startswith("_") for part in alias.name.split(".")[1:])
    ]


def _node_import_violations(path: Path, node: ast.AST) -> list[str]:
    if isinstance(node, ast.ImportFrom):
        return _from_import_violations(path, node)
    if isinstance(node, ast.Import):
        return _module_import_violations(path, node)
    return []


def _parse_python_source(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _production_source_paths() -> list[Path]:
    # Migrations are immutable history; their local SQL helpers stay frozen.
    return [REPOSITORY_ROOT / "manage.py"] + [
        path
        for source_root in PRODUCTION_SOURCE_ROOTS
        for path in sorted(source_root.rglob("*.py"))
        if "migrations" not in path.parts
    ]


def _private_import_violations() -> list[str]:
    return [
        violation
        for source_path in _production_source_paths()
        for node in ast.walk(_parse_python_source(source_path))
        for violation in _node_import_violations(
            source_path.relative_to(REPOSITORY_ROOT), node
        )
    ]


def test_production_modules_do_not_import_private_symbols_across_modules() -> None:
    assert _private_import_violations() == []


def test_existing_foundation_apps_remain_registered() -> None:
    # Given: Django has loaded the existing Phase 0 application registry
    # When: registered application names are collected
    registered_app_names = {
        app_config.name for app_config in django_apps.get_app_configs()
    }

    # Then: every required Django, DRF, and OTP application remains registered
    assert registered_app_names >= FOUNDATION_APP_NAMES


def test_domain_app_configs_are_registered() -> None:
    # Given: the 14 planned domain application configuration paths
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
        "prescription.issue_prescription",
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


def test_prescription_draft_requires_explicit_encounter_patient_issuer_category() -> (
    None
):
    parameters = signature(create_prescription_draft).parameters
    assert list(parameters) == [
        "clinic_id",
        "encounter_id",
        "patient_id",
        "issuer_id",
        "category",
    ]
    assert all(p.kind is Parameter.KEYWORD_ONLY for p in parameters.values())


def test_invoice_requires_explicit_scope_and_exact_minor_units() -> None:
    parameters = signature(create_invoice).parameters
    assert list(parameters) == [
        "clinic_id",
        "patient_id",
        "amount_minor",
        "idempotency_key",
        "appointment_id",
        "encounter_id",
    ]
    assert all(p.kind is Parameter.KEYWORD_ONLY for p in parameters.values())


def test_consent_requires_exact_offer_purpose_and_explicit_action() -> None:
    parameters = signature(record_consent).parameters
    assert list(parameters) == ["offer", "purpose", "accepted"]
    assert all(p.kind is Parameter.KEYWORD_ONLY for p in parameters.values())


def test_clinical_note_requires_exact_version_and_expected_revision() -> None:
    parameters = signature(record_clinical_note).parameters
    assert list(parameters) == [
        "clinic_id",
        "version_id",
        "expected_revision",
        "content",
    ]
    assert all(p.kind is Parameter.KEYWORD_ONLY for p in parameters.values())


def test_reminder_eligibility_requires_stored_operation_scope() -> None:
    assert list(signature(reminder_send_eligible).parameters) == ["scope"]


def test_teleconsult_session_requires_exact_clinic_and_encounter() -> None:
    parameters = signature(create_session).parameters
    assert list(parameters) == ["clinic_id", "encounter_id"]
    assert all(p.kind is Parameter.KEYWORD_ONLY for p in parameters.values())


def test_intake_submission_requires_explicit_response_and_revision() -> None:
    parameters = signature(submit_intake).parameters
    assert list(parameters) == ["response_id", "answers", "expected_revision"]
    assert all(p.kind is Parameter.KEYWORD_ONLY for p in parameters.values())


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

from __future__ import annotations

from hashlib import sha256
from inspect import Parameter, signature
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.identity.permissions import (
    BUNDLES_V1,
    BUNDLES_V2,
    INSURANCE_PERMISSIONS_V2,
    PATIENT_SCOPED_PERMISSIONS_V1,
)
from apps.insurance import services
from apps.insurance.batches import batch_bytes, require_rehearsal
from apps.insurance.errors import InsuranceConflictError, InsuranceUnavailableError
from apps.insurance.models import Appeal, Batch, Return
from apps.insurance.rls import TABLES
from apps.insurance.states import TRANSITIONS, require_transition
from django.apps import apps
from django.db.migrations.loader import MigrationLoader

from infra.insurance_catalog import TABLES as EXPECTED_TABLES
from insurance.test_tiss import facts

if TYPE_CHECKING:
    from pytest_django.fixtures import SettingsWrapper


def test_dedicated_v2_permission_has_no_read_or_chart_write_side_effects() -> None:
    assert set(BUNDLES_V2) == set(BUNDLES_V1)
    for role, permissions in BUNDLES_V2.items():
        assert permissions & INSURANCE_PERMISSIONS_V2 == (
            {"tiss.clinical_approve"} if role == "physician" else set()
        )
        assert permissions >= BUNDLES_V1[role]
    assert "tiss.clinical_read" in PATIENT_SCOPED_PERMISSIONS_V1
    assert "tiss.clinical_approve" not in PATIENT_SCOPED_PERMISSIONS_V1
    assert "tiss.clinical_approve" not in set().union(*BUNDLES_V1.values())
    assert {"tiss.clinical_approve"} == INSURANCE_PERMISSIONS_V2


def test_all_insurance_commands_are_keyword_only_and_explicitly_scoped() -> None:
    for name in services.__all__:
        if name in {
            "GuideSelection",
            "ContractTerms",
            "MembershipTerms",
            "PayerBackfill",
            "PayerCheck",
        }:
            continue
        parameters = signature(getattr(services, name)).parameters
        assert "clinic_id" in parameters
        assert not {"actor", "request", "organization_id"} & parameters.keys()
        assert all(p.kind is Parameter.KEYWORD_ONLY for p in parameters.values())


@pytest.mark.parametrize(
    ("kind", "current", "target"),
    [
        (kind, current, target)
        for kind, pairs in TRANSITIONS.items()
        for current, target in sorted(pairs)
    ],
)
def test_legal_state_transitions(kind: str, current: str, target: str) -> None:
    require_transition(kind=kind, current=current, target=target)


@pytest.mark.parametrize(
    ("kind", "current", "target"),
    [
        ("batch", "draft", "submitted"),
        ("batch", "unknown", "submitting"),
        ("batch", "acknowledged", "paid"),
        ("appeal", "draft", "submitted"),
        ("guide", "sealed", "draft"),
    ],
)
def test_skipped_or_unsafe_transitions_fail(
    kind: str, current: str, target: str
) -> None:
    with pytest.raises(InsuranceConflictError):
        require_transition(kind=kind, current=current, target=target)


def test_schema_state_and_posture_registry_match_without_database_access() -> None:
    state = MigrationLoader(None).project_state()
    tables = {
        model._meta.db_table for model in apps.get_app_config("insurance").get_models()
    }
    assert tables == set(TABLES) == set(EXPECTED_TABLES)
    for model in apps.get_app_config("insurance").get_models():
        frozen = state.apps.get_model("insurance", model.__name__)
        assert {
            (field.name, field.__class__, field.null)
            for field in model._meta.local_fields
        } == {
            (field.name, field.__class__, field.null)
            for field in frozen._meta.local_fields
        }


def test_sealed_byte_tampering_is_refused_before_any_export() -> None:
    from apps.insurance.tiss import PINNED_VERSIONS, generate_batch  # noqa: PLC0415

    xml = generate_batch(facts=facts(), versions=PINNED_VERSIONS)
    batch = Batch(
        id=uuid4(),
        xml=xml.decode(),
        content_sha256=sha256(xml).hexdigest(),
        versions=dict(PINNED_VERSIONS),
    )
    assert batch_bytes(batch) == xml
    batch.xml = batch.xml.replace("123.45", "123.46")
    with pytest.raises(InsuranceConflictError):
        batch_bytes(batch)


def test_return_envelope_retains_non_utf8_source_bytes_exactly() -> None:
    field = Return._meta.get_field("xml")
    source = b'<?xml version="1.0" encoding="ISO-8859-1"?><x>Sint\xe9tico</x>'
    assert field._encode(source) == source


def test_empty_clinical_assertions_can_be_stored_without_inventing_approval() -> None:
    field = Appeal._meta.get_field("clinical_assertions")
    assert field._encode("") is None
    assert field.null


def test_synthetic_default_off_refuses_all_operator_legs(
    settings: SettingsWrapper,
) -> None:
    settings.INSURANCE_SYNTHETIC_ENABLED = False
    with pytest.raises(InsuranceUnavailableError):
        require_rehearsal()


def test_live_operator_leg_cannot_be_enabled_by_a_synthetic_flag(
    settings: SettingsWrapper, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.INSURANCE_SYNTHETIC_ENABLED = True
    monkeypatch.setattr("apps.insurance.batches.resolved_data_mode", lambda: "live")
    with pytest.raises(InsuranceUnavailableError):
        require_rehearsal()

"""Todo 52: closed care vocabularies and pure scheduling arithmetic, no database."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from apps.workflows.care import pro_instruments
from apps.workflows.care.validation import (
    CareActionSpec,
    PROSpec,
    ReferralSpec,
    due_after_days,
    return_window,
    validate_action,
    validate_pro,
    validate_referral,
)
from apps.workflows.validation import WorkflowInputError
from django.utils import translation
from django.utils.translation import gettext

NOW = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)


def action(**changes: object) -> CareActionSpec:
    terms: dict[str, object] = {
        "kind": "return_window",
        "window_start": NOW + timedelta(days=42),
        "window_end": NOW + timedelta(days=49),
    }
    terms.update(changes)
    return CareActionSpec(**terms)  # type: ignore[arg-type]


def test_return_window_is_whole_weeks_from_the_injected_instant() -> None:
    start, end = return_window(now=NOW, weeks=6)
    assert start == NOW + timedelta(weeks=6)
    assert end == start + timedelta(days=7)
    for weeks in (0, -1, 105, True, 1.5):
        with pytest.raises(WorkflowInputError):
            return_window(now=NOW, weeks=weeks)  # type: ignore[arg-type]
    with pytest.raises(WorkflowInputError):
        return_window(now=NOW.replace(tzinfo=None), weeks=6)


@pytest.mark.parametrize(
    "changes",
    [
        {"kind": "diagnosis"},
        {"window_end": NOW + timedelta(days=41)},
        {"window_start": NOW.replace(tzinfo=None)},
        {"window_end": NOW + timedelta(days=42 + 800)},
        {"instruction": "x"},
        {"referral": ReferralSpec(target_kind="specialty", target_label="Cardiologia")},
        {"kind": "patient_instruction"},
        {"kind": "patient_instruction", "instruction": "x" * 2001},
        {"kind": "referral"},
    ],
)
def test_malformed_actions_are_rejected_without_reflecting_input(
    changes: dict[str, object],
) -> None:
    with pytest.raises(WorkflowInputError) as error:
        validate_action(action(**changes))
    assert "x" * 5 not in str(error.value)


def test_typed_payload_is_attached_only_to_its_own_kind() -> None:
    assert validate_action(action())["kind"] == "return_window"
    note = action(kind="patient_instruction", instruction="Repouso relativo.")
    assert validate_action(note)["instruction"] == "Repouso relativo."
    refer = action(
        kind="referral",
        referral=ReferralSpec(target_kind="specialty", target_label="Cardiologia"),
    )
    assert validate_action(refer)["kind"] == "referral"


@pytest.mark.parametrize(
    "spec",
    [
        ReferralSpec(target_kind="hospital", target_label="Clinica"),
        ReferralSpec(target_kind="specialty", target_label=""),
        ReferralSpec(target_kind="external", target_label="y" * 201),
        ReferralSpec(target_kind="unspecified", target_label="Clinica"),
    ],
)
def test_referral_targets_are_closed_and_bounded(spec: ReferralSpec) -> None:
    with pytest.raises(WorkflowInputError):
        validate_referral(spec)


def test_due_after_days_is_bounded_whole_days() -> None:
    assert due_after_days(now=NOW, days=30) == NOW + timedelta(days=30)
    for days in (0, -3, 731, 1.5, True):
        with pytest.raises(WorkflowInputError):
            due_after_days(now=NOW, days=days)  # type: ignore[arg-type]


def pro(**changes: object) -> PROSpec:
    terms: dict[str, object] = {
        "instrument_key": "phq9",
        "first_due_at": NOW + timedelta(days=14),
        "interval_days": 14,
        "occurrences": 4,
        "response_role": "physician",
        "staffing_policy_ref": "eg3-pending-staffing-policy",
    }
    terms.update(changes)
    return PROSpec(**terms)  # type: ignore[arg-type]


def test_only_instruments_whose_license_permits_use_can_be_assigned() -> None:
    assert pro_instruments.available_keys() == frozenset({"phq9", "gad7"})
    assert validate_pro(pro())["instrument_key"] == "phq9"
    assert validate_pro(pro(instrument_key="gad7"))["instrument_version"] == 1
    for key in ("growth_percentile", "bmi", "unknown", ""):
        with pytest.raises(WorkflowInputError):
            validate_pro(pro(instrument_key=key))


@pytest.mark.parametrize(
    "changes",
    [
        {"interval_days": 0},
        {"interval_days": 366},
        {"occurrences": 0},
        {"occurrences": 13},
        {"response_role": "patient"},
        {"staffing_policy_ref": ""},
        {"staffing_policy_ref": "Has Spaces"},
        {"first_due_at": NOW.replace(tzinfo=None)},
        {"plan_id": "not-a-uuid"},
    ],
)
def test_pro_schedule_and_queue_terms_are_closed(changes: dict[str, object]) -> None:
    with pytest.raises(WorkflowInputError):
        validate_pro(pro(**changes))


def test_pro_plan_link_must_be_a_uuid() -> None:
    identifier = uuid4()
    assert validate_pro(pro(plan_id=identifier))["plan_id"] == str(identifier)
    for value in (str(identifier), identifier.int, ""):
        with pytest.raises(WorkflowInputError):
            validate_pro(pro(plan_id=value))


def test_concern_is_declared_by_the_registry_and_nonresponse_never_scores() -> None:
    assert pro_instruments.concerning("phq9", {"item_9": "1"}) is True
    assert pro_instruments.concerning("phq9", {"item_9": "0", "item_1": "3"}) is False
    assert pro_instruments.concerning("gad7", {"item_1": "3"}) is False
    # Without an answer there is nothing to read: absence is not a score.
    assert pro_instruments.concerning("phq9", {}) is False
    assert pro_instruments.NO_RESPONSE_LABEL == "No response"


def test_nonresponse_label_is_translated_for_the_pt_br_catalog() -> None:
    with translation.override("pt-br"):
        assert gettext(pro_instruments.NO_RESPONSE_LABEL) == "Sem resposta"

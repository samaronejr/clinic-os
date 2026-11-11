"""Unit economics model: exact fixture totals, source labels, rejected inputs."""

from __future__ import annotations

import copy
import json
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest
import yaml
from ops.economics import model

if TYPE_CHECKING:
    from pathlib import Path

FIXTURE_VALUES: dict[str, object] = {
    "clinics": 2,
    "clinicians_per_clinic": 1,
    "visits_per_clinician_day": 10,
    "capture_share": 0.5,
    "capture_minutes_per_visit": 20,
    "retry_overhead": 0.1,
    "burst_factor": 2,
    "working_days_per_month": 20,
    "long_consult_share": 0.1,
    "long_consult_factor": 2,
    "caregiver_share": 0.2,
    "caregiver_factor": 1.5,
    "attachments_per_visit": 1,
    "attachment_gb": 0.5,
    "teleconsult_share": 0.1,
    "video_participants": 2,
    "messages_per_visit": 2,
    "llm_input_tokens_per_visit": 1000,
    "llm_output_tokens_per_visit": 500,
    "llm_cached_tokens_per_visit": 0,
    "p_asr": 0.1,
    "p_llm_input": 2,
    "p_llm_output": 4,
    "p_llm_cached": 1,
    "p_video": 0.05,
    "p_message": 0.2,
    "p_storage": 0.1,
    "fees_per_visit": 1,
    "fixed_per_clinic_month": 100,
}


def _committed() -> dict[str, Any]:
    loaded = yaml.safe_load(model.VARIABLES_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _fixture() -> dict[str, Any]:
    document = _committed()
    for name, value in FIXTURE_VALUES.items():
        document["variables"][name]["value"] = value
    return document


def test_fixture_totals_are_exact() -> None:
    expected = model.run(_fixture())["scenarios"]["expected"]
    usage = expected["usage"]
    assert usage["reference_audio_minutes_per_day"] == Decimal(200)
    assert usage["billable_audio_minutes_per_day"] == Decimal(264)
    assert usage["provisioned_audio_minutes_per_day"] == Decimal(480)
    cogs = expected["cogs_per_clinic_month"]
    assert cogs["components"] == {
        "fixed": Decimal(100),
        "asr": Decimal(264),
        "llm": Decimal("0.44"),
        "video": Decimal(40),
        "messages": Decimal(80),
        "storage": Decimal(10),
        "fees": Decimal(200),
    }
    assert cogs["total"] == Decimal("694.44")
    assert expected["cogs_profile_month"] == Decimal("1388.88")


def test_p100_reference_audio_and_unpriced_costs_stay_null() -> None:
    output = model.run(_committed())
    for name in model.SCENARIOS:
        result = output["scenarios"][name]
        assert result["usage"]["reference_audio_minutes_per_day"] == Decimal(300_000)
        assert result["cogs_per_clinic_month"]["total"] is None
    assert {"p_asr", "p_llm_input", "fixed_per_clinic_month"} <= set(
        output["owner_input_required"]
    )


def test_every_variable_has_source_and_date_or_assumption_label() -> None:
    for name, spec in _committed()["variables"].items():
        assert str(spec["source"]).strip(), name
        assert spec.get("date") or spec.get("assumption") is True, name
        if name.startswith(("p_", "fees_", "fixed_")):
            assert spec["value"] is None, name
            assert spec["owner_input_required"] is True, name


@pytest.mark.parametrize("drop", ["source", "date"])
def test_missing_source_label_fails(drop: str) -> None:
    document = copy.deepcopy(_committed())
    del document["variables"]["clinics"][drop]
    with pytest.raises(model.EconomicsInputError, match="clinics"):
        model.run(document)


def test_unmarked_null_value_fails() -> None:
    document = copy.deepcopy(_committed())
    del document["variables"]["p_asr"]["owner_input_required"]
    with pytest.raises(model.EconomicsInputError, match="p_asr"):
        model.run(document)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("visits_per_clinician_day", -1),
        ("long_consult_share", {"low": 0, "expected": -0.1, "high": 1}),
        ("p_asr", -0.01),
    ],
)
def test_negative_inputs_rejected(name: str, value: object) -> None:
    document = _fixture()
    document["variables"][name]["value"] = value
    with pytest.raises(model.EconomicsInputError, match=name):
        model.run(document)


def test_guardrails_alert_at_eighty_percent_of_high_quota() -> None:
    meters = model.run(_fixture())["guardrails"]["meters"]
    assert set(meters) == set(model.SEEDED_QUOTAS)
    audio = meters["ai_audio_seconds"]
    assert audio["alert_at"] == -(-audio["proposed_quota"] * 4 // 5)


def test_cli_writes_json(tmp_path: Path) -> None:
    out = tmp_path / "economics.json"
    assert model.main(["--output", str(out)]) == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    expected = data["scenarios"]["expected"]
    assert expected["usage"]["reference_audio_minutes_per_day"] == "300000"

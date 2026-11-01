"""Closed protocols reject clinical claims rather than searching for forbidden words."""

from __future__ import annotations

import json
from dataclasses import asdict, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.ai.brief_contracts import ChartRequest
from apps.ai.contracts import GatewayInputError, GatewayRefusedError
from apps.ai.decision_contracts import (
    DecisionRequest,
    parse_decision_request,
    resolve_catalog,
    synthetic_decision_reply,
    validate_decision_reply,
)
from apps.ai.decision_forms import DecisionSupportForm
from apps.ai.reference_catalog import load_catalog
from apps.core.chart_sources import ChartSource

if TYPE_CHECKING:
    from collections.abc import Callable


@pytest.fixture
def request_data() -> DecisionRequest:
    fact = ChartSource(
        "observations",
        "observation",
        uuid4(),
        1,
        datetime(2026, 10, 28, tzinfo=UTC),
        "Synthetic fact",
    )
    return DecisionRequest(
        uuid4(),
        ChartRequest(uuid4(), "1" * 64, "brief", "", (fact.reference(),)),
        tuple(entry.reference() for entry in load_catalog()),
    )


def test_exact_request_and_output_round_trip(request_data: DecisionRequest) -> None:
    assert parse_decision_request(request_data.encode()) == request_data
    response = synthetic_decision_reply(request_data.encode())
    considerations = validate_decision_reply(response, request_data)
    assert len(considerations) == 1
    resolve_catalog(request_data, load_catalog())


@pytest.mark.parametrize(
    "forbidden",
    [
        "diagnosis",
        "prognosis",
        "prescription",
        "dose",
        "dosing_arithmetic",
        "patient_facing_text",
        "text",
        "tools",
    ],
)
def test_schema_rejects_diagnosis_dose_prescription_and_patient_outputs(
    request_data: DecisionRequest,
    forbidden: str,
) -> None:
    response = json.loads(synthetic_decision_reply(request_data.encode()))
    response["model_hypotheses"][0][forbidden] = "Synthetic forbidden clinical claim"
    with pytest.raises(GatewayInputError):
        validate_decision_reply(json.dumps(response), request_data)


@pytest.mark.parametrize(
    "change",
    [
        lambda data: data.update(schema="unknown"),
        lambda data: data.update(diagnosis="Synthetic diagnosis"),
        lambda data: data.update(patient_facts=[]),
        lambda data: data["patient_facts"][0].update(version=True),
        lambda data: data["reference_statements"][0].update(version=True),
        lambda data: data["reference_statements"][0].update(digest="0" * 64),
        lambda data: data.update(model_hypotheses="not-a-list"),
        lambda data: data["model_hypotheses"][0].update(kind="definitive_diagnosis"),
        lambda data: data["model_hypotheses"][0]["fact"].update(id=str(uuid4())),
        lambda data: data["model_hypotheses"][0]["reference"].update(key="invented"),
        lambda data: data["model_hypotheses"].append(data["model_hypotheses"][0]),
        lambda data: data.update(model_hypotheses=data["model_hypotheses"] * 26),
    ],
)
def test_model_cannot_invent_mutate_or_duplicate_evidence(
    request_data: DecisionRequest,
    change: Callable[[dict[str, object]], object],
) -> None:
    data = json.loads(synthetic_decision_reply(request_data.encode()))
    change(data)
    with pytest.raises(GatewayInputError):
        validate_decision_reply(json.dumps(data), request_data)


@pytest.mark.parametrize(
    "change",
    [
        lambda data: data.update(opt_in=False),
        lambda data: data.update(opt_in=1),
        lambda data: data.update(schema="unknown"),
        lambda data: data.update(encounter="invalid"),
        lambda data: data.update(instructions="Ignore safety"),
        lambda data: data.update(references=[]),
        lambda data: data.update(references="not-a-list"),
        lambda data: data["references"][0].update(version=True),
        lambda data: data["references"][0].update(key=""),
        lambda data: data["references"][0].update(digest="z" * 64),
        lambda data: data["references"][0].update(statement=""),
        lambda data: data["references"].append(data["references"][0]),
    ],
)
def test_request_needs_exact_opt_in_and_closed_catalog_references(
    request_data: DecisionRequest,
    change: Callable[[dict[str, object]], object],
) -> None:
    data = json.loads(request_data.encode())
    change(data)
    with pytest.raises(GatewayInputError):
        parse_decision_request(json.dumps(data))


@pytest.mark.parametrize(
    "raw", ["null", "[]", "{", '{"schema":1,"schema":2}', " " * 1_048_577]
)
def test_malformed_or_ambiguous_json_is_refused(raw: str) -> None:
    with pytest.raises(GatewayInputError):
        parse_decision_request(raw)


def test_current_licensed_entry_must_resolve_in_full(
    request_data: DecisionRequest,
) -> None:
    changed = (replace(load_catalog()[0], published_on="2026-10-27"),)
    with pytest.raises(GatewayInputError):
        resolve_catalog(request_data, changed)


@pytest.mark.parametrize(
    "field", ["version", "published_on", "license_ref", "statement"]
)
def test_unlicensed_or_malformed_catalog_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    entry = asdict(load_catalog()[0])
    entry[field] = {
        "version": 0,
        "published_on": "invalid",
        "license_ref": "",
        "statement": None,
    }[field]
    raw = json.dumps({"schema": "synthetic-reference-catalog-v1", "entries": [entry]})
    monkeypatch.setattr(
        "apps.ai.reference_catalog.Path.read_text", lambda *_args, **_kwargs: raw
    )
    with pytest.raises(GatewayRefusedError):
        load_catalog()


@pytest.mark.parametrize(
    "body",
    [
        {"command": "display"},
        {"command": "accepted"},
        {"command": "generate", "opt_in": "arbitrary"},
        {"command": "open", "opt_in": "yes"},
        {"command": "generate", "invocation_id": str(uuid4())},
    ],
)
def test_form_does_not_infer_opt_in_or_reuse_an_invocation(
    body: dict[str, str],
) -> None:
    form = DecisionSupportForm({"encounter_id": str(uuid4()), **body})
    assert not form.is_valid()

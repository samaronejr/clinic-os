"""Task 63: the model can select a catalog query, never write a query."""

import json
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path

import pytest
from apps.analytics.catalog import METRICS
from apps.analytics.contracts import (
    AnalyticsInputError,
    descriptor,
    parse_descriptor,
    render_value,
)


def test_catalog_has_every_initial_metric_and_complete_versioned_definitions() -> None:
    schema = json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "docs/analytics/metric-definition-v1.json"
        ).read_text()
    )
    assert set(METRICS) == {
        "utilization",
        "no_show",
        "waiting_time",
        "task_turnaround",
        "receivables",
        "verified_collections",
        "net_collections",
        "denials",
        "payout_liabilities",
        "result_ack_latency",
        "follow_up_overdue",
        "support_load",
        "ai_correction_time",
        "activation",
        "retained_usage",
    }
    for metric in METRICS.values():
        specification = metric.specification()
        import_module("jsonschema").Draft202012Validator(schema).validate(specification)
        assert specification["version"] == 1
        assert specification["authorization"]
        assert specification["provenance"]
        assert specification["quality"]
        assert specification["numerator"]
        assert specification["denominator"]
        formula = specification["formula"]
        assert isinstance(formula, dict)
        assert formula["op"] in {"sum", "ratio"}


def test_descriptor_roundtrip_and_zero_denominator_never_invents_a_rate() -> None:
    value = descriptor(
        metric_id="no_show",
        start=datetime(2026, 11, 1, tzinfo=UTC),
        end=datetime(2026, 11, 7, tzinfo=UTC),
        tz="America/Sao_Paulo",
    )
    assert parse_descriptor(json.dumps(value)).metric_id == "no_show"
    assert render_value("no_show", "0", "0") is None
    assert render_value("no_show", "1", "4") == "25.00"
    assert render_value("receivables", "9007199254740993", "1") == "9007199254740993"


@pytest.mark.parametrize(
    "patch",
    [
        {"sql": "SELECT * FROM identity_user"},
        {"clinics": "all"},
        {"metric_id": "arbitrary"},
        {"version": True},
        {"version": 2},
        {"dimensions": ["income"]},
        {"dimensions": ["clinic"]},
        {"filters": {"postcode": "00000"}},
        {"window": {"start": "2026-11-01", "end": "2026-11-02"}},
        {"tz": "Mars/Olympus"},
    ],
)
def test_untrusted_model_output_outside_the_schema_is_rejected(
    patch: dict[str, object],
) -> None:
    value = descriptor(
        metric_id="no_show",
        start=datetime(2026, 11, 1, tzinfo=UTC),
        end=datetime(2026, 11, 7, tzinfo=UTC),
        tz="America/Sao_Paulo",
    )
    with pytest.raises(AnalyticsInputError, match="invalid metric query"):
        parse_descriptor(json.dumps(value | patch))


def test_duplicate_keys_and_nonfinite_numbers_are_rejected() -> None:
    with pytest.raises(AnalyticsInputError):
        parse_descriptor('{"metric_id":"no_show","metric_id":"receivables"}')
    with pytest.raises(AnalyticsInputError):
        parse_descriptor('{"version":NaN}')


@pytest.mark.parametrize(
    "start",
    [
        "20261101T000000+0000",
        "2026-11-01 00:00:00+00:00",
        "2026-11-01T00:00:00.0000001+00:00",
    ],
)
def test_nl_datetimes_reject_non_schema_isoformat_extensions(start: str) -> None:
    value = descriptor(
        metric_id="no_show",
        start=datetime(2026, 11, 1, tzinfo=UTC),
        end=datetime(2026, 11, 7, tzinfo=UTC),
        tz="America/Sao_Paulo",
    )
    value["window"] = {"start": start, "end": "2026-11-07T00:00:00+00:00"}
    with pytest.raises(AnalyticsInputError):
        parse_descriptor(json.dumps(value))


def test_journal_aggregates_keep_exact_cents_beyond_decimal_precision() -> None:
    amount = "9223372036854775807000000000000000000000"
    assert render_value("receivables", amount, "1") == amount

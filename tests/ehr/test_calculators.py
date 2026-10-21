"""Sourced arithmetic is exact, bounded, and refuses ambiguous input."""

from __future__ import annotations

import subprocess
import sys
from decimal import ROUND_DOWN, localcontext
from pathlib import Path

import pytest
from apps.ehr.calculators import calculate
from apps.ehr.calculators.catalog import DEFINITIONS
from apps.ehr.calculators.contracts import Calculation, Refusal

_run_process = subprocess.run


def test_dispatch_import_does_not_require_django_setup() -> None:
    completed = _run_process(
        (
            sys.executable,
            "-c",
            "from django.conf import settings; "
            "assert not settings.configured; "
            "from apps.ehr.calculators import calculate; "
            "result = calculate(key='bmi', version=1, inputs={"
            "'weight':'70','weight_unit':'kg','height':'175','height_unit':'cm'}); "
            "assert not settings.configured; "
            "print(result.value)",
        ),
        check=False,
        capture_output=True,
        cwd=Path(__file__).resolve().parents[2],
        env={"PYTHONDONTWRITEBYTECODE": "1", "PYTHONTZPATH": ""},
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "22.86"


@pytest.mark.parametrize("key", DEFINITIONS)
def test_registry_boundary_vectors_are_executable_and_version_bound(key: str) -> None:
    definition = DEFINITIONS[key]
    assert definition.boundary_vectors
    for vector in definition.boundary_vectors:
        assert (
            calculate(key=key, version=definition.version, inputs=dict(vector.inputs))
            == vector.expected
        )


@pytest.mark.parametrize(
    ("inputs", "expected"),
    [
        (
            {"weight": "70", "weight_unit": "kg", "height": "175", "height_unit": "cm"},
            "22.86",
        ),
        (
            {
                "weight": "70000",
                "weight_unit": "g",
                "height": "1.75",
                "height_unit": "m",
            },
            "22.86",
        ),
        (
            {
                "weight": "16.9",
                "weight_unit": "kg",
                "height": "105.4",
                "height_unit": "cm",
            },
            "15.21",
        ),
        (
            {
                "weight": "16.6",
                "weight_unit": "kg",
                "height": "99.1",
                "height_unit": "cm",
            },
            "16.90",
        ),
        (
            {
                "weight": "18.3",
                "weight_unit": "kg",
                "height": "103.5",
                "height_unit": "cm",
            },
            "17.08",
        ),
    ],
)
def test_bmi_reference_arithmetic_and_unit_conversion(
    inputs: dict[str, str], expected: str
) -> None:
    result = calculate(key="bmi", version=1, inputs=inputs)
    assert isinstance(result, Calculation)
    assert (result.value, result.unit, result.version) == (expected, "kg/m2", 1)
    assert result.source


@pytest.mark.parametrize(
    "value",
    ["70,5", "1,234.5", "1.234,5", "1 234", "NaN", "Infinity", "1e3", "-1", "0"],
)
def test_bmi_refuses_ambiguous_or_nonpositive_measurement(value: str) -> None:
    result = calculate(
        key="bmi",
        version=1,
        inputs={
            "weight": value,
            "weight_unit": "kg",
            "height": "175",
            "height_unit": "cm",
        },
    )
    assert isinstance(result, Refusal)
    assert result.code == "invalid_inputs"


@pytest.mark.parametrize("key", ["bmi", "egfr", "corrected_age"])
def test_missing_inputs_never_produce_a_result(key: str) -> None:
    assert isinstance(calculate(key=key, version=1, inputs={}), Refusal)


def test_corrected_age_published_aap_example() -> None:
    result = calculate(
        key="corrected_age",
        version=1,
        inputs={"chronological_age": "16", "gestational_age": "32"},
    )
    assert isinstance(result, Calculation)
    assert (result.value, result.unit) == ("8.00", "wk")


@pytest.mark.parametrize(
    ("chronological", "gestational", "expected"),
    [("0", "40", "0.00"), ("8", "32", "0.00"), ("7", "32", "-1.00")],
)
def test_corrected_age_preserves_before_due_date_values(
    chronological: str, gestational: str, expected: str
) -> None:
    result = calculate(
        key="corrected_age",
        version=1,
        inputs={"chronological_age": chronological, "gestational_age": gestational},
    )
    assert isinstance(result, Calculation)
    assert result.value == expected


@pytest.mark.parametrize(("sex", "creatinine"), [("female", "0.7"), ("male", "0.9")])
def test_egfr_kappa_boundary_and_conversion(sex: str, creatinine: str) -> None:
    common = {"age": "18", "sex": sex}
    conventional = calculate(
        key="egfr",
        version=1,
        inputs={**common, "creatinine": creatinine, "creatinine_unit": "mg/dL"},
    )
    si_value = "61.88" if sex == "female" else "79.56"
    si = calculate(
        key="egfr",
        version=1,
        inputs={**common, "creatinine": si_value, "creatinine_unit": "umol/L"},
    )
    assert isinstance(conventional, Calculation)
    assert si == conventional
    assert conventional.value == ("128.48" if sex == "female" else "126.96")


def test_egfr_refuses_children_and_unknown_formula_sex() -> None:
    for inputs in (
        {"age": "17", "sex": "female"},
        {"age": "18", "sex": "unspecified"},
    ):
        result = calculate(
            key="egfr",
            version=1,
            inputs={**inputs, "creatinine": "1", "creatinine_unit": "mg/dL"},
        )
        assert isinstance(result, Refusal)


def test_licensed_data_is_not_enabled_by_arbitrary_inputs() -> None:
    result = calculate(
        key="growth_percentile", version=1, inputs={"license_status": "available"}
    )
    assert isinstance(result, Refusal)
    assert result.code == "license_pending"


def test_phq9_publisher_worked_example_is_sixteen() -> None:
    scores = (0, 1, 1, 1, 2, 2, 3, 3, 3)
    result = calculate(
        key="phq9",
        version=1,
        inputs={f"item_{index}": str(score) for index, score in enumerate(scores, 1)},
    )
    assert isinstance(result, Calculation)
    assert (result.value, result.unit) == ("16", "1")


@pytest.mark.parametrize(("key", "count"), [("phq9", 9), ("gad7", 7)])
@pytest.mark.parametrize("score", ["0", "3"])
def test_instrument_published_score_boundaries(
    key: str, count: int, score: str
) -> None:
    result = calculate(
        key=key,
        version=1,
        inputs={f"item_{index}": score for index in range(1, count + 1)},
    )
    assert isinstance(result, Calculation)
    assert result.value == str(count * int(score))


@pytest.mark.parametrize(("key", "count"), [("phq9", 9), ("gad7", 7)])
@pytest.mark.parametrize("value", ["", "4", "-1", "1.0", "1,0"])
def test_instruments_never_impute_or_coerce_scores(
    key: str, count: int, value: str
) -> None:
    result = calculate(
        key=key,
        version=1,
        inputs={f"item_{index}": value for index in range(1, count + 1)},
    )
    assert isinstance(result, Refusal)
    assert isinstance(calculate(key=key, version=1, inputs={}), Refusal)


def test_calculation_owns_decimal_context_and_rejects_unknown_versions() -> None:
    values = {"weight": "70", "weight_unit": "kg", "height": "175", "height_unit": "cm"}
    with localcontext() as context:
        context.prec = 2
        context.rounding = ROUND_DOWN
        result = calculate(key="bmi", version=1, inputs=values)
    assert isinstance(result, Calculation)
    assert result.value == "22.86"
    assert isinstance(calculate(key="bmi", version=2, inputs=values), Refusal)
    assert isinstance(calculate(key="__import__", version=1, inputs=values), Refusal)


@pytest.mark.parametrize(
    ("key", "inputs", "expected"),
    [
        (
            "bmi",
            {"weight": "70", "weight_unit": "kg", "height": "175", "height_unit": "cm"},
            "22.86",
        ),
        (
            "corrected_age",
            {"chronological_age": "104", "gestational_age": "32"},
            "96.00",
        ),
        (
            "egfr",
            {
                "age": "18",
                "sex": "male",
                "creatinine": "0.9",
                "creatinine_unit": "mg/dL",
            },
            "126.96",
        ),
    ],
)
def test_ambient_exponent_limits_cannot_change_arithmetic(
    key: str, inputs: dict[str, str], expected: str
) -> None:
    with localcontext() as caller:
        caller.prec = 2
        caller.rounding = ROUND_DOWN
        caller.Emax = 0
        caller.Emin = 0
        result = calculate(key=key, version=1, inputs=inputs)
        assert caller.Emax == 0
        assert caller.prec == 2
    assert isinstance(result, Calculation)
    assert result.value == expected

"""Clinic Ops unit economics model (todo 74, docs/business/economics.md).

Pure Python over ``ops/economics/variables.yaml``. Every variable must carry a
``source`` and either a ``date`` or ``assumption: true``; negative inputs are
rejected. A null variable (no sourced number yet) makes every cost that depends
on it null, and the output lists it under ``owner_input_required``. Arithmetic
uses ``Decimal`` so fixture totals are exact.

Run: ``uv run python -m ops.economics.model --output economics.json``.
"""

from __future__ import annotations

import argparse
import json
import math
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, Never

import yaml

VARIABLES_PATH: Final = Path(__file__).with_name("variables.yaml")
SCENARIOS: Final = ("low", "expected", "high")
MILLION: Final = Decimal(1_000_000)
BYTES_PER_GB: Final = Decimal(1_000_000_000)
# apps/subscriptions/services.py raises the spending alert at used*5 >= quota*4.
ALERT_FRACTION: Final = Decimal("0.8")
# Provisional v1 quotas seeded by apps/subscriptions/migrations/_subscription_sql.py.
SEEDED_QUOTAS: Final = {
    "ai_tokens_in": 10_000_000,
    "ai_tokens_out": 10_000_000,
    "ai_tokens_cached": 10_000_000,
    "ai_audio_seconds": 360_000,
    "video_seconds": 360_000,
    "messages": 10_000,
    "storage_bytes": 10_737_418_240,
}

Values = dict[str, Decimal | None]


class EconomicsInputError(ValueError):
    """A variable is unlabelled, malformed or negative."""


def _fail(message: str) -> Never:
    raise EconomicsInputError(message)


def _number(name: str, raw: object) -> Decimal | None:
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int | float | str):
        _fail(f"{name}: value must be a number or null")
    value = Decimal(str(raw))
    if not value.is_finite() or value < 0:
        _fail(f"{name}: value must be a finite non-negative number")
    return value


def validate(document: object) -> dict[str, dict[str, Decimal | None]]:
    """Return per-scenario values after checking every label and input."""
    if not isinstance(document, dict) or not isinstance(
        document.get("variables"), dict
    ):
        _fail("variables file must hold a 'variables' mapping")
    resolved: dict[str, dict[str, Decimal | None]] = {}
    for name, spec in document["variables"].items():
        if not isinstance(spec, dict):
            _fail(f"{name}: spec must be a mapping")
        if not str(spec.get("source") or "").strip():
            _fail(f"{name}: missing source label")
        if not spec.get("date") and spec.get("assumption") is not True:
            _fail(f"{name}: needs a source date or 'assumption: true'")
        if not str(spec.get("unit") or "").strip():
            _fail(f"{name}: missing unit")
        raw = spec.get("value")
        if raw is None and spec.get("owner_input_required") is not True:
            _fail(f"{name}: null value must be marked owner_input_required")
        if isinstance(raw, dict):
            if set(raw) != set(SCENARIOS):
                _fail(f"{name}: scenario mapping needs exactly {SCENARIOS}")
            resolved[name] = {s: _number(name, raw[s]) for s in SCENARIOS}
        else:
            value = _number(name, raw)
            resolved[name] = dict.fromkeys(SCENARIOS, value)
    return resolved


def _mul(*factors: Decimal | None) -> Decimal | None:
    total = Decimal(1)
    for factor in factors:
        if factor is None:
            return None
        total *= factor
    return total


def _add(*terms: Decimal | None) -> Decimal | None:
    if any(term is None for term in terms):
        return None
    return sum((term for term in terms if term is not None), Decimal(0))


def _required(v: Values, name: str) -> Decimal:
    value = v[name]
    if value is None:
        _fail(f"{name}: usage input cannot be null")
    return value


def scenario(v: Values) -> dict[str, Any]:
    """Compute usage, per-clinic COGS and the P-100 total for one scenario."""
    clinics = _required(v, "clinics")
    if clinics == 0:
        _fail("clinics: must be positive")
    days = _required(v, "working_days_per_month")
    retry = 1 + _required(v, "retry_overhead")
    visits_day = (
        clinics
        * _required(v, "clinicians_per_clinic")
        * _required(v, "visits_per_clinician_day")
    )
    captured_day = visits_day * _required(v, "capture_share")
    reference_audio_day = captured_day * _required(v, "capture_minutes_per_visit")
    mix = (
        1
        + _required(v, "long_consult_share") * (_required(v, "long_consult_factor") - 1)
        + _required(v, "caregiver_share") * (_required(v, "caregiver_factor") - 1)
    )
    audio_day = reference_audio_day * mix
    billable_audio_day = audio_day * retry
    visits_cm = visits_day * days / clinics
    captured_cm = captured_day * days / clinics
    audio_cm = billable_audio_day * days / clinics
    video_min_cm = (
        visits_cm
        * _required(v, "teleconsult_share")
        * _required(v, "capture_minutes_per_visit")
        * _required(v, "video_participants")
    )
    messages_cm = visits_cm * _required(v, "messages_per_visit")
    storage_gb_cm = (
        visits_cm
        * _required(v, "attachments_per_visit")
        * _required(v, "attachment_gb")
    )
    tokens = {
        kind: _mul(captured_cm, retry, v[f"llm_{kind}_tokens_per_visit"])
        for kind in ("input", "output", "cached")
    }
    llm = _add(
        *(
            _mul(tokens[kind], v[f"p_llm_{kind}"], 1 / MILLION)
            for kind in ("input", "output", "cached")
        )
    )
    components: dict[str, Decimal | None] = {
        "fixed": v["fixed_per_clinic_month"],
        "asr": _mul(audio_cm, v["p_asr"]),
        "llm": llm,
        "video": _mul(video_min_cm, v["p_video"]),
        "messages": _mul(messages_cm, v["p_message"]),
        "storage": _mul(storage_gb_cm, v["p_storage"]),
        "fees": _mul(visits_cm, v["fees_per_visit"]),
    }
    per_clinic = _add(*components.values())
    return {
        "usage": {
            "reference_audio_minutes_per_day": reference_audio_day,
            "audio_minutes_per_day": audio_day,
            "billable_audio_minutes_per_day": billable_audio_day,
            "provisioned_audio_minutes_per_day": audio_day
            * _required(v, "burst_factor"),
            "per_clinic_month": {
                "visits": visits_cm,
                "captured_visits": captured_cm,
                "billable_audio_minutes": audio_cm,
                "llm_tokens": tokens,
                "video_participant_minutes": video_min_cm,
                "messages": messages_cm,
                "storage_gb_added": storage_gb_cm,
            },
        },
        "cogs_per_clinic_month": {"components": components, "total": per_clinic},
        "cogs_profile_month": _mul(per_clinic, clinics),
    }


def _ceil(value: Decimal | None) -> int | None:
    return None if value is None else math.ceil(value)


def guardrails(v: Values) -> dict[str, object]:
    """Quota proposals per clinic-month from the high scenario, alert at 80%."""
    usage = scenario(v)["usage"]
    cm = usage["per_clinic_month"]
    tokens = cm["llm_tokens"]
    proposed: dict[str, int | None] = {
        "ai_tokens_in": _ceil(tokens["input"]),
        "ai_tokens_out": _ceil(tokens["output"]),
        "ai_tokens_cached": _ceil(tokens["cached"]),
        "ai_audio_seconds": _ceil(cm["billable_audio_minutes"] * 60),
        "video_seconds": _ceil(cm["video_participant_minutes"] * 60),
        "messages": _ceil(cm["messages"]),
        "storage_bytes": _ceil(cm["storage_gb_added"] * BYTES_PER_GB),
    }
    return {
        "basis": "high scenario per clinic-month; proposal only, owner approves",
        "alert_fraction": ALERT_FRACTION,
        "on_exhaustion": "AI unavailable; manual workflow stays available",
        "meters": {
            meter: {
                "proposed_quota": quota,
                "alert_at": _ceil(None if quota is None else quota * ALERT_FRACTION),
                "seeded_provisional_quota": SEEDED_QUOTAS[meter],
            }
            for meter, quota in proposed.items()
        },
    }


def run(document: object) -> dict[str, Any]:
    """Validate the variables document and return the full model output."""
    resolved = validate(document)
    results = {
        s: scenario({name: values[s] for name, values in resolved.items()})
        for s in SCENARIOS
    }
    missing = sorted(
        name for name, values in resolved.items() if values["expected"] is None
    )
    return {
        "schema_version": 1,
        "currency": "BRL",
        "data_class": "model; every price is owner-input-required until dated",
        "owner_input_required": missing,
        "scenarios": results,
        "guardrails": guardrails(
            {name: values["high"] for name, values in resolved.items()}
        ),
    }


def _json_default(value: object) -> str:
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    raise TypeError(type(value).__name__)


def to_json(output: dict[str, Any]) -> str:
    """Serialize with exact decimal strings."""
    return json.dumps(output, default=_json_default, indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    """Write the model output for the given variables file."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variables", type=Path, default=VARIABLES_PATH)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    document = yaml.safe_load(args.variables.read_text(encoding="utf-8"))
    args.output.write_text(to_json(run(document)), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

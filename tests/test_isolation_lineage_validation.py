from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest
from ops.testing.isolation_common import (
    IsolationError,
    JsonObject,
    JsonValue,
    canonical_bytes,
    raw_sha256,
    write_no_replace,
)
from ops.testing.isolation_lineage import (
    first_lineage_seed_record,
    load_complete_lineage,
)

if TYPE_CHECKING:
    from pathlib import Path


ATTEMPT_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def _primary_sources() -> list[JsonValue]:
    return [
        {
            "attempt_id": ATTEMPT_ID,
            "bundle_path": None,
            "primary_commit_sha": f"{todo:040x}",
            "relative_path": (
                f"todo-evidence/task-{todo}-clinic-os-phase-1a-staff-scheduling.json"
            ),
            "sha256": f"{todo:064x}",
            "todo": todo,
        }
        for todo in range(1, 21)
    ]


def _write_lineage_chain(attempt_root: Path, mutation: str | None = None) -> None:
    seed = first_lineage_seed_record(ATTEMPT_ID)
    seed_raw = canonical_bytes(seed)
    validation: JsonObject = {
        "current_attempt_id": ATTEMPT_ID,
        "fix_sources": [],
        "next_fix_sequence": 1,
        "primary_sources": _primary_sources(),
        "schema_version": 1,
        "seed_sha256": raw_sha256(seed_raw),
    }
    if mutation is not None:
        _mutate_validation(validation, mutation)
    validation_raw = canonical_bytes(validation)
    lineage = cast("JsonObject", dict(validation))
    lineage["lineage_validation_sha256"] = raw_sha256(validation_raw)
    write_no_replace(attempt_root / "receipt-lineage-seed.json", seed_raw, mode=0o400)
    write_no_replace(
        attempt_root / "receipt-lineage-validation.json",
        validation_raw,
        mode=0o400,
    )
    write_no_replace(
        attempt_root / "receipt-lineage.json",
        canonical_bytes(lineage),
        mode=0o400,
    )


def _mutate_validation(validation: JsonObject, mutation: str) -> None:
    primaries = cast("list[JsonObject]", validation["primary_sources"])
    if mutation == "nineteen":
        primaries.pop()
    elif mutation == "twenty-one":
        primaries.append(
            {
                "attempt_id": ATTEMPT_ID,
                "bundle_path": None,
                "primary_commit_sha": f"{21:040x}",
                "relative_path": (
                    "todo-evidence/task-21-clinic-os-phase-1a-staff-scheduling.json"
                ),
                "sha256": f"{21:064x}",
                "todo": 21,
            }
        )
    elif mutation == "duplicate":
        primaries[-1]["todo"] = 19
    elif mutation == "gap":
        primaries[9]["todo"] = 11
    elif mutation == "wrong-keys":
        primaries[0]["unexpected"] = True
    elif mutation == "fix-gap":
        validation["fix_sources"] = [
            {
                "attempt_id": ATTEMPT_ID,
                "bundle_path": None,
                "fix_commit_sha": "b" * 40,
                "relative_path": "todo-evidence/review-fix-2.json",
                "sequence": 2,
                "sha256": "c" * 64,
            }
        ]
        validation["next_fix_sequence"] = 3
    elif mutation == "foreign-attempt":
        primaries[0]["attempt_id"] = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        primaries[0]["bundle_path"] = "archived-attempt"
    elif mutation == "seed-hash":
        validation["seed_sha256"] = "d" * 64
    else:
        message = f"unknown mutation: {mutation}"
        raise AssertionError(message)


def test_first_attempt_validation_introduces_twenty_primary_sources(
    tmp_path: Path,
) -> None:
    _write_lineage_chain(tmp_path)

    hashes = load_complete_lineage(tmp_path, ATTEMPT_ID)

    assert hashes.seed_sha256
    assert hashes.validation_sha256
    assert hashes.lineage_sha256


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("nineteen", "exactly todos 1 through 20"),
        ("twenty-one", "exactly todos 1 through 20"),
        ("duplicate", "exactly todos 1 through 20"),
        ("gap", "exactly todos 1 through 20"),
        ("wrong-keys", "open or unknown root"),
        ("fix-gap", "not globally contiguous"),
        ("foreign-attempt", "invented a foreign receipt source"),
        ("seed-hash", "differs from its seed"),
    ],
)
def test_first_attempt_validation_rejects_malformed_sources(
    tmp_path: Path,
    mutation: str,
    message: str,
) -> None:
    _write_lineage_chain(tmp_path, mutation)

    with pytest.raises(IsolationError, match=message):
        load_complete_lineage(tmp_path, ATTEMPT_ID)

"""Publish and validate immutable cross-attempt receipt-lineage records."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Never, cast

from ops.testing.isolation_common import (
    MODE_IMMUTABLE,
    IsolationError,
    JsonObject,
    JsonValue,
    canonical_bytes,
    load_json,
    raw_sha256,
    regular_identity,
    write_no_replace,
)

if TYPE_CHECKING:
    from pathlib import Path

SEED_KEYS: Final = {
    "schema_version",
    "current_attempt_id",
    "previous_attempt_id",
    "previous_bundle_path",
    "previous_tombstone_sha256",
    "previous_lineage_sha256",
    "primary_sources",
    "fix_sources",
    "next_fix_sequence",
}
VALIDATION_KEYS: Final = {
    "schema_version",
    "current_attempt_id",
    "seed_sha256",
    "primary_sources",
    "fix_sources",
    "next_fix_sequence",
}
LINEAGE_KEYS: Final = VALIDATION_KEYS | {"lineage_validation_sha256"}


@dataclass(frozen=True, slots=True)
class LineageHashes:
    """Authenticated hashes for the seed, checkpoint, and final lineage."""

    seed_sha256: str
    validation_sha256: str
    lineage_sha256: str


@dataclass(frozen=True, slots=True)
class SuccessorLineageSeedInputs:
    """Authenticated predecessor fields projected into a successor seed."""

    prior_attempt_root: Path
    prior_attempt_id: str
    current_attempt_id: str
    bundle_path: str
    tombstone_sha256: str
    lineage_sha256: str


def publish_first_lineage_seed(attempt_root: Path, attempt_id: str) -> None:
    """Create the no-predecessor seed before any attempt receipt publication."""
    write_no_replace(
        attempt_root / "receipt-lineage-seed.json",
        canonical_bytes(first_lineage_seed_record(attempt_id)),
        mode=MODE_IMMUTABLE,
    )


def first_lineage_seed_record(attempt_id: str) -> JsonObject:
    """Build the immutable no-predecessor receipt-lineage seed."""
    return {
        "current_attempt_id": attempt_id,
        "fix_sources": [],
        "next_fix_sequence": 1,
        "previous_attempt_id": None,
        "previous_bundle_path": None,
        "previous_lineage_sha256": None,
        "previous_tombstone_sha256": None,
        "primary_sources": [],
        "schema_version": 1,
    }


def successor_lineage_seed(
    inputs: SuccessorLineageSeedInputs,
) -> JsonObject:
    """Translate current-attempt source locators into immutable bundle locators."""
    load_complete_lineage(inputs.prior_attempt_root, inputs.prior_attempt_id)
    lineage, _raw = _load(inputs.prior_attempt_root / "receipt-lineage.json")
    primary = _translate_sources(
        lineage.get("primary_sources"),
        inputs.prior_attempt_id,
        inputs.bundle_path,
        kind="primary",
    )
    fixes = _translate_sources(
        lineage.get("fix_sources"),
        inputs.prior_attempt_id,
        inputs.bundle_path,
        kind="fix",
    )
    return {
        "current_attempt_id": inputs.current_attempt_id,
        "fix_sources": fixes,
        "next_fix_sequence": lineage["next_fix_sequence"],
        "previous_attempt_id": inputs.prior_attempt_id,
        "previous_bundle_path": inputs.bundle_path,
        "previous_lineage_sha256": inputs.lineage_sha256,
        "previous_tombstone_sha256": inputs.tombstone_sha256,
        "primary_sources": primary,
        "schema_version": 1,
    }


def load_complete_lineage(attempt_root: Path, attempt_id: str) -> LineageHashes:
    """Validate the seed/checkpoint/final chain at one logical attempt root."""
    seed, seed_raw = _load(attempt_root / "receipt-lineage-seed.json")
    if set(seed) != SEED_KEYS or seed.get("schema_version") != 1:
        _fail("receipt lineage seed has an open or unknown root")
    _require_common(seed, attempt_id)
    seed_sha = raw_sha256(seed_raw)

    validation, validation_raw = _load(attempt_root / "receipt-lineage-validation.json")
    if set(validation) != VALIDATION_KEYS or validation.get("schema_version") != 1:
        _fail("receipt lineage validation has an open or unknown root")
    if validation.get("seed_sha256") != seed_sha:
        _fail("receipt lineage validation differs from its seed")
    _require_common_equality(seed, validation, attempt_id)
    validation_sha = raw_sha256(validation_raw)

    lineage, lineage_raw = _load(attempt_root / "receipt-lineage.json")
    if set(lineage) != LINEAGE_KEYS or lineage.get("schema_version") != 1:
        _fail("final receipt lineage has an open or unknown root")
    if lineage.get("lineage_validation_sha256") != validation_sha:
        _fail("final receipt lineage differs from its validation checkpoint")
    if lineage.get("seed_sha256") != seed_sha:
        _fail("final receipt lineage differs from its seed")
    _require_common_equality(validation, lineage, attempt_id)
    return LineageHashes(seed_sha, validation_sha, raw_sha256(lineage_raw))


def _load(path: Path) -> tuple[JsonObject, bytes]:
    try:
        regular_identity(path, mode=MODE_IMMUTABLE)
        return load_json(path)
    except FileNotFoundError as error:
        message = f"required receipt lineage file is absent: {path.name}"
        raise IsolationError(message) from error


def _require_common(record: JsonObject, attempt_id: str) -> None:
    if record.get("current_attempt_id") != attempt_id:
        _fail("receipt lineage belongs to another attempt")
    for key in ("primary_sources", "fix_sources"):
        if not isinstance(record.get(key), list):
            _fail("receipt lineage sources must be arrays")
    sequence = record.get("next_fix_sequence")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
        _fail("receipt lineage next fix sequence is invalid")


def _require_common_equality(
    predecessor: JsonObject,
    current: JsonObject,
    attempt_id: str,
) -> None:
    _require_common(current, attempt_id)
    for key in (
        "current_attempt_id",
        "primary_sources",
        "fix_sources",
        "next_fix_sequence",
    ):
        if current.get(key) != predecessor.get(key):
            _fail("receipt lineage records disagree")


def _translate_sources(
    value: object,
    prior_attempt_id: str,
    bundle_path: str,
    *,
    kind: str,
) -> list[JsonValue]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        _fail("receipt lineage sources must be object arrays")
    expected = (
        {
            "attempt_id",
            "bundle_path",
            "primary_commit_sha",
            "relative_path",
            "sha256",
            "todo",
        }
        if kind == "primary"
        else {
            "attempt_id",
            "bundle_path",
            "fix_commit_sha",
            "relative_path",
            "sequence",
            "sha256",
        }
    )
    translated: list[JsonValue] = []
    for source in cast("list[JsonObject]", value):
        if set(source) != expected:
            _fail("receipt lineage source has an open or unknown root")
        result = dict(source)
        source_attempt = source.get("attempt_id")
        locator = source.get("bundle_path")
        if source_attempt == prior_attempt_id and locator is None:
            result["bundle_path"] = bundle_path
        elif not isinstance(source_attempt, str) or not isinstance(locator, str):
            _fail("receipt lineage source has an invalid logical locator")
        translated.append(result)
    return translated


def _fail(message: str) -> Never:
    raise IsolationError(message)

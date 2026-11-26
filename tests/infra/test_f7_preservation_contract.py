import hashlib
import json
from pathlib import Path

import pytest
from ops.testing.f7_projection_bytes import (
    BinaryProjectionMismatchError,
    Cells,
    verify_projection,
)
from ops.testing.migration_upgrade_wave_h_floor import Snapshot, records_preserved

from infra import wave_h_entry_differential


def test_exact_receipt_requires_original_keys_columns_values_and_count() -> None:
    row = {"id": "first", "actor_id": "a", "payload": {"n": 1}}
    digest = hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest()
    expected: Snapshot = {"columns": list(row), "rows": {"first": digest}, "count": 1}
    assert records_preserved(expected, [("first", row)])
    assert not records_preserved(expected, [])
    assert not records_preserved(expected, [("second", row)])
    assert not records_preserved(expected, [("first", row), ("first", row)])
    assert not records_preserved(expected, [("first", {"id": "first"})])
    assert not records_preserved(expected, [("first", {**row, "actor_id": "b"})])


@pytest.mark.parametrize("spelling", ["01", "1 ", "1.0"])
def test_text_projection_does_not_normalize_spelling(spelling: str) -> None:
    row = {"id": "1"}
    digest = hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest()
    expected: Snapshot = {"columns": ["id"], "rows": {"1": digest}, "count": 1}
    assert not records_preserved(expected, [("1", {"id": spelling})])


@pytest.mark.parametrize(
    ("baseline", "changed"),
    [
        (bytes(16), bytes(15) + b"\x01"),
        (bytes(8), bytes(7) + b"\x01"),
        (b'\x01{"n": 1}', b'\x01{"n": 2}'),
        (b"\x00\x80", b"\x00\x81"),
        (b"actor", b"Actor"),
    ],
    ids=["uuid", "timestamp-or-bigint", "jsonb", "bytea", "text"],
)
def test_binary_projection_preserves_each_payload_byte(
    baseline: bytes, changed: bytes
) -> None:
    expected: Cells = {"synthetic": (baseline,)}
    verify_projection("comms_message", expected, expected)
    with pytest.raises(BinaryProjectionMismatchError, match="byte_preserved:"):
        verify_projection("comms_message", expected, {"synthetic": (changed,)})


def test_clone_receipts_bind_only_the_checks_currently_owned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        wave_h_entry_differential,
        "_database_identity",
        lambda dsn: [dsn, 100 if dsn == "source" else 101],
    )
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    source = {"UPGRADE_ADMIN_DATABASE_URL": "source"}
    payloads: dict[str, dict[str, object]] = {
        "UPGRADE_SEED_FILE": {"database": ["source", 100], "seeds": ["inbox"]},
        "UPGRADE_BASE_OBSERVATION": {"database": ["source", 100], "base_sha": "base"},
    }
    for variable, data in payloads.items():
        path = source_dir / variable
        path.write_text(json.dumps(data), encoding="utf-8")
        source[variable] = str(path)
    clone = {"UPGRADE_ADMIN_DATABASE_URL": "restored"}
    receipts = wave_h_entry_differential._lane_clone_receipts(source, clone, tmp_path)
    assert receipts.keys() == payloads.keys()
    for variable, expected in payloads.items():
        assert json.loads(Path(source[variable]).read_text()) == expected
        assert json.loads(Path(receipts[variable]).read_text()) == {
            **expected,
            "database": ["restored", 101],
        }

    bad_observation = Path(source["UPGRADE_BASE_OBSERVATION"])
    bad_observation.write_text('{"database":["foreign",100]}', encoding="utf-8")
    refused = tmp_path / "refused"
    refused.mkdir()
    with pytest.raises(RuntimeError, match="actor_fault_source_identity_mismatch"):
        wave_h_entry_differential._lane_clone_receipts(source, clone, refused)
    assert not list(refused.iterdir())

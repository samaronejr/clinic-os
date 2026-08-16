from __future__ import annotations

from typing import TYPE_CHECKING, Final, cast

import pytest
from ops.testing.isolation_common import IsolationError, JsonObject
from ops.testing.shared_evidence_baseline import capture_manifest, verify_manifest

if TYPE_CHECKING:
    from pathlib import Path

ATTEMPT_ID: Final = "12345678-1234-4123-8123-123456789abc"
PRIOR_ATTEMPT_ID: Final = "22345678-1234-4123-8123-123456789abc"


def test_manifest_excludes_only_the_current_phase1a_destinations(
    tmp_path: Path,
) -> None:
    # Given: populated prior history alongside every exact current-attempt control path.
    evidence_root = tmp_path / "evidence"
    _populate_history(evidence_root)
    _populate_current_destinations(evidence_root)

    # When: the current attempt captures its shared-evidence baseline.
    manifest = capture_manifest(evidence_root, attempt_id=ATTEMPT_ID)
    paths = _relative_paths(manifest)

    # Then: prior history and the archive sentinel remain protected.
    assert "clinic-os-phase1a-runtime" in paths
    assert (
        f"clinic-os-phase1a-runtime/{PRIOR_ATTEMPT_ID}/todo-evidence/F1.json" in paths
    )
    assert "clinic-os-phase1a-rejected" in paths
    assert (
        f"clinic-os-phase1a-rejected/{PRIOR_ATTEMPT_ID}/{'a' * 40}/rejection.json"
        in paths
    )
    assert "isolation-archive-rollover-phase1a.sentinel" in paths
    assert "unrelated/nested/evidence.json" in paths
    assert not any(
        path == f"clinic-os-phase1a-runtime/{ATTEMPT_ID}"
        or path.startswith(f"clinic-os-phase1a-runtime/{ATTEMPT_ID}/")
        for path in paths
    )
    assert not any(
        path == f"clinic-os-phase1a-rejected/{ATTEMPT_ID}"
        or path.startswith(f"clinic-os-phase1a-rejected/{ATTEMPT_ID}/")
        for path in paths
    )
    assert not any(path.startswith("clinic-os-phase1a-final") for path in paths)


def test_manifest_verification_ignores_current_growth_but_detects_history_drift(
    tmp_path: Path,
) -> None:
    # Given: protected prior history and empty current runtime/rejection parents.
    evidence_root = tmp_path / "evidence"
    _populate_history(evidence_root)
    expected = capture_manifest(evidence_root, attempt_id=ATTEMPT_ID)

    # When: only exact current destinations grow after snapshot publication.
    _populate_current_destinations(evidence_root)

    # Then: current growth is ignored, but completed history remains immutable.
    verify_manifest(evidence_root, expected, attempt_id=ATTEMPT_ID)
    prior = (
        evidence_root
        / "clinic-os-phase1a-rejected"
        / PRIOR_ATTEMPT_ID
        / ("a" * 40)
        / "rejection.json"
    )
    prior.write_bytes(b"drifted\n")
    with pytest.raises(IsolationError, match="baseline drifted"):
        verify_manifest(evidence_root, expected, attempt_id=ATTEMPT_ID)


def _populate_history(evidence_root: Path) -> None:
    runtime = evidence_root / "clinic-os-phase1a-runtime"
    rejected = evidence_root / "clinic-os-phase1a-rejected"
    (runtime / PRIOR_ATTEMPT_ID / "todo-evidence").mkdir(parents=True)
    (runtime / PRIOR_ATTEMPT_ID / "todo-evidence" / "F1.json").write_bytes(b"{}\n")
    bundle = rejected / PRIOR_ATTEMPT_ID / ("a" * 40)
    bundle.mkdir(parents=True)
    (bundle / "rejection.json").write_bytes(b"{}\n")
    (evidence_root / "isolation-archive-rollover-phase1a.sentinel").write_bytes(
        b"sentinel\n"
    )
    unrelated = evidence_root / "unrelated" / "nested"
    unrelated.mkdir(parents=True)
    (unrelated / "evidence.json").write_bytes(b"{}\n")


def _populate_current_destinations(evidence_root: Path) -> None:
    current_runtime = evidence_root / "clinic-os-phase1a-runtime" / ATTEMPT_ID
    current_rejected = evidence_root / "clinic-os-phase1a-rejected" / ATTEMPT_ID
    current_runtime.mkdir(parents=True)
    current_rejected.mkdir(parents=True)
    (current_runtime / "owned.json").write_bytes(b"{}\n")
    (current_rejected / "future.json").write_bytes(b"{}\n")
    final = evidence_root / "clinic-os-phase1a-final"
    final.mkdir()
    (final / "F4-final.txt").write_bytes(b"APPROVE\n")
    review_inputs = evidence_root / "review-inputs"
    review_inputs.mkdir(exist_ok=True)
    (review_inputs / "approved-plan.md").write_bytes(b"approved\n")
    (review_inputs / "approved-plan.sha256").write_bytes(b"a" * 64 + b"\n")
    for name in (
        "isolation-ledger-phase1a.json",
        "isolation-ledger-phase1a.lock",
        "isolation-archive-rollover-phase1a.json",
        "isolation-ledger-final-phase1a.json",
    ):
        (evidence_root / name).write_bytes(b"{}\n")


def _relative_paths(manifest: JsonObject) -> set[str]:
    entries = cast("list[JsonObject]", manifest["entries"])
    return {cast("str", entry["relative_path"]) for entry in entries}

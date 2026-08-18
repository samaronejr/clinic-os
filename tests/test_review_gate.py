from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest
from ops.testing import final_review_lane_controller as final_controller
from ops.testing import isolation_barrier_process as barrier_process
from ops.testing import isolation_common as isolation
from ops.testing import isolation_terminal_publication as publication

from isolation_claim_fixtures import SECOND_CLAIM_ID
from isolation_terminal_publisher_fixtures import stale_publisher_ledger

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SHA = "a" * 40


def test_review_verdict_rejects_conditional_approval_prose() -> None:
    # Given: a verdict that adds conditional prose outside the one JSON object.
    module_path = PROJECT_ROOT / "ops/testing/validate_review_verdict.py"
    assert module_path.is_file()
    specification = importlib.util.spec_from_file_location(
        "review_verdict", module_path
    )
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    valid = {
        "findings": [],
        "lane": "F1",
        "schema_version": 1,
        "sha": SHA,
        "verdict": "APPROVE",
    }
    raw = json.dumps(valid, separators=(",", ":")).encode() + b"\nif tests pass"

    # When / Then: the public validator refuses every trailing byte.
    with pytest.raises(Exception, match="one JSON object"):
        module.validate_review_verdict(raw, "F1", SHA)


def test_review_child_has_no_claim_or_terminal_publication_capability() -> None:
    # Given: the child-only review shell invoked by the durable lane controller.
    script = PROJECT_ROOT / "ops/testing/run_review_gate.sh"
    assert script.is_file()
    source = script.read_text(encoding="utf-8")

    # When / Then: it disables instruction discovery and owns no outer lifecycle.
    for required in (
        "--ignore-user-config",
        "--ignore-rules",
        "project_doc_max_bytes=0",
        "--sandbox read-only",
        "approval_policy=never",
    ):
        assert required in source
    for forbidden in (
        " reserve ",
        " activate ",
        " release ",
        "trap ",
        "final-terminal",
    ):
        assert forbidden not in source


def test_controller_imports_staged_review_bytes_through_exact_authorization(
    tmp_path: Path,
) -> None:
    ledger_path, _ = stale_publisher_ledger(tmp_path, "active")
    ledger, _ = isolation.load_json(ledger_path)
    boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    ledger["boot_id"] = boot_id
    boot_observation = ledger["boot_observation"]
    assert isinstance(boot_observation, dict)
    boot_observation["boot_id"] = boot_id
    isolation.write_atomic_replace(ledger_path, isolation.canonical_bytes(ledger))
    terminal = (
        Path(str(ledger["attempt_root"])).parents[1]
        / "clinic-os-phase1a-final/terminal"
    )
    staged = tmp_path / "F1-verdict.json"
    raw = b'{"verdict":"APPROVE"}\n'
    isolation.write_no_replace(staged, raw, mode=isolation.MODE_IMMUTABLE)

    digest = publication.publish_terminal_file(
        ledger_path,
        SECOND_CLAIM_ID,
        "f1-verdict",
        staged,
        terminal,
    )

    assert (terminal / "F1-verdict.json").read_bytes() == raw
    assert digest == isolation.raw_sha256(raw)
    assert (
        publication.publish_terminal_file(
            ledger_path,
            SECOND_CLAIM_ID,
            "f1-verdict",
            staged,
            terminal,
        )
        == digest
    )


def test_barrier_child_cannot_execute_payload_before_parent_release(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "started"
    child = barrier_process.start_barrier_process(
        (
            sys.executable,
            "-c",
            f"from pathlib import Path; Path({str(marker)!r}).write_text('started')",
        ),
        dict(os.environ),
        tmp_path / "stdout",
        tmp_path / "stderr",
    )
    try:
        assert not marker.exists()
        child.release()
        result = child.wait(5)
    finally:
        child.close()
    assert result.exit_code == 0
    assert result.signal is None
    assert marker.read_text() == "started"


def test_final_review_cli_dispatches_only_the_fixed_f1_form(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[object] = []
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "final_review_lane_controller.py",
            "F1",
            "--sha",
            SHA,
            "--inputs",
            str(final_controller.EXPECTED_INPUTS),
        ],
    )

    def dispatch(request: object) -> int:
        observed.append(request)
        return 0

    monkeypatch.setattr(
        final_controller,
        "run_review_controller",
        dispatch,
    )

    assert final_controller.main() == 0
    assert len(observed) == 1
    request = observed[0]
    assert isinstance(request, final_controller.ReviewControllerRequest)
    assert request.lane == "F1"
    assert request.sha == SHA

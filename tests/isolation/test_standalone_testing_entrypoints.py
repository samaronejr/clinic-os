from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINTS = (
    "final_wave_controller.py",
    "final_review_lane_controller.py",
    "freeze_final_wave_inputs.py",
    "freeze_final_wave_outputs.py",
    "freeze_final_wave_final.py",
    "final_artifact_gate.py",
    "scope_gate.py",
    "validate_image_contract.py",
    "validate_review_verdict.py",
    "validate_f2_receipt.py",
)

# The primary receipt publisher is contract-immutable and hash-pinned, so it is
# intentionally excluded from this bare-script bootstrap regression.


@pytest.mark.parametrize("entrypoint", ENTRYPOINTS)
def test_bare_testing_entrypoints_bootstrap_without_pythonpath(entrypoint: str) -> None:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    read_fd, write_fd = os.pipe()
    process = os.posix_spawn(
        sys.executable,
        (sys.executable, f"ops/testing/{entrypoint}", "--help"),
        env,
        file_actions=(
            (os.POSIX_SPAWN_DUP2, write_fd, 1),
            (os.POSIX_SPAWN_DUP2, write_fd, 2),
            (os.POSIX_SPAWN_CLOSE, read_fd),
            (os.POSIX_SPAWN_CLOSE, write_fd),
        ),
    )
    os.close(write_fd)
    output = os.read(read_fd, 1_000_000).decode()
    os.close(read_fd)
    _, status = os.waitpid(process, 0)

    assert "ModuleNotFoundError" not in output
    assert "No module named 'ops'" not in output
    assert os.waitstatus_to_exitcode(status) in {0, 2}

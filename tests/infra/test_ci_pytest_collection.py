"""Real pytest collection changes must fail before shard deselection."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Literal, assert_never

import pytest
from ops.testing.ci_pytest_partition import partition
from ops.testing.process_helpers import run_process
from ops.testing.runtime_paths import runtime_directory

if TYPE_CHECKING:
    from collections.abc import Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ORIGINAL = "def test_parallel():\n    pass\n\ndef test_serial():\n    pass\n"


@pytest.mark.parametrize("phase", ["parallel", "serial"])
@pytest.mark.parametrize("shard", [1, 2])
@pytest.mark.parametrize("mutation", ["added", "missing", "unchanged"])
def test_execution_collection_preserves_only_unchanged_population(
    tmp_path: Path,
    phase: Literal["parallel", "serial"],
    shard: int,
    mutation: Literal["added", "missing", "unchanged"],
) -> None:
    # Given: a genuine independent collection and valid phase-specific inputs.
    with (
        runtime_directory(tmp_path, purpose="collection") as directory,
        pytest.MonkeyPatch.context() as environment,
    ):
        tests = directory / "tests"
        tests.mkdir()
        files = ("tests/test_a.py", "tests/test_b.py")
        for name in files:
            _ = (directory / name).write_text(ORIGINAL, encoding="utf-8")
        _ = (directory / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
        _ = (directory / "ci-serial-tests.txt").write_text(
            "".join(f"worker-environment {name}::test_serial\n" for name in files),
            encoding="utf-8",
        )
        _ = (directory / "collection_plant.py").write_text(
            """from pathlib import Path
from ops.testing import ci_pytest_plugin
entries = ci_pytest_plugin.load_manifest(
    Path(__file__).with_name("ci-serial-tests.txt")
)
ci_pytest_plugin.load_manifest = lambda: entries
""",
            encoding="utf-8",
        )
        record = directory / "records"
        record.mkdir(mode=0o700)
        environment.chdir(directory)
        environment.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
        environment.setenv("PYTHONDONTWRITEBYTECODE", "1")
        environment.setenv(
            "PYTHONPATH", os.pathsep.join((str(directory), str(PROJECT_ROOT)))
        )
        environment.delenv("PYTEST_ADDOPTS", raising=False)
        environment.delenv("PYTEST_PLUGINS", raising=False)
        common = (
            sys.executable,
            "-m",
            "pytest",
            "-c",
            str(directory / "pytest.ini"),
            "-p",
            "ops.testing.ci_pytest_plugin",
            "-p",
            "collection_plant",
            "-o",
            f"cache_dir={directory / 'cache'}",
            "-q",
            f"--ci-pytest-record={record}",
        )
        collected = run_process(
            (*common, "--ci-pytest-phase=collect", "--collect-only", "tests")
        )
        assert collected.returncode == 0, collected.stdout + collected.stderr
        reference = [
            f"{name}::{test}"
            for name in files
            for test in ("test_parallel", "test_serial")
        ]
        serial = {f"{name}::test_serial" for name in files}
        selection = partition(reference, serial)
        paths: Sequence[str]
        match phase:
            case "parallel":
                paths = ["tests"]
                changed = selection[3 - shard]["parallel"][0].split("::", 1)[0]
            case "serial":
                paths = sorted(
                    {node.split("::", 1)[0] for node in selection[shard]["serial"]}
                )
                changed = paths[0]
            case unreachable_phase:
                assert_never(unreachable_phase)
        match mutation:
            case "added":
                body = ORIGINAL + "\ndef test_added_after_reference():\n    pass\n"
                expected_exit = pytest.ExitCode.USAGE_ERROR
            case "missing":
                body = "def test_serial():\n    pass\n"
                expected_exit = pytest.ExitCode.USAGE_ERROR
            case "unchanged":
                body = ORIGINAL
                expected_exit = pytest.ExitCode.OK
            case unreachable_mutation:
                assert_never(unreachable_mutation)
        _ = (directory / changed).write_text(body, encoding="utf-8")
        # When: the real execution phase recollects after the source mutation.
        executed = run_process(
            (
                *common,
                f"--ci-pytest-phase={phase}",
                f"--ci-pytest-shard={shard}",
                *paths,
            )
        )
    # Then: additions/removals fail the job; unchanged restricted inputs run.
    assert executed.returncode == expected_exit, executed.stdout + executed.stderr

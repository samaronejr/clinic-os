from __future__ import annotations

import asyncio
import os
import shlex
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
PREFLIGHT = "uv run --locked python -m ops.testing.pidfd_preflight"
MISSING = "lacks required Linux pidfd APIs: os.pidfd_open"
RUN_MODULE = """
import os, runpy, sys
del os.pidfd_open
module = sys.argv.pop(1)
runpy.run_module(module, run_name="__main__", alter_sys=True)
"""
RUN_UV_COMMAND = """
import os, runpy, sys
del os.pidfd_open
assert sys.argv.pop(1) == "run"
while sys.argv[1].startswith("--"):
    sys.argv.pop(1)
if sys.argv[1] == "python":
    assert sys.argv[2] == "-m"
    module = sys.argv[3]
    del sys.argv[1:4]
else:
    module = sys.argv.pop(1)
runpy.run_module(module, run_name="__main__", alter_sys=True)
"""
SHA = "1" * 40


async def _run(*argv: str, cwd: Path, env: dict[str, str]) -> tuple[int | None, str]:
    process = await asyncio.create_subprocess_exec(
        *argv, cwd=cwd, env=env, stderr=asyncio.subprocess.PIPE
    )
    async with asyncio.timeout(60):
        _, stderr = await process.communicate()
    return process.returncode, stderr.decode()


@pytest.mark.parametrize(
    ("module", "arguments"),
    [
        ("ci_pytest", ("--partition-only",)),
        ("ci_image_contracts", ()),
        ("image_smoke_controller", ("build", "--sha", SHA)),
        ("tls_stack_controller", ("smoke",)),
        ("browser_runner_controller", ("probe",)),
        ("renewal_runner", ("ci",)),
        ("renewal_runner", ("browser", "--suite", "clinical-inbox")),
    ],
)
def test_python_gate_refuses_before_dispatch(
    tmp_path: Path, module: str, arguments: tuple[str, ...]
) -> None:
    work, artifacts = tmp_path / "work", tmp_path / "artifacts"
    if module == "ci_pytest":
        arguments += (
            "--workers",
            "1",
            "--work-root",
            str(work),
            "--artifact-root",
            str(artifacts),
        )
    elif module == "renewal_runner":
        arguments += ("--artifact-root", str(artifacts))
    status, stderr = asyncio.run(
        _run(
            sys.executable,
            "-c",
            RUN_MODULE,
            f"ops.testing.{module}",
            *arguments,
            cwd=tmp_path,
            env={**os.environ, "PYTHONPATH": str(ROOT)},
        )
    )
    assert status != 0
    assert MISSING in stderr
    assert not work.exists()
    assert not artifacts.exists()


@pytest.mark.parametrize(
    ("target", "flags", "diagnostic"),
    [
        ("ci-image-contracts", "", MISSING),
        ("ci-browser-contract", "-i", "do not support --ignore-errors"),
        ("ci-image-contracts", "--ignore-errors", "do not support --ignore-errors"),
    ],
)
def test_make_cannot_dispatch_after_pidfd_refusal(
    tmp_path: Path, target: str, flags: str, diagnostic: str
) -> None:
    _ = (tmp_path / "Makefile").write_bytes((ROOT / "Makefile").read_bytes())
    testing = tmp_path / "ops/testing"
    testing.mkdir(parents=True)
    manifest = "ci-required-browser-suites.txt"
    _ = (testing / manifest).write_bytes((ROOT / "ops/testing" / manifest).read_bytes())
    for stage in ("browser_runner.sh", "image_smoke.sh", "tls_stack.sh"):
        script = testing / stage
        _ = script.write_text("#!/bin/sh\n: > dispatched\n", encoding="utf-8")
        script.chmod(0o700)
    tests = tmp_path / "tests/isolation"
    tests.mkdir(parents=True)
    _ = (tests / "test_container_contract.py").write_text(
        "from pathlib import Path\n"
        "Path('collected').touch()\n"
        "def test_canary(): pass\n",
        encoding="utf-8",
    )
    driver = tmp_path / "uv-sentry.py"
    _ = driver.write_text(RUN_UV_COMMAND, encoding="utf-8")
    status, stderr = asyncio.run(
        _run(
            "/usr/bin/make",
            "--no-print-directory",
            target,
            "POSTGRES_CONTAINER=synthetic",
            f"UV={shlex.join((sys.executable, str(driver)))}",
            cwd=tmp_path,
            env={
                **os.environ,
                "MAKEFLAGS": flags,
                "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
                "PYTHONPATH": str(ROOT),
            },
        )
    )
    assert status != 0
    assert diagnostic in stderr
    assert not (tmp_path / "collected").exists()
    assert not (tmp_path / "dispatched").exists()


def test_workflow_and_make_gate_inventory_is_guarded() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    jobs = workflow["jobs"]
    assert set(jobs) == {
        "test",
        "python-coverage",
        "contracts",
        "renewal-browser",
        "worker-integration",
        "migration-upgrade",
        "renewal-acceptance",
        "actionlint",
    }
    for name, job in jobs.items():
        scripts = [step["run"] for step in job["steps"] if "run" in step]
        assert "isolation_ledger.py snapshot" in scripts[0], name
        assert scripts[1] == PREFLIGHT, name
    makefile = (ROOT / "Makefile").read_text()
    for target, drivers in {
        "ci": ("-m ops.testing.pidfd_preflight", "-m ops.testing.ci_pytest"),
        "ci-browser-contract": (
            "ops/testing/browser_runner.sh probe",
            "-m ops.testing.renewal_runner browser",
        ),
        "ci-image-contracts": (
            "-m ops.testing.ci_image_contracts",
            "ops/testing/image_smoke.sh build",
            "ops/testing/tls_stack.sh smoke",
        ),
    }.items():
        recipe = makefile.split(f"\n{target}:\n", 1)[1].split("\n\n", 1)[0]
        assert all(driver in recipe for driver in drivers), target
    for shell, module in {
        "browser_runner": "browser_runner_controller",
        "image_smoke": "image_smoke_controller",
        "tls_stack": "tls_stack_controller",
    }.items():
        script = (ROOT / f"ops/testing/{shell}.sh").read_text()
        assert f'"$python_bin" -m ops.testing.{module} ' in script

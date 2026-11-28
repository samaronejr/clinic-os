from __future__ import annotations

import asyncio
import os
import re
import shlex
import sys
from pathlib import Path

import pytest
from ops.testing.renewal_acceptance import _workflow_guard_failures

ROOT = Path(__file__).resolve().parents[2]
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


async def _run(
    *argv: str, cwd: Path, env: dict[str, str], stdin: str = ""
) -> tuple[int | None, str, str]:
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(60):
            stdout, stderr = await process.communicate(stdin.encode())
    except TimeoutError:
        process.kill()
        await process.wait()
        raise
    return process.returncode, stdout.decode(), stderr.decode()


@pytest.mark.parametrize(
    ("module", "arguments"),
    [
        ("ci_pytest", ("--partition-only",)),
        ("ci_image_contracts", ()),
        ("ci_hosted_gate", ("actionlint",)),
        ("ci_local", ("image",)),
        ("ci_local", ("browser",)),
        ("ci_local", ("quality", "--make", "/usr/bin/false", "--workers", "1")),
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
    elif module == "ci_hosted_gate":
        arguments += (str(artifacts),)
    status, _, stderr = asyncio.run(
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


def test_checked_quality_refuses_an_inherited_pidfdless_python(tmp_path: Path) -> None:
    incapable = tmp_path / "python"
    incapable.write_text(
        f"#!{sys.executable}\nimport os\ndel os.pidfd_open\n"
        "assert not hasattr(os, 'pidfd_open')\n"
    )
    incapable.chmod(0o700)
    make = tmp_path / "make"
    make.write_text("#!/bin/sh\n: > db-dispatched\n")
    make.chmod(0o700)
    status, _, stderr = asyncio.run(
        _run(
            sys.executable,
            "-m",
            "ops.testing.ci_local",
            "quality",
            "--make",
            str(make),
            "--workers",
            "1",
            cwd=tmp_path,
            env={**os.environ, "PYTHONPATH": str(ROOT), "UV_PYTHON": str(incapable)},
        )
    )
    assert status == 1
    assert "UV_PYTHON does not name the checked interpreter" in stderr
    assert not (tmp_path / "db-dispatched").exists()


@pytest.mark.parametrize(
    ("flags", "gnumakeflags"),
    [
        ("", ""),
        ("-i", ""),
        ("-k", ""),
        ("-ik", ""),
        ("", "-i"),
    ],
)
@pytest.mark.parametrize("target", ["ci", "ci-browser-contract", "ci-image-contracts"])
def test_make_cannot_dispatch_after_pidfd_refusal(
    tmp_path: Path, target: str, flags: str, gnumakeflags: str
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
    status, _, stderr = asyncio.run(
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
                "GNUMAKEFLAGS": gnumakeflags,
                "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
                "PYTHONPATH": str(ROOT),
            },
        )
    )
    if not gnumakeflags and flags in {"", "-k"}:
        assert status != 0
    assert MISSING in stderr
    assert not (tmp_path / "collected").exists()
    assert not (tmp_path / "dispatched").exists()


@pytest.mark.parametrize(
    ("target", "options", "expected"),
    [
        (target, (option,), code)
        for target in (
            "ci",
            "ci-browser-contract",
            "ci-image-contracts",
            "db-bootstrap",
        )
        for option, code in (("-n", 0), ("-t", 0), ("-q", 1))
    ]
    + [("ci", ("-n", "--eval=override MAKEFLAGS :="), 0)],
)
def test_make_dry_modes_start_no_process(
    tmp_path: Path, target: str, options: tuple[str, ...], expected: int
) -> None:
    _ = (tmp_path / "Makefile").write_bytes((ROOT / "Makefile").read_bytes())
    status, _, stderr = asyncio.run(
        _run(
            "/usr/bin/make",
            *options,
            target,
            "UV=touch uv-dispatched;",
            "COMPOSE=touch compose-dispatched;",
            cwd=tmp_path,
            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
        )
    )
    assert status == expected, stderr
    assert not (tmp_path / "uv-dispatched").exists()
    assert not (tmp_path / "compose-dispatched").exists()


MAKE_EXEMPTIONS = {
    "bootstrap-clinic": ("manage.py bootstrap_clinic", "clinic administration"),
    "provision-staff": ("manage.py provision_staff", "clinic administration"),
    "set-clinic-timezone": ("manage.py set_clinic_timezone", "clinic administration"),
    "revoke-staff-role": ("manage.py revoke_staff_role", "clinic administration"),
    "db-inputs": ("$(db_inputs)", "database input validation"),
    "db-bootstrap": ("ops/db/bootstrap.sql", "database lifecycle"),
    "db-posture": ("ops/db/posture.py", "database lifecycle"),
    "migrate": ("manage.py migrate", "database lifecycle"),
    "isolated-db-up": ("./ops/testing/isolated_db.sh up", "database lifecycle"),
    "isolated-db-status": ("./ops/testing/isolated_db.sh status", "database lifecycle"),
    "isolated-db-down": ("./ops/testing/isolated_db.sh down", "database lifecycle"),
    "restore-rehearsal": ("ops.testing.restore_rehearsal", "standalone backup restore"),
    "current-source-snapshot": ("ops.testing.current_source_snapshot", "evidence only"),
}
GATE_RECIPE = re.compile(
    r"\$\(UV\) run (?:--frozen --no-sync --no-env-file python -m "
    r"ops\.testing\.ci_local (?:browser|image)|--locked --all-groups python -m "
    r"ops\.testing\.ci_local quality "
    r'--workers "\$\(CI_PYTEST_WORKERS\)")'
)
WORKLOAD = re.compile(r"\bpytest\b|\bops(?:\.testing\.|/testing/)")


def _make_rules(makefile: Path) -> dict[str, tuple[set[str], str]]:
    # Query GNU Make's database through an empty synthetic goal, not a gate.
    sentinel = "__pidfd_inventory__"
    status, database, stderr = asyncio.run(
        _run(
            "/usr/bin/make",
            "-pRrq",
            "-f",
            str(makefile),
            "-f",
            "-",
            sentinel,
            "POSTGRES_CONTAINER=inventory",
            cwd=makefile.parent,
            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
            stdin=f"{sentinel}: ;\n",
        )
    )
    assert status in {0, 1}, stderr
    files = database.split("\n# Files\n", 1)[1].split("\n# files hash-table", 1)[0]
    rules: dict[str, tuple[set[str], str]] = {}
    for block in files.split("\n\n"):
        target = re.search(r"^([^\s:#]+):(?![^\n]*=)([^\n]*)$", block, re.MULTILINE)
        commands = [
            line.strip() for line in block.splitlines() if line.startswith("\t")
        ]
        if target and target[1] != sentinel:
            commands = [line.removesuffix("\\").rstrip() for line in commands]
            dependencies = set(target[2].split()) - {"|"}
            rules[target[1]] = dependencies, " ".join(commands)
    return rules


def _assert_make_guards(makefile: Path) -> None:
    rules = _make_rules(makefile)
    assert MAKE_EXEMPTIONS.keys() <= rules.keys(), "stale Make gate exemption"
    gates = set()
    for target, (dependencies, recipe) in rules.items():
        if not recipe:
            continue
        if GATE_RECIPE.fullmatch(recipe):
            assert not dependencies, f"Make gate has prerequisites: {target}"
            gates.add(target)
            continue
        assert target in MAKE_EXEMPTIONS, f"unguarded Make target: {target}"
        command, reason = MAKE_EXEMPTIONS[target]
        assert command in recipe, target
        assert not WORKLOAD.search(recipe.replace(command, "", 1)), (
            f"unguarded workload in {target} ({reason})"
        )
    assert gates == {"ci", "ci-image-contracts", "ci-browser-contract"}


def test_workflow_and_make_gate_inventory_is_guarded() -> None:
    _assert_make_guards(ROOT / "Makefile")
    assert not _workflow_guard_failures((ROOT / ".github/workflows/ci.yml").read_text())


def test_inventory_rejects_new_make_test_target(tmp_path: Path) -> None:
    makefile = tmp_path / "Makefile"
    _ = makefile.write_text(
        (ROOT / "Makefile").read_text()
        + "\n.PHONY: unguarded-test\nunguarded-test:\n\tpytest -q tests\n"
    )
    with pytest.raises(AssertionError, match="unguarded Make target: unguarded-test"):
        _assert_make_guards(makefile)


def test_inventory_rejects_make_gate_work_prerequisite(tmp_path: Path) -> None:
    makefile = tmp_path / "Makefile"
    _ = makefile.write_text(
        (ROOT / "Makefile").read_text() + "\nci-browser-contract: db-posture\n"
    )
    with pytest.raises(AssertionError, match="Make gate has prerequisites"):
        _assert_make_guards(makefile)


def test_inventory_accepts_recipe_less_make_alias(tmp_path: Path) -> None:
    makefile = tmp_path / "Makefile"
    _ = makefile.write_text(
        (ROOT / "Makefile").read_text()
        + "\n.PHONY: ci-via-prerequisite\nci-via-prerequisite: ci-browser-contract\n"
    )
    rules = _make_rules(makefile)
    assert rules["ci-via-prerequisite"] == ({"ci-browser-contract"}, "")
    _assert_make_guards(makefile)


def test_inventory_rejects_second_make_gate_command(tmp_path: Path) -> None:
    makefile = tmp_path / "Makefile"
    original = (ROOT / "Makefile").read_text()
    _ = makefile.write_text(
        original.replace(
            "ops.testing.ci_local browser\n",
            "ops.testing.ci_local browser\n\tpytest -q tests\n",
            1,
        )
    )
    with pytest.raises(
        AssertionError, match="unguarded Make target: ci-browser-contract"
    ):
        _assert_make_guards(makefile)


@pytest.mark.parametrize("forced", ["+", "$(MAKE); "])
def test_dry_run_detects_forced_recipe_mutation(tmp_path: Path, forced: str) -> None:
    makefile = tmp_path / "Makefile"
    command = "$(UV) run --locked --all-groups python -m ops.testing.ci_local quality"
    _ = makefile.write_text(
        (ROOT / "Makefile").read_text().replace(command, forced + command)
    )
    status, _, stderr = asyncio.run(
        _run(
            "/usr/bin/make",
            "-n",
            "ci",
            "MAKE=true",
            "UV=touch uv-dispatched;",
            cwd=tmp_path,
            env={"PATH": "/usr/bin:/bin"},
        )
    )
    assert (tmp_path / "uv-dispatched").exists(), (status, stderr)
    with pytest.raises(AssertionError, match="unguarded Make target: ci"):
        _assert_make_guards(makefile)

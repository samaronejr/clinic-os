from __future__ import annotations

import asyncio
import os
import re
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
                "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
                "PYTHONPATH": str(ROOT),
            },
        )
    )
    assert status != 0
    assert diagnostic in stderr
    assert not (tmp_path / "collected").exists()
    assert not (tmp_path / "dispatched").exists()


@pytest.mark.parametrize("target", ["ci", "ci-browser-contract", "ci-image-contracts"])
@pytest.mark.parametrize(
    ("makeflags", "mflags", "argv"),
    [
        ("-i -- MFLAGS=", "", ()),
        ("-i -- MAKECMDGOALS=", "", ()),
        ("--keep-going", "", ()),
        ("", "", ("-n",)),
        ("", "", ("-t",)),
        ("", "", ("-q",)),
        ("", "-i", ("-i", "MAKEFLAGS=")),
        ("", "", ("-i", "MAKEFLAGS=", "MFLAGS=")),
    ],
)
def test_make_rejects_unsafe_modes_before_any_dispatch(
    tmp_path: Path, target: str, makeflags: str, mflags: str, argv: tuple[str, ...]
) -> None:
    _ = (tmp_path / "Makefile").write_bytes((ROOT / "Makefile").read_bytes())
    status, _, stderr = asyncio.run(
        _run(
            "/usr/bin/make",
            *argv,
            target,
            "POSTGRES_CONTAINER=synthetic",
            "UV=touch uv-dispatched;",
            cwd=tmp_path,
            env={**os.environ, "MAKEFLAGS": makeflags, "MFLAGS": mflags},
        )
    )
    assert status == 2
    assert "CI gates do not support" in stderr
    assert not (tmp_path / "uv-dispatched").exists()


@pytest.mark.parametrize(
    ("flags", "argv"),
    [
        ("-i", ("ci-browser-contract",)),
        ("-n", (".DEFAULT_GOAL=./ci",)),
        ("-i", ("db-inputs", ".EXTRA_PREREQS=ci-browser-contract")),
        ("-n", ("ci-via-prerequisite",)),
        ("-i -- MFLAGS=", (".DEFAULT_GOAL=./ci-browser-contract",)),
        ("-i", ("recursive-ci",)),
    ],
)
def test_make_checks_modes_when_gate_is_reached_indirectly(
    tmp_path: Path, flags: str, argv: tuple[str, ...]
) -> None:
    makefile = tmp_path / "Makefile"
    _ = makefile.write_text(
        (ROOT / "Makefile").read_text() + "\n.PHONY: ci-via-prerequisite recursive-ci\n"
        "ci-via-prerequisite: ci-browser-contract\n"
        "recursive-ci:\n"
        "\t+$(MAKE) ci-browser-contract; code=$$?; printf 'child=%s\\n' $$code\n"
    )
    status, stdout, stderr = asyncio.run(
        _run(
            "/usr/bin/make",
            *argv,
            "POSTGRES_CONTAINER=synthetic",
            "UV=touch uv-dispatched;",
            cwd=tmp_path,
            env={**os.environ, "MAKEFLAGS": flags, "MAKECMDGOALS": "db-inputs"},
        )
    )
    if argv == ("recursive-ci",):
        # The external recursive caller can ignore the child's error; prove
        # that the child itself refused rather than relying on the outer exit.
        assert "child=2" in stdout.splitlines(), (stdout, stderr)
    else:
        assert status == 2, (stdout, stderr)
    assert "CI gates do not support" in stderr
    assert not (tmp_path / "uv-dispatched").exists()


MAKE_EXEMPTIONS = {
    "bootstrap-clinic": ("manage.py bootstrap_clinic", "clinic administration"),
    "provision-staff": ("manage.py provision_staff", "clinic administration"),
    "set-clinic-timezone": ("manage.py set_clinic_timezone", "clinic administration"),
    "revoke-staff-role": ("manage.py revoke_staff_role", "clinic administration"),
    "db-inputs": ("POSTGRES_DB", "database input validation"),
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
    r'ops\.testing\.ci_local quality --make "\$\(MAKE\)" '
    r'--workers "\$\(CI_PYTEST_WORKERS\)")'
)
WORKLOAD = re.compile(r"\bpytest\b|\bops(?:\.testing\.|/testing/)")
MAKE_GUARD = "+$(call require_strict_make)"


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
        target = re.search(r"^([^\s:#]+):([^\n]*)$", block, re.MULTILINE)
        commands = [
            line.strip() for line in block.splitlines() if line.startswith("\t")
        ]
        if target and target[1] != sentinel:
            commands = [line.removesuffix("\\").rstrip() for line in commands]
            dependencies = set(target[2].split()) - {"|"}
            rules[target[1]] = dependencies, " ".join(commands)
    return rules


def _prerequisite_closure(
    rules: dict[str, tuple[set[str], str]], target: str
) -> set[str]:
    seen: set[str] = set()
    pending = [target]
    while pending:
        current = pending.pop()
        if current not in seen:
            seen.add(current)
            pending.extend(rules.get(current, (set(), ""))[0])
    return seen


def _assert_make_guards(makefile: Path) -> None:
    rules = _make_rules(makefile)
    assert MAKE_EXEMPTIONS.keys() <= rules.keys(), "stale Make gate exemption"
    gates = set()
    for target, (_, recipe) in rules.items():
        if not recipe:
            continue
        if GATE_RECIPE.fullmatch(recipe.removeprefix(MAKE_GUARD)):
            assert recipe.startswith(MAKE_GUARD), f"missing Make guard: {target}"
            gates.add(target)
            continue
        assert target in MAKE_EXEMPTIONS, f"unguarded Make target: {target}"
        command, reason = MAKE_EXEMPTIONS[target]
        assert command in recipe, target
        assert not WORKLOAD.search(recipe.replace(command, "", 1)), (
            f"unguarded workload in {target} ({reason})"
        )
    assert gates, "no Make gate recipes found"
    for target in rules.keys() - {".PHONY", ".DEFAULT"}:
        if _prerequisite_closure(rules, target) & gates:
            _assert_make_scope(makefile, target)


def _assert_make_scope(makefile: Path, target: str) -> None:
    # All paths to the guarded recipe must refuse, including default and alias
    # paths. Use an inert uv command in case the rule no longer refuses.
    for goals in ((target,), (f".DEFAULT_GOAL={target}",)):
        status, _, stderr = asyncio.run(
            _run(
                "/usr/bin/make",
                "-n",
                "-f",
                str(makefile),
                *goals,
                "POSTGRES_CONTAINER=inventory",
                "UV=false",
                "MAKE=false",
                cwd=makefile.parent,
                env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
            )
        )
        assert status == 2, f"Make guard scope omits {target}: {goals}"
        assert "CI gates do not support" in stderr


def _assert_workflow_guards(workflow: Path) -> None:
    source = yaml.safe_load(workflow.read_text())
    assert "defaults" not in source
    for name, job in source["jobs"].items():
        assert "defaults" not in job, name
        assert "continue-on-error" not in job, name
        steps = job["steps"]
        index = next(i for i, step in enumerate(steps) if "run" in step)
        assert "isolation_ledger.py snapshot" in steps[index]["run"], name
        # Exact keys ensure the check is not conditional, ignored, or
        # dispatched through an alternate shell.
        assert steps[index + 1] == {
            "name": "Verify Python pidfd support",
            "run": PREFLIGHT,
        }, f"unguarded CI job: {name}"


def test_workflow_and_make_gate_inventory_is_guarded() -> None:
    _assert_make_guards(ROOT / "Makefile")
    _assert_workflow_guards(ROOT / ".github/workflows/ci.yml")


def test_inventory_rejects_new_make_test_target(tmp_path: Path) -> None:
    makefile = tmp_path / "Makefile"
    _ = makefile.write_text(
        (ROOT / "Makefile").read_text()
        + "\n.PHONY: unguarded-test\nunguarded-test:\n\tpytest -q tests\n"
    )
    with pytest.raises(AssertionError, match="unguarded Make target: unguarded-test"):
        _assert_make_guards(makefile)


def test_inventory_rejects_new_make_gate_missing_macro(tmp_path: Path) -> None:
    makefile = tmp_path / "Makefile"
    _ = makefile.write_text(
        (ROOT / "Makefile").read_text() + "\n.PHONY: unscoped-gate\nunscoped-gate:\n"
        "\t$(UV) run --frozen --no-sync --no-env-file python -m "
        "ops.testing.ci_local image\n"
    )
    with pytest.raises(AssertionError, match="missing Make guard: unscoped-gate"):
        _assert_make_guards(makefile)


def test_inventory_follows_recipe_less_make_target(tmp_path: Path) -> None:
    makefile = tmp_path / "Makefile"
    _ = makefile.write_text(
        (ROOT / "Makefile").read_text()
        + "\n.PHONY: ci-via-prerequisite\nci-via-prerequisite: ci-browser-contract\n"
    )
    rules = _make_rules(makefile)
    assert rules["ci-via-prerequisite"] == ({"ci-browser-contract"}, "")
    assert "ci-browser-contract" in _prerequisite_closure(rules, "ci-via-prerequisite")
    _assert_make_guards(makefile)


@pytest.mark.parametrize("conditional", [False, True])
def test_inventory_rejects_new_unguarded_ci_job(
    tmp_path: Path, *, conditional: bool
) -> None:
    workflow = tmp_path / "ci.yml"
    source = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    template = source["jobs"]["test"]
    snapshot, guard = [step for step in template["steps"] if "run" in step][:2]
    steps = [snapshot]
    if conditional:
        steps.append({**guard, "if": "${{ false }}"})
    steps.append({"run": "pytest -q tests"})
    source["jobs"]["unguarded-test"] = {"runs-on": "ubuntu-latest", "steps": steps}
    _ = workflow.write_text(yaml.safe_dump(source))
    with pytest.raises(AssertionError, match="unguarded CI job: unguarded-test"):
        _assert_workflow_guards(workflow)

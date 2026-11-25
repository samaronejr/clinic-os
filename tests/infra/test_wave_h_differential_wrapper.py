"""Integration contract for the opt-in, claimed-PostgreSQL Wave H wrapper."""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[2]
BASE = "61918080021ddcfe2912e6922677143d5704d96d"
FOUNDATION = "b39ae436590c8a9c00ad8fc93d619875d1822827"


requires_postgres = pytest.mark.skipif(
    os.environ.get("RUN_WAVE_H_DIFFERENTIAL_E2E") != "1",
    reason="requires a claimed PostgreSQL instance and locked base environments",
)


def _wrapper_env(
    base: str, tmp_path: Path, bin_dir: Path | None, run_id: str
) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("UPGRADE_WAVE_H_")
    }
    if base != BASE:
        env.pop("UPGRADE_REUSE_ENVIRONMENT", None)
    if bin_dir is not None:
        env["PATH"] = str(bin_dir)
    git = shutil.which("git")
    assert git is not None
    commit = subprocess.check_output(  # noqa: S603 - local fixed Git query
        [git, "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()
    return {
        **env,
        "GITHUB_WORKSPACE": str(ROOT),
        "RUNNER_TEMP": str(tmp_path),
        "UPGRADE_WAVE_H_DIFFERENTIAL": "1",
        "UPGRADE_RUN_ID": run_id,
        "UPGRADE_COMMIT_SHA": commit,
    }


def _nonce(output: str) -> list[str]:
    return re.findall(
        r"^migration-upgrade: run_nonce=([0-9a-f]{32})$", output, re.MULTILINE
    )


def _run_wrapper(
    base: str, tmp_path: Path, *, bin_dir: Path | None = None
) -> subprocess.CompletedProcess[str]:
    run_id = uuid4().hex
    env = _wrapper_env(base, tmp_path, bin_dir, run_id)
    result = subprocess.run(  # noqa: S603 - fixed repository harness and pinned base
        [str(ROOT / "ops/testing/migration_upgrade.sh"), base],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    route = tmp_path / "migration-upgrade" / "verdict.json"
    if route.is_file() and not any(
        f"FAIL / {reason}" in result.stdout
        for reason in ("verdict_destination_unwritable", "unsafe_destination")
    ):
        verdict = json.loads(route.read_text(encoding="utf-8"))
        assert verdict.get("run_id") == run_id, result.stdout[-6000:]
        assert verdict.get("commit_sha") == env["UPGRADE_COMMIT_SHA"]
        assert _nonce(result.stdout) == [verdict.get("run_nonce")]
        assert float(verdict["started_at"]) > 0
    return result


@requires_postgres
def test_wrapper_generates_native_probe_cases_and_publishes_verdict(
    tmp_path: Path,
) -> None:
    result = _run_wrapper(BASE, tmp_path)
    receipt = tmp_path / "migration-upgrade"
    verdict_path = receipt / "verdict.json"
    assert verdict_path.is_file(), result.stdout[-6000:]
    verdict = json.loads(verdict_path.read_text(encoding="utf-8"))
    assert result.returncode == 0, (verdict, result.stdout[-6000:])
    assert verdict["result"] == "PASS"
    assert verdict["base_sha"] == BASE
    assert verdict["database_recreated"] is False
    assert verdict["differential"] == {
        "result": "PASS",
        "projection_cases": 3844,
        "probe_window_cases": 118,
        "differences": [],
        "base_reject_lane_pass_pairs": [],
    }
    assert not (receipt / "base").exists()
    assert not list(tmp_path.glob("migration-upgrade-secrets.*"))


@requires_postgres
def test_wrapper_names_probe_case_generation_failure(tmp_path: Path) -> None:
    result = _run_wrapper(FOUNDATION, tmp_path)
    receipt = tmp_path / "migration-upgrade"
    verdict_path = receipt / "verdict.json"
    assert verdict_path.is_file(), result.stdout[-6000:]
    verdict = json.loads(verdict_path.read_text(encoding="utf-8"))
    assert result.returncode == 1
    assert verdict["result"] == "FAIL"
    assert verdict["reason"] == "candidate_probe_case_generation"
    assert verdict["differential"]["probe_window_cases"] == 0
    assert not (receipt / "base").exists()
    assert not list(tmp_path.glob("migration-upgrade-secrets.*"))


def _tools(tmp_path: Path, *, missing: str = "") -> Path:
    bin_dir = tmp_path / "tools"
    bin_dir.mkdir(parents=True)
    for name in (
        "bash",
        "git",
        "python3",
        "docker",
        "uv",
        "mkdir",
        "mv",
        "rm",
        "head",
        "od",
        "tr",
        "chmod",
        "cmp",
        "ln",
        "realpath",
        "flock",
        "stat",
        "rmdir",
        "find",
        "sort",
        "sha256sum",
        "grep",
        "env",
    ):
        if name == missing:
            continue
        source = shutil.which(name)
        if source is None:
            # These tests stop before invoking Docker/uv.
            assert name in {"docker", "uv"}
            source = "/bin/false"
        (bin_dir / name).symlink_to(source)
    return bin_dir


def _old_pass(tmp_path: Path) -> Path:
    receipt = tmp_path / "migration-upgrade"
    receipt.mkdir()
    (receipt / "verdict.json").write_text(
        '{"result":"PASS","run_id":"previous"}\n', encoding="utf-8"
    )
    return receipt


def test_wrapper_terminates_with_current_interrupted_verdict(tmp_path: Path) -> None:
    receipt = _old_pass(tmp_path)
    bin_dir = _tools(tmp_path, missing="git")
    git = shutil.which("git")
    assert git is not None
    shim = bin_dir / "git"
    shim.write_text(
        "#!/bin/bash\n"
        'if [[ "$3:$4" == worktree:add ]]; then kill -TERM "$PPID"; exit 0; fi\n'
        f'exec {git} "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(0o700)
    result = _run_wrapper(BASE, tmp_path, bin_dir=bin_dir)
    assert result.returncode == 143, result.stdout
    verdict = json.loads((receipt / "verdict.json").read_text(encoding="utf-8"))
    assert verdict["result"] == "FAIL"
    assert verdict["reason"] == "interrupted"
    assert verdict["detail"] == "TERM"
    assert not list(tmp_path.glob("migration-upgrade-secrets.*"))


@pytest.mark.parametrize(
    ("signal", "status"), [("TERM", 143), ("INT", 130), ("HUP", 129)]
)
def test_wrapper_interrupts_at_verdict_replacement(
    tmp_path: Path, signal: str, status: int
) -> None:
    receipt = _old_pass(tmp_path)
    bin_dir = _tools(tmp_path, missing="mv")
    mv = shutil.which("mv")
    assert mv is not None
    shim = bin_dir / "mv"
    shim.write_text(
        f'#!/bin/bash\n{mv} "$@" || exit $?\n'
        'if [[ "$*" == *".verdict-stale-"* || "$*" == *".pending"* ]]; then\n'
        f'  kill -{signal} "$PPID"\n'
        "fi\n",
        encoding="utf-8",
    )
    shim.chmod(0o700)
    result = _run_wrapper(BASE, tmp_path, bin_dir=bin_dir)
    assert result.returncode == status, result.stdout
    verdict = json.loads((receipt / "verdict.json").read_text(encoding="utf-8"))
    assert (verdict["result"], verdict["reason"], verdict["detail"]) == (
        "FAIL",
        "interrupted",
        signal,
    )


@pytest.mark.parametrize(
    ("signal", "status"), [("TERM", 143), ("INT", 130), ("HUP", 129)]
)
def test_wrapper_interrupts_on_lock_return_before_running(
    tmp_path: Path, signal: str, status: int
) -> None:
    receipt = _old_pass(tmp_path)
    bin_dir = _tools(tmp_path, missing="flock")
    flock = shutil.which("flock")
    assert flock is not None
    shim = bin_dir / "flock"
    shim.write_text(
        f'#!/bin/bash\n{flock} "$@" || exit $?\nkill -{signal} "$PPID"\n',
        encoding="utf-8",
    )
    shim.chmod(0o700)
    witness = tmp_path / "transitions"
    mv = shutil.which("mv")
    assert mv is not None
    (bin_dir / "mv").unlink()
    recorder = bin_dir / "mv"
    recorder.write_text(
        "#!/bin/bash\n"
        'IFS= read -r record < "${@: -2:1}" || exit 1\n'
        f'{mv} "$@" || exit $?\n'
        f'printf "%s\\n" "$record" >> {witness!s}\n',
        encoding="utf-8",
    )
    recorder.chmod(0o700)

    result = _run_wrapper(BASE, tmp_path, bin_dir=bin_dir)

    assert result.returncode == status, result.stdout
    verdict = json.loads((receipt / "verdict.json").read_text(encoding="utf-8"))
    transitions = [
        json.loads(line) for line in witness.read_text(encoding="utf-8").splitlines()
    ]
    assert [item["result"] for item in transitions] == ["RUNNING", "FAIL"]
    assert all(item["run_nonce"] == verdict["run_nonce"] for item in transitions)
    assert transitions[-1] == verdict
    assert (verdict["reason"], verdict["detail"]) == ("interrupted", signal)


def test_wrapper_preserves_replacement_at_vacated_pending_name(tmp_path: Path) -> None:
    receipt = _old_pass(tmp_path)
    bin_dir = _tools(tmp_path, missing="mv")
    mv = shutil.which("mv")
    assert mv is not None
    shim = bin_dir / "mv"
    shim.write_text(
        "#!/bin/bash\n"
        'source="${@: -2:1}"\n'
        'IFS= read -r record < "$source"\n'
        f'{mv} "$@" || exit $?\n'
        """if [[ "$record" == *'"result":"RUNNING"'* ]]; then
    (set -o noclobber; printf 'unowned replacement\\n' > "$source") || exit 1
fi
""",
        encoding="utf-8",
    )
    shim.chmod(0o700)

    result = _run_wrapper("", tmp_path, bin_dir=bin_dir)

    assert result.returncode == 1, result.stdout
    verdict = json.loads((receipt / "verdict.json").read_text(encoding="utf-8"))
    assert (verdict["result"], verdict["reason"]) == ("FAIL", "missing_base_sha")
    [replacement] = receipt.glob(".verdict-*.pending")
    assert replacement.read_bytes() == b"unowned replacement\n"


def test_wrapper_does_not_adopt_preplaced_private_key(tmp_path: Path) -> None:
    bin_dir = _tools(tmp_path, missing="git")
    git = shutil.which("git")
    assert git is not None
    shim = bin_dir / "git"
    shim.write_text(
        "#!/bin/bash\n"
        'if [[ "$3:$4" == worktree:add ]]; then\n'
        f'    {git} "$@" || exit $?\n'
        '    path="${6%/*}/tenant-kek.secret"\n'
        "    (set -o noclobber; printf 'unowned key marker\\n' > \"$path\") || exit 1\n"
        '    stat -c "%d:%i" "$path" > "$RUNNER_TEMP/marker-identity"\n'
        "    exit\n"
        "fi\n"
        f'exec {git} "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(0o700)
    for name in ("docker", "uv"):
        (bin_dir / name).unlink()
    (bin_dir / "docker").symlink_to("/bin/false")
    (bin_dir / "uv").symlink_to("/bin/true")

    result = _run_wrapper(BASE, tmp_path, bin_dir=bin_dir)

    assert result.returncode != 0, result.stdout
    [marker] = tmp_path.glob("migration-upgrade-secrets.*/tenant-kek.secret")
    identity = marker.stat()
    assert f"{identity.st_dev}:{identity.st_ino}\n" == (
        tmp_path / "marker-identity"
    ).read_text(encoding="utf-8")
    assert marker.read_bytes() == b"unowned key marker\n"


@pytest.mark.parametrize("collision", [False, True])
def test_child_output_receipt_controls_cleanup(tmp_path: Path, collision: bool) -> None:
    bin_dir = _tools(tmp_path, missing="git")
    git = shutil.which("git")
    assert git is not None
    shim = bin_dir / "git"
    shim.write_text(
        "#!/bin/bash\n"
        'if [[ "$3:$4" == worktree:add ]]; then\n'
        f'    {git} "$@" || exit $?\n'
        '    output="${6%/*}/ai-assertions.json"\n'
        + ('    printf "unowned\\n" > "$output"\n' if collision else "")
        + """    python3 - "$output" <<'PY'
import os
import runpy
import sys
from pathlib import Path
path = Path(sys.argv[1])
helper = os.environ.get("UPGRADE_OWNED_OUTPUT_HELPER")
if helper is None:
    path.write_text("child wrote\\n")
else:
    with runpy.run_path(helper)["open_owned"](path) as output:
        output.write("child wrote\\n")
    print("owned child output created")
PY
    exit 1
fi
"""
        + f'exec {git} "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(0o700)

    result = _run_wrapper(BASE, tmp_path, bin_dir=bin_dir)

    assert result.returncode == 1
    outputs = list(tmp_path.glob("migration-upgrade-secrets.*/ai-assertions.json"))
    if collision:
        assert "private_output_exists: ai-assertions.json" in result.stdout
        [output] = outputs
        assert output.read_bytes() == b"unowned\n"
    else:
        assert "owned child output created" in result.stdout
        assert not outputs


@pytest.mark.parametrize("alias_root", [False, True])
def test_wrapper_rejects_symlink_without_writing(
    tmp_path: Path, alias_root: bool
) -> None:
    actual = tmp_path / "actual"
    actual.mkdir()
    if alias_root:
        target = _old_pass(actual)
        caller = tmp_path / "alias"
        caller.symlink_to(actual, target_is_directory=True)
    else:
        target = _old_pass(actual)
        caller = tmp_path
        (caller / "migration-upgrade").symlink_to(target, target_is_directory=True)
    receipt = target / "verdict.json"
    before = (receipt.stat().st_ino, receipt.read_bytes())
    result = _run_wrapper(BASE, caller)
    assert result.returncode != 0
    assert "FAIL / unsafe_destination" in result.stdout
    assert (receipt.stat().st_ino, receipt.read_bytes()) == before
    assert list(target.iterdir()) == [receipt]


def test_concurrent_wrapper_with_same_ci_identity_cannot_certify(
    tmp_path: Path,
) -> None:
    receipt = _old_pass(tmp_path)
    bin_dir = _tools(tmp_path, missing="git")
    git = shutil.which("git")
    assert git is not None
    shim = bin_dir / "git"
    shim.write_text(
        "#!/bin/bash\n"
        'if [[ "$3:$4" == worktree:add ]]; then echo staging; read -r _; exit 1; fi\n'
        f'exec {git} "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(0o700)
    args = [str(ROOT / "ops/testing/migration_upgrade.sh"), BASE]
    env = _wrapper_env(BASE, tmp_path, bin_dir, "same-ci-attempt")
    with subprocess.Popen(  # noqa: S603 - fixed repository harness and pinned base
        args,
        cwd=ROOT,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    ) as first:
        assert first.stdout is not None
        try:
            lines = []
            for line in first.stdout:
                if line == "staging\n":
                    break
                lines.append(line)
            else:
                pytest.fail(f"first wrapper did not reach staging: {lines}")
            before = (receipt / "verdict.json").read_bytes()
            second = subprocess.run(  # noqa: S603 - same fixed repository harness
                args,
                cwd=ROOT,
                stdin=subprocess.DEVNULL,
                env=_wrapper_env(
                    BASE,
                    tmp_path,
                    _tools(tmp_path / "second", missing="uv"),
                    "same-ci-attempt",
                ),
                capture_output=True,
                text=True,
                check=False,
                timeout=15,
            )
            assert second.returncode == 75, second.stdout + second.stderr
            assert "FAIL / concurrent_run" in second.stderr
            assert (receipt / "verdict.json").read_bytes() == before
            first_nonce = _nonce("".join(lines))
            assert first_nonce == [json.loads(before)["run_nonce"]]
            assert not _nonce(second.stdout)
        finally:
            first.communicate(input="finish\n", timeout=15)


def test_wrapper_records_setup_failure_without_python(tmp_path: Path) -> None:
    receipt = _old_pass(tmp_path)
    (receipt / "seed.pending").mkdir()
    result = _run_wrapper(BASE, tmp_path, bin_dir=_tools(tmp_path, missing="python3"))
    assert result.returncode == 127, result.stdout
    verdict = json.loads((receipt / "verdict.json").read_text(encoding="utf-8"))
    assert verdict["result"] == "FAIL"
    assert verdict["reason"] == "setup"
    assert verdict["detail"] == "missing_python3"


@pytest.mark.skipif(os.geteuid() == 0, reason="requires actual write denial")
def test_unwritable_destination_never_certifies_previous_run(tmp_path: Path) -> None:
    receipt = _old_pass(tmp_path)
    receipt.chmod(0o500)
    try:
        result = _run_wrapper(BASE, tmp_path)
        assert result.returncode != 0
        assert "verdict_destination_unwritable" in result.stdout
        assert "== check out base" not in result.stdout
        verdict = json.loads((receipt / "verdict.json").read_text(encoding="utf-8"))
        assert verdict["run_id"] == "previous"
    finally:
        receipt.chmod(0o700)


def test_wrapper_preserves_unowned_staging_paths(tmp_path: Path) -> None:
    receipt = _old_pass(tmp_path)
    base = receipt / "base"
    base.mkdir()
    original_inode = base.stat().st_ino
    pending = receipt / "verdict.pending"
    pending.write_text("unowned", encoding="utf-8")
    result = _run_wrapper(BASE, tmp_path, bin_dir=_tools(tmp_path, missing="uv"))
    assert pending.read_text(encoding="utf-8") == "unowned"
    assert base.stat().st_ino == original_inode
    assert result.returncode == 127
    assert not list(tmp_path.glob("migration-upgrade-secrets.*"))


def test_wrapper_preserves_stale_pending_directory_and_publishes_failure(
    tmp_path: Path,
) -> None:
    receipt = tmp_path / "migration-upgrade"
    stale = receipt / "seed.pending"
    stale.mkdir(parents=True)
    unowned = stale / "keep"
    unowned.write_text("unowned", encoding="utf-8")
    verdict_path = receipt / "verdict.json"
    verdict_path.write_text('{"result":"PASS"}\n', encoding="utf-8")

    result = _run_wrapper(BASE, tmp_path)

    assert result.returncode == 1
    assert unowned.read_text(encoding="utf-8") == "unowned"
    assert verdict_path.is_file(), result.stdout[-6000:]
    verdict = json.loads(verdict_path.read_text(encoding="utf-8"))
    assert verdict["result"] == "FAIL"
    assert verdict["reason"] == "stale_seed_pending"
    assert not (receipt / "base").exists()
    assert not list(tmp_path.glob("migration-upgrade-secrets.*"))

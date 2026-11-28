"""Integration contract for the opt-in, claimed-PostgreSQL Wave H wrapper."""

import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[2]
BASE = "61918080021ddcfe2912e6922677143d5704d96d"
FOUNDATION = "b39ae436590c8a9c00ad8fc93d619875d1822827"
# The only interpreter any stand-in tool may name: the sandbox can see it, and a
# Python found on PATH (a user install, miniconda) is outside what the run allows.
SYSTEM_PYTHON = "/usr/bin/python3"


@pytest.fixture
def synthetic_secret_backend() -> None:
    """The subprocess provisions its own KEK inside its private run tree."""


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
    # The real tools a shim execs by absolute path must be reachable inside the
    # sandbox, where only named paths exist.
    real_tools = os.pathsep.join(
        str(Path(found).resolve().parent)
        for found in (shutil.which("uv"), shutil.which("git"))
        if found
    )
    return {
        **env,
        "UPGRADE_SANDBOX_READ_ONLY": real_tools,
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
        "bwrap",
        "git",
        "tar",
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
        source = SYSTEM_PYTHON if name == "python3" else shutil.which(name)
        if source is None:
            # These tests stop before invoking Docker/uv.
            assert name in {"docker", "uv"}
            source = "/bin/false"
        (bin_dir / name).symlink_to(source)
    if missing != "python3":
        assert (bin_dir / "python3").resolve().parent == Path("/usr/bin")
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
    # Children run in a sandbox that cannot see the wrapper, so the signal comes
    # from a trusted-wrapper command: the check right after run-dir creation.
    bin_dir = _tools(tmp_path, missing="stat")
    stat = shutil.which("stat")
    assert stat is not None
    shim = bin_dir / "stat"
    shim.write_text(
        "#!/bin/bash\n"
        'if [[ "$*" == *"%d:%i:%a"* ]]; then kill -TERM "$PPID"; fi\n'
        f'exec {stat} "$@"\n',
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
        'if [[ "$3" == archive || "$1" == clone || "$3:$4" == worktree:add ]]; then\n'
        f'    {git} "$@" || exit $?\n'
        '    base="${UPGRADE_PRIVATE_RUN_DIR:-${6%/*}}/base"\n'
        '    path="${base%/*}/tenant-kek.secret"\n'
        "    (set -o noclobber; printf 'unowned key marker\\n' > \"$path\") || exit 1\n"
        '    echo "marker-identity $(stat -c %d:%i "$path")" >&2\n'
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
    assert "marker-identity " in result.stdout
    assert "private_output_exists: tenant-kek.secret" in result.stdout
    assert not list(tmp_path.glob("migration-upgrade-secrets.*"))


@pytest.mark.parametrize("collision", [False, True])
def test_child_output_receipt_controls_cleanup(tmp_path: Path, collision: bool) -> None:
    bin_dir = _tools(tmp_path, missing="git")
    git = shutil.which("git")
    assert git is not None
    shim = bin_dir / "git"
    shim.write_text(
        "#!/bin/bash\n"
        'if [[ "$3" == archive || "$1" == clone || "$3:$4" == worktree:add ]]; then\n'
        f'    {git} "$@" || exit $?\n'
        '    base="${UPGRADE_PRIVATE_RUN_DIR:-${6%/*}}/base"\n'
        '    output="${base%/*}/ai-assertions.json"\n'
        + ('    printf "unowned\\n" > "$output"\n' if collision else "")
        + """    python3 - "$output" >&2 <<'PY'
import os
import sys
from pathlib import Path
path = Path(sys.argv[1])
try:
    with path.open("x", encoding="utf-8") as output:
        output.write("child wrote\\n")
except FileExistsError:
    print("private_output_exists: ai-assertions.json")
    assert path.read_bytes() == b"unowned\\n"
else:
    assert Path(os.environ["UPGRADE_PRIVATE_RUN_DIR"]).samefile(path.parent)
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
    else:
        assert "owned child output created" in result.stdout
    assert not outputs


@pytest.mark.parametrize("directory", [False, True])
def test_wrapper_refuses_preplaced_run_path(tmp_path: Path, directory: bool) -> None:
    bin_dir = _tools(tmp_path, missing="od")
    shim = bin_dir / "od"
    nonce = "a" * 32
    path = tmp_path / f"migration-upgrade-secrets.{nonce}"
    if directory:
        path.mkdir(mode=0o700)
    marker = path / "marker" if directory else path
    marker.write_bytes(b"untouched\n")
    before = marker.stat()
    shim.write_text(f"#!/bin/bash\nprintf '%s\\n' {nonce}\n", encoding="utf-8")
    shim.chmod(0o700)

    result = _run_wrapper(BASE, tmp_path, bin_dir=bin_dir)

    assert result.returncode == 1
    assert "private_run_create_failed" in result.stdout
    assert list(tmp_path.glob("migration-upgrade-secrets.*")) == [path]
    assert os.path.samestat(before, marker.stat())
    assert marker.read_bytes() == b"untouched\n"


def test_wrapper_leaves_foreign_private_prefix_alone(tmp_path: Path) -> None:
    run = tmp_path / "migration-upgrade-secrets.unrelated"
    run.mkdir(mode=0o700)
    marker = run / "marker"
    marker.write_bytes(b"foreign\n")
    before = marker.stat()

    result = _run_wrapper(BASE, tmp_path, bin_dir=_tools(tmp_path))

    assert result.returncode == 1
    assert "private_run_create_failed" in result.stdout
    verdict = json.loads(
        (tmp_path / "migration-upgrade/verdict.json").read_text(encoding="utf-8")
    )
    assert verdict["result"] == "FAIL"
    assert os.path.samestat(before, marker.stat())
    assert marker.read_bytes() == b"foreign\n"


def test_wrapper_removes_planted_tree_without_following_links(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "marker"
    marker.write_bytes(b"untouched\n")
    before = marker.stat().st_ino
    bin_dir = _tools(tmp_path, missing="git")
    shim = bin_dir / "git"
    git = shutil.which("git")
    assert git is not None
    shim.write_text(
        "#!/bin/bash\n"
        'if [[ "$3" == archive || "$1" == clone || "$3:$4" == worktree:add ]]; then\n'
        '    [[ -n "${UPGRADE_PRIVATE_RUN_DIR:-}" ]] || exit 2\n'
        '    mkdir "$UPGRADE_PRIVATE_RUN_DIR/planted" || exit 2\n'
        '    printf "planted\\n" > "$UPGRADE_PRIVATE_RUN_DIR/planted/marker"\n'
        '    ln -s -- "$RUNNER_TEMP/outside" "$UPGRADE_PRIVATE_RUN_DIR/planted/link"\n'
        "    echo planted-tree >&2\n"
        "    exit 1\n"
        f'fi\nexec {git} "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(0o700)

    result = _run_wrapper(BASE, tmp_path, bin_dir=bin_dir)

    assert result.returncode == 1
    assert "planted-tree" in result.stdout
    assert not list(tmp_path.glob("migration-upgrade-secrets.*"))
    assert (marker.stat().st_ino, marker.read_bytes()) == (before, b"untouched\n")


def test_f7_inbox_seed_refuses_without_private_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer = ROOT / "ops/testing/migration_upgrade_inbox_seed.py"
    if not writer.is_file():
        pytest.skip("exercises the F7 reference writer")
    from runpy import run_path  # noqa: PLC0415 - F7 writer is optional on this branch

    monkeypatch.delenv("UPGRADE_PRIVATE_RUN_DIR", raising=False)
    with pytest.raises(
        RuntimeError, match="private_run_directory_required: inbox seed"
    ):
        run_path(str(writer))["seed_inbox"]({})


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
    # Children have no stdin, so the first wrapper is held at staging by a FIFO.
    # It lives in the tools directory because the sandbox binds only that.
    release = bin_dir / "release"
    os.mkfifo(release)
    # The child opens it read-only (Landlock allows nothing more outside the run);
    # holding a write end here keeps that open from blocking, whatever the order.
    release_fd = os.open(release, os.O_RDWR)
    shim = bin_dir / "git"
    shim.write_text(
        "#!/bin/bash\n"
        'if [[ "$3" == archive || "$1" == clone || "$3:$4" == worktree:add ]]; then\n'
        f'    echo staging >&2; read -r -t 300 _ < "{release}"; exit 1\nfi\n'
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
            os.write(release_fd, b"finish\n")
            first.communicate(timeout=15)


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


def _plant_shim(
    bin_dir: Path, tool: str, stage: str, plant: str, scratch: Path
) -> None:
    """Run ``plant`` inside the run directory the moment ``stage`` is reached."""
    real = shutil.which(tool)
    assert real is not None
    shim = bin_dir / tool
    shim.unlink()
    shim.write_text(
        "#!/bin/bash\n"
        f"if [[ {stage} ]]; then\n"
        '    RUN="$UPGRADE_PRIVATE_RUN_DIR"\n'
        f'    FOREIGN_DIR="{scratch / "foreign-dir"}"\n'
        f'    FOREIGN_FILE="{scratch / "foreign-file"}"\n'
        f"    {plant}\n"
        "fi\n"
        f'exec {real} "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(0o700)


def _foreign(tmp_path: Path) -> tuple[Path, Path, os.stat_result]:
    directory = tmp_path / "foreign-dir"
    directory.mkdir()
    marker = tmp_path / "foreign-file"
    marker.write_bytes(b"foreign\n")
    # A foreign file named for the interpreter: a name must never earn trust.
    (tmp_path / "python3").write_bytes(b"foreign\n")
    return directory, marker, marker.stat()


def _assert_untouched(directory: Path, marker: Path, before: os.stat_result) -> None:
    assert list(directory.iterdir()) == []
    assert (directory.parent / "python3").read_bytes() == b"foreign\n"
    assert (marker.stat().st_ino, marker.read_bytes()) == (
        before.st_ino,
        b"foreign\n",
    )


PLANTED_NAMES = {
    "archive_output_link": ('ln -s -- "$FOREIGN_FILE" "$RUN/base.tar"', None),
    "extraction_root_directory_link": (
        'ln -s -- "$FOREIGN_DIR" "$RUN/base"',
        "private_extract_failed",
    ),
    "extraction_root_file_link": (
        'ln -s -- "$FOREIGN_FILE" "$RUN/base"',
        "private_extract_failed",
    ),
    "tmp_replaced_by_link": (
        'mv -- "$RUN/tmp" "$RUN/tmp.moved"; ln -s -- "$FOREIGN_DIR" "$RUN/tmp"',
        "private_tree_foreign_entry",
    ),
    "uv_cache_replaced_by_link": (
        'mv -- "$RUN/uv-cache" "$RUN/uv-cache.moved"; '
        'ln -s -- "$FOREIGN_DIR" "$RUN/uv-cache"',
        "private_tree_foreign_entry",
    ),
}


@pytest.mark.parametrize("name", PLANTED_NAMES)
def test_run_children_never_follow_a_planted_name(tmp_path: Path, name: str) -> None:
    plant, refusal = PLANTED_NAMES[name]
    directory, marker, before = _foreign(tmp_path)
    bin_dir = _tools(tmp_path)
    _plant_shim(bin_dir, "git", '"$3" == archive', plant, tmp_path)

    result = _run_wrapper(BASE, tmp_path, bin_dir=bin_dir)

    assert result.returncode == 1, result.stdout
    _assert_untouched(directory, marker, before)
    if refusal:
        assert f"setup: {refusal}" in result.stdout


def test_environment_link_never_follows_a_planted_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UPGRADE_REUSE_ENVIRONMENT", "1")
    directory, marker, before = _foreign(tmp_path)
    bin_dir = _tools(tmp_path)
    _plant_shim(
        bin_dir,
        "cmp",
        '-d "$UPGRADE_PRIVATE_RUN_DIR/base"',
        'ln -s -- "$FOREIGN_DIR" "$RUN/base/.venv"',
        tmp_path,
    )

    result = _run_wrapper(BASE, tmp_path, bin_dir=bin_dir)

    assert result.returncode == 1, result.stdout
    assert "FileExistsError" in result.stdout
    _assert_untouched(directory, marker, before)


@dataclass(frozen=True)
class ShimOptions:
    """How the stand-in tool plants: where it execs on, and what the links name."""

    passthrough: bool = False
    stdio: bool = False
    python_named: bool = False
    writer: bool = False  # plant a link to stdout and leave the write to the real tool


def _plant_commands(
    links: tuple[tuple[str, str], ...], options: ShimOptions | None = None
) -> str:
    """Replace each entry with a link the way a follower's attacker would.

    The real entry (uv's lock file, its interpreter directory) is moved aside
    first, so the link is always created; a name that cannot be planted fails
    loudly instead of being mistaken for an escape. In ``stdio`` mode the links
    name the wrapper's own descriptors and are opened for truncation; in
    ``python_named`` mode a file link points at a foreign file called python3.
    """
    options = options or ShimOptions()
    plants = ""
    for index, (base, name) in enumerate(links):
        variable, label = PLANT_BASES[base]
        directory = name.endswith(DIRECTORY_PLANTS) and not (
            options.python_named and name == "interpreter-v4"
        )
        if options.writer:
            target = "/proc/self/fd/1"
        elif options.stdio:
            target = (
                f"/proc/self/fd/{index % 3}"
                if index % 2 == 0
                else f"/dev/fd/{index % 3}"
            )
        elif options.python_named:
            target = "$FOREIGN_DIR" if directory else "$FOREIGN_PYTHON"
        else:
            target = "$FOREIGN_DIR" if directory else "$FOREIGN_FILE"
        written = f"{variable}/{name}/pwned" if directory else f"{variable}/{name}"
        if options.writer:
            attempt = f"        echo PLANTED {label}{name} >&2\n"
        elif options.stdio:
            attempt = (
                f'        {{ echo PWN > "{variable}/{name}"; }} 2>/dev/null\n'
                f"        echo STDIO_ATTEMPTED {label}{name} >&2\n"
            )
        else:
            attempt = (
                f'        if {{ echo PWN >> "{written}"; }} 2>/dev/null; then\n'
                f"            echo WRITE_ESCAPED {label}{name} >&2\n"
                "        else\n"
                f"            echo WRITE_BLOCKED {label}{name} >&2\n"
                "        fi\n"
            )
        plants += (
            f'    mv -f -- "{variable}/{name}" "{variable}/{name}.moved" 2>/dev/null\n'
            f'    if ln -s -- "{target}" "{variable}/{name}" 2>/dev/null; then\n'
            f"{attempt}"
            "    else\n"
            f"        echo PLANT_FAILED {label}{name} >&2\n"
            "    fi\n"
        )
    return plants


def _mid_run_shim(
    bin_dir: Path,
    tool: str,
    plant: tuple[str, tuple[tuple[str, str], ...]],
    scratch: Path,
    options: ShimOptions,
) -> None:
    """Plant links when the trigger holds, then write through them like a follower."""
    trigger, links = plant
    real = shutil.which(tool)
    assert real is not None
    shim = bin_dir / tool
    shim.unlink()
    shim.write_text(
        "#!/bin/bash\n"
        'RUN="$UPGRADE_PRIVATE_RUN_DIR"\n'
        'NATIVE="${UPGRADE_WAVE_H_PROBE_CASES%/*}"\n'
        'CACHE="$UV_CACHE_DIR"\n'
        f'FOREIGN_DIR="{scratch / "foreign-dir"}"\n'
        f'FOREIGN_FILE="{scratch / "foreign-file"}"\n'
        f'FOREIGN_PYTHON="{scratch / "python3"}"\n'
        f"if [[ {trigger} ]]; then\n"
        f"{_plant_commands(links, options)}"
        "fi\n" + (f'exec {real} "$@"\n' if options.passthrough else "exit 0\n"),
        encoding="utf-8",
    )
    shim.chmod(0o700)


# Either the census or an exclusive create refuses a planted name.
REFUSALS = ("private_tree_foreign_entry", "FileExistsError")
PLANT_BASES = {
    "run": ("$RUN", ""),
    "native": ("$NATIVE", "native/"),
    "cache": ("$CACHE", "uv-cache/"),
}
DIRECTORY_PLANTS = (".venv", "interpreter-v4", "simple-v19", "wheels-v6")
# The reviewer's plant points, each with its siblings: the wave-commit writers
# open native/seed.json and native/ai-assertions.json, uv opens its cache's
# interpreter-v4 directory and lock file.
NATIVE_WRITERS = '"${UPGRADE_WAVE_H_PROBE_CASES##*/}" == probes.json'
NATIVE_PLANTS = (
    ("native", "seed.json"),
    ("native", "ai-assertions.json"),
    ("native", "environment.json"),
    ("cache", "interpreter-v4"),
    ("cache", ".lock"),
)
MID_RUN_PLANTS = {
    "uv_sync": (
        "uv",
        '"$1" == sync',
        (("cache", ".lock"), ("cache", "interpreter-v4"), ("run", "base/.venv")),
    ),
    "verifier": (
        "uv",
        '"$*" == *wave_h_entry_differential.py*',
        (
            ("run", "verified.json"),
            ("run", "differential.json"),
            ("run", "native/ai-assertions.json"),
            ("cache", "interpreter-v4"),
        ),
    ),
    "native": (
        "git",
        '"$3" == checkout',
        (
            ("run", "native/seed.json"),
            ("run", "native/ai-assertions.json"),
            ("run", "native/probes.json"),
            ("run", "native/environment.json"),
        ),
    ),
    "native_writers": ("uv", NATIVE_WRITERS, NATIVE_PLANTS),
}
# One link and nothing else, named for what the census used to trust.
PYTHON_PLANT = ("uv", '"$1" == sync', (("cache", "interpreter-v4"),))
UNSHARE = shutil.which("unshare") or "/usr/bin/unshare"


def _database_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    # docker is a no-op here, so the connection strings only need to parse.
    for key in (
        "MIGRATION_DATABASE_URL",
        "APP_DATABASE_URL",
        "TEST_SUPERUSER_DATABASE_URL",
        "LOCKS_DATABASE_URL",
    ):
        monkeypatch.setenv(key, "postgresql://role:secret@127.0.0.1:1/clinic")
    for key, value in (
        ("POSTGRES_CONTAINER", "unused"),
        ("POSTGRES_PASSWORD", "unused"),
        ("POSTGRES_USER", "postgres"),
        ("POSTGRES_PORT", "5432"),
    ):
        monkeypatch.setenv(key, value)


def _stage_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    plant: tuple[str, str, tuple[tuple[str, str], ...]],
    options: ShimOptions | None = None,
) -> Path:
    """Tools whose stand-in for ``plant``'s tool plants links at its trigger."""
    monkeypatch.delenv("UPGRADE_REUSE_ENVIRONMENT", raising=False)
    _database_environment(monkeypatch)
    tool, trigger, links = plant
    bin_dir = _tools(tmp_path)
    (bin_dir / "docker").unlink()
    (bin_dir / "docker").symlink_to("/bin/true")
    if tool != "uv":
        stand_in = bin_dir / "uv"
        stand_in.unlink()
        stand_in.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
        stand_in.chmod(0o700)
    chosen = replace(options or ShimOptions(), passthrough=tool == "git")
    _mid_run_shim(bin_dir, tool, (trigger, links), tmp_path, chosen)
    return bin_dir


def _not_a_pass(tmp_path: Path) -> None:
    published = tmp_path / "migration-upgrade" / "verdict.json"
    if published.is_file():
        verdict = json.loads(published.read_text(encoding="utf-8"))
        assert verdict["result"] != "PASS"


@pytest.mark.parametrize("stage", MID_RUN_PLANTS)
def test_links_planted_mid_run_never_reach_a_foreign_inode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    directory, marker, before = _foreign(tmp_path)
    bin_dir = _stage_tools(tmp_path, monkeypatch, MID_RUN_PLANTS[stage])

    result = _run_wrapper(BASE, tmp_path, bin_dir=bin_dir)

    assert result.returncode != 0, result.stdout
    _assert_untouched(directory, marker, before)
    assert "WRITE_ESCAPED" not in result.stdout, result.stdout[-3000:]
    assert "PLANT_FAILED" not in result.stdout, result.stdout[-3000:]
    for base, name in MID_RUN_PLANTS[stage][2]:
        label = PLANT_BASES[base][1] + name
        assert f"WRITE_BLOCKED {label}" in result.stdout, result.stdout[-3000:]
    assert any(refusal in result.stdout for refusal in REFUSALS), result.stdout[-3000:]
    _not_a_pass(tmp_path)


def test_a_link_named_for_the_interpreter_is_refused_when_it_resolves_elsewhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The census judges the inode a link resolves to, not what it is called."""
    directory, marker, before = _foreign(tmp_path)
    bin_dir = _stage_tools(
        tmp_path, monkeypatch, PYTHON_PLANT, ShimOptions(python_named=True)
    )

    result = _run_wrapper(BASE, tmp_path, bin_dir=bin_dir)

    assert result.returncode != 0, result.stdout
    _assert_untouched(directory, marker, before)
    assert "PLANT_FAILED" not in result.stdout, result.stdout[-3000:]
    assert "WRITE_BLOCKED uv-cache/interpreter-v4" in result.stdout, result.stdout[
        -3000:
    ]
    assert any(refusal in result.stdout for refusal in REFUSALS), result.stdout[-3000:]
    _not_a_pass(tmp_path)


def _run_with_host_stdio(
    tmp_path: Path, bin_dir: Path
) -> tuple[subprocess.CompletedProcess[bytes], Path, Path, bytes]:
    """Run the wrapper with host files as stdin and stdout, and return them."""
    prefix = b"HOST-FILE\n"
    host_in, host_out = tmp_path / "host-stdin", tmp_path / "host-stdout"
    host_in.write_bytes(prefix)
    host_out.write_bytes(prefix)
    env = _wrapper_env(BASE, tmp_path, bin_dir, uuid4().hex)
    with host_in.open("rb") as stdin, host_out.open("ab") as stdout:
        result = subprocess.run(  # noqa: S603 - fixed repository harness
            [str(ROOT / "ops/testing/migration_upgrade.sh"), BASE],
            cwd=ROOT,
            env=env,
            stdin=stdin,
            stdout=stdout,
            stderr=stdout,
            check=False,
        )
    return result, host_in, host_out, prefix


@pytest.mark.parametrize("stage", MID_RUN_PLANTS)
def test_stdio_links_planted_mid_run_never_reach_the_wrappers_host_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    bin_dir = _stage_tools(
        tmp_path, monkeypatch, MID_RUN_PLANTS[stage], ShimOptions(stdio=True)
    )

    result, host_in, host_out, prefix = _run_with_host_stdio(tmp_path, bin_dir)

    output = host_out.read_bytes()
    assert result.returncode != 0
    assert b"STDIO_ATTEMPTED" in output, output[-3000:]
    # The wrapper's own stdio stayed the wrapper's: nothing reopened it for
    # truncation, so what it held before the run is still there.
    assert host_in.read_bytes() == prefix
    assert output.startswith(prefix)


@requires_postgres
def test_the_real_seed_writer_never_reopens_the_wrappers_stdout(
    tmp_path: Path,
) -> None:
    """The pinned seed writer, opening a link to its stdout, spares the host file."""
    bin_dir = _tools(tmp_path)
    options = ShimOptions(passthrough=True, writer=True)
    _mid_run_shim(
        bin_dir, "uv", (NATIVE_WRITERS, (("native", "seed.json"),)), tmp_path, options
    )

    result, host_in, host_out, prefix = _run_with_host_stdio(tmp_path, bin_dir)

    output = host_out.read_bytes()
    assert result.returncode != 0
    assert b"PLANTED native/seed.json" in output, output[-3000:]
    assert host_in.read_bytes() == prefix
    assert output.startswith(prefix), output[:200]


@requires_postgres
def test_a_pinned_wave_writer_run_with_the_real_uv_never_writes_a_planted_link(
    tmp_path: Path,
) -> None:
    """The pinned wave-commit writers and the real uv meet the planted links."""
    directory, marker, before = _foreign(tmp_path)
    bin_dir = _tools(tmp_path)
    options = ShimOptions(passthrough=True)
    _mid_run_shim(bin_dir, "uv", (NATIVE_WRITERS, NATIVE_PLANTS), tmp_path, options)

    result = _run_wrapper(BASE, tmp_path, bin_dir=bin_dir)

    assert result.returncode != 0, result.stdout[-3000:]
    _assert_untouched(directory, marker, before)
    assert "WRITE_ESCAPED" not in result.stdout, result.stdout[-3000:]
    assert "PLANT_FAILED" not in result.stdout, result.stdout[-3000:]
    # The plant must really have run, or the test proves nothing.
    assert "WRITE_BLOCKED native/seed.json" in result.stdout, result.stdout[-3000:]
    published = tmp_path / "migration-upgrade" / "verdict.json"
    verdict = json.loads(published.read_text(encoding="utf-8"))
    assert verdict["result"] == "FAIL"


# The reviewer's plant points, each driven by the real tool that opens it: the
# census-approved interpreter link under the real makemigrations, the pinned seed
# writer, and the pinned native verifier. Each link names a late host mount.
# The pinned native verifier publishes with ``write_text``, which follows a link,
# so every case here is a write through a planted link that must not land.
LATE_MOUNT_CASES = {
    "interpreter_cache": (
        '"$*" == *"manage.py makemigrations"*',
        ("cache", "interpreter-v4", "python3"),
    ),
    "seed_writer": (NATIVE_WRITERS, ("native", "seed.json", "target")),
    "native_verifier": (
        '"$*" == *wave_h_entry_differential.py*',
        ("native", "ai-assertions.json", "target"),
    ),
}


def _late_mount_shim(
    bin_dir: Path, fifo: Path, mount_dir: Path, case: tuple[str, tuple[str, str, str]]
) -> None:
    """Stand in for uv: announce, wait for the mount, plant, run the real uv."""
    trigger, (base, name, kind) = case
    variable, label = PLANT_BASES[base]
    real = shutil.which("uv")
    assert real is not None
    shim = bin_dir / "uv"
    shim.unlink()
    shim.write_text(
        "#!/bin/bash\n"
        'RUN="$UPGRADE_PRIVATE_RUN_DIR"\n'
        # The differential's probe file is the lane's, in the run directory; the
        # native directory is where the pinned writers' environment file lives.
        'NATIVE="${UPGRADE_WAVE_H_NATIVE_ENV:-$UPGRADE_WAVE_H_PROBE_CASES}"\n'
        'NATIVE="${NATIVE%/*}"\n'
        'CACHE="$UV_CACHE_DIR"\n'
        f"if [[ {trigger} ]]; then\n"
        "    echo MOUNT_NOW >&2\n"
        f'    read -r -t 300 _ < "{fifo}"\n'
        f'    mv -f -- "{variable}/{name}" "{variable}/{name}.moved" 2>/dev/null\n'
        f'    if ln -s -- "{mount_dir}/late/{kind}" "{variable}/{name}"; then\n'
        f"        echo PLANTED {label}{name} >&2\n"
        "    else\n"
        f"        echo PLANT_FAILED {label}{name} >&2\n"
        "    fi\n"
        "fi\n"
        f'{real} "$@"\nexit $?\n',
        encoding="utf-8",
    )
    shim.chmod(0o700)


@requires_postgres
@pytest.mark.parametrize("case", LATE_MOUNT_CASES)
def test_a_real_tool_never_writes_through_a_link_to_a_mount_made_mid_run(
    tmp_path: Path, case: str
) -> None:
    """A real writer meets a link to a host mount made after its sandbox started.

    The wrapper runs inside a user namespace whose mount point is shared, so a
    mount made by this test is an event the sandbox could receive. The shim
    announces itself and blocks on a FIFO until the mount exists; no timer
    decides when the plant happens. A tool that writes through the link must end
    the run non-PASS and leave the host's files exactly as the host put them.
    """
    bin_dir = _tools(tmp_path)
    fifo = bin_dir / "release"
    os.mkfifo(fifo)
    mount_dir = tmp_path / "mnt"
    mount_dir.mkdir()
    _late_mount_shim(bin_dir, fifo, mount_dir, LATE_MOUNT_CASES[case])
    plan = {
        "argv": [str(ROOT / "ops/testing/migration_upgrade.sh"), BASE],
        "env": _wrapper_env(BASE, tmp_path, bin_dir, uuid4().hex),
        "cwd": str(ROOT),
        "mount_dir": str(mount_dir),
        "fifo": str(fifo),
        "secret": "secret",
    }
    (tmp_path / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    helper = Path(__file__).resolve().parent / "wave_h_mid_run_mount.py"

    result = subprocess.run(  # noqa: S603 - fixed helper in a private namespace
        [
            UNSHARE,
            "--user",
            "--map-root-user",
            "--mount",
            "--propagation",
            "shared",
            sys.executable,
            str(helper),
            str(tmp_path / "plan.json"),
        ],
        capture_output=True,
        text=True,
        check=False,
        # Only bounds a failure: the standalone differential alone runs for an hour.
        timeout=4 * 3600,
    )

    assert result.returncode == 0, result.stderr
    outcome = json.loads(result.stdout)
    output = outcome["output"]
    assert "PLANTED" in output, output[-3000:]
    assert "PLANT_FAILED" not in output, output[-3000:]
    assert outcome["returncode"] != 0, output[-3000:]
    # What the host mounted is exactly what the host put there.
    assert outcome["target"] == "secret"
    assert outcome["python3"] == []
    _not_a_pass(tmp_path)


@requires_postgres
def test_a_same_user_namespace_escape_at_the_real_writer_never_reaches_a_host_file(
    tmp_path: Path,
) -> None:
    """The architect's escape against the real seed writer, judged on the host.

    At the barrier a same-user process enters the sandbox's namespaces and clears
    MS_RDONLY on the mount that exposes a host file; the writer then follows a
    planted link to it. The host file is checked here, by this test, without the
    wrapper's census or audit.
    """
    bin_dir = _tools(tmp_path)
    fifo = bin_dir / "release"
    os.mkfifo(fifo)
    mount_dir = tmp_path / "mnt"
    (mount_dir / "late").mkdir(parents=True)
    host_file = mount_dir / "late" / "target"
    host_file.write_text("secret", encoding="utf-8")
    before = host_file.stat()
    _late_mount_shim(bin_dir, fifo, mount_dir, LATE_MOUNT_CASES["seed_writer"])
    environment = _wrapper_env(BASE, tmp_path, bin_dir, uuid4().hex)
    environment["UPGRADE_SANDBOX_READ_ONLY"] += os.pathsep + str(mount_dir)
    plan = {
        "argv": [str(ROOT / "ops/testing/migration_upgrade.sh"), BASE],
        "env": environment,
        "cwd": str(ROOT),
        "mount_dir": str(mount_dir),
        "fifo": str(fifo),
        "secret": "secret",
        "escape": True,
    }
    (tmp_path / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    helper = Path(__file__).resolve().parent / "wave_h_mid_run_mount.py"

    result = subprocess.run(  # noqa: S603 - fixed helper in a private namespace
        [
            UNSHARE,
            "--user",
            "--map-root-user",
            "--mount",
            "--propagation",
            "shared",
            sys.executable,
            str(helper),
            str(tmp_path / "plan.json"),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=4 * 3600,
    )

    assert result.returncode == 0, result.stderr
    outcome = json.loads(result.stdout)
    output = outcome["output"]
    assert "ESCAPE CLEARED" in output, output[-3000:]
    assert "PLANTED" in output, output[-3000:]
    assert outcome["returncode"] != 0, output[-3000:]
    after = host_file.stat()
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    assert host_file.read_bytes() == b"secret"
    _not_a_pass(tmp_path)


def _gate(real: str, fifo: Path, trigger: str, action: str = "") -> str:
    """A stand-in tool: at the trigger act, announce, and wait for the peer."""
    return (
        "#!/bin/bash\n"
        f"if [[ {trigger} ]]; then\n"
        f"{action}"
        "    echo MOUNT_NOW >&2\n"
        f'    read -r -t 300 _ < "{fifo}"\n'
        "fi\n"
        f'exec {real} "$@"\n'
    )


def _rewrite_attestations(names: tuple[str, ...]) -> str:
    """Bash that rewrites run files the way anything able to write the run could."""
    listed = " ".join(repr(name) for name in names)
    return (
        f"    {SYSTEM_PYTHON} -B -I - \"$UPGRADE_PRIVATE_RUN_DIR\" {listed} <<'PYEOF'\n"
        "import json, sys\n"
        "from pathlib import Path\n"
        "for name in sys.argv[2:]:\n"
        "    path = Path(sys.argv[1]) / name\n"
        "    if path.is_file():\n"
        "        data = json.loads(path.read_text())\n"
        '        data["forged"] = "yes"\n'
        "        path.write_text(json.dumps(data))\n"
        "PYEOF\n"
    )


def _forge_differential(real_python: str) -> str:
    """Forge the sealed report in place and announce the hash a peer would write."""
    return (
        f"    {real_python} -B -I - \"$UPGRADE_PRIVATE_RUN_DIR\" <<'PYEOF'\n"
        "import hashlib, json, sys\n"
        "from pathlib import Path\n"
        'path = Path(sys.argv[1]) / "differential.json"\n'
        "if path.is_file():\n"
        "    data = json.loads(path.read_text())\n"
        '    data["projection_cases"] = 515151\n'
        '    data["forged"] = "yes"\n'
        "    path.write_text(json.dumps(data))\n"
        "    digest = hashlib.sha256(path.read_bytes()).hexdigest()\n"
        '    print("FORGED differential.json", digest, file=sys.stderr, flush=True)\n'
        "PYEOF\n"
    )


def _assert_system_interpreters(shim: str) -> None:
    """A stand-in tool may run the system Python and no other interpreter."""
    stray = re.findall(r"(?<!/usr/bin/)\bpython3?\b(?![._/-])", shim)
    assert stray == [], shim
    for path in re.findall(r"(/\S*/python3?\S*)", shim):
        assert path.startswith("/usr/bin/"), path


def _peer_run(
    tmp_path: Path,
    shims: Callable[[Path], dict[str, str]],
    *,
    edit: tuple[str, ...] = (),
    differential: bool = True,
) -> tuple[dict[str, object], str]:
    """Run the wrapper with stand-in tools and a peer; return outcome and verdict."""
    bin_dir = _tools(tmp_path)
    fifo = bin_dir / "release"
    os.mkfifo(fifo)
    mount_dir = tmp_path / "mnt"
    mount_dir.mkdir()
    for tool, text in shims(fifo).items():
        _assert_system_interpreters(text)
        shim = bin_dir / tool
        shim.unlink()
        shim.write_text(text, encoding="utf-8")
        shim.chmod(0o700)
    environment = _wrapper_env(BASE, tmp_path, bin_dir, uuid4().hex)
    if not differential:
        environment["UPGRADE_WAVE_H_DIFFERENTIAL"] = ""
    plan = {
        "argv": [str(ROOT / "ops/testing/migration_upgrade.sh"), BASE],
        "env": environment,
        "cwd": str(ROOT),
        "mount_dir": str(mount_dir),
        "fifo": str(fifo),
        "tamper": {
            "runner_temp": str(tmp_path),
            "edit": [str(ROOT / "ops/testing" / name) for name in edit],
            "canary": str(tmp_path / "canary"),
        },
    }
    (tmp_path / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    helper = Path(__file__).resolve().parent / "wave_h_mid_run_mount.py"

    result = subprocess.run(  # noqa: S603 - fixed helper in a private namespace
        [
            UNSHARE,
            "--user",
            "--map-root-user",
            "--mount",
            "--propagation",
            "shared",
            sys.executable,
            str(helper),
            str(tmp_path / "plan.json"),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=4 * 3600,
    )

    assert result.returncode == 0, result.stderr
    published = tmp_path / "migration-upgrade" / "verdict.json"
    return json.loads(result.stdout), published.read_text(encoding="utf-8")


def _tool(name: str) -> str:
    found = shutil.which(name)
    assert found is not None
    return found


@requires_postgres
@pytest.mark.parametrize(
    ("forge", "passes"),
    [
        pytest.param(("verified.json",), False, id="attestation"),
        pytest.param(("differential.json",), True, id="differential_report"),
    ],
)
def test_a_rewrite_after_the_supervisor_read_a_file_never_reaches_the_verdict(
    tmp_path: Path, forge: tuple[str, ...], passes: bool
) -> None:
    """The verdict is built from the bytes read once, not from the files again.

    A rewritten attestation must stop the run (the verdict the verifier wrote no
    longer agrees with what was read); a rewritten differential report must not
    change a word of the published one. No seal file may exist for a peer to
    rewrite at the barrier.
    """

    def shims(fifo: Path) -> dict[str, str]:
        return {
            "uv": _gate(
                _tool("uv"),
                fifo,
                '"$*" == *"manage.py makemigrations"*',
                _rewrite_attestations(forge),
            )
        }

    outcome, published = _peer_run(tmp_path, shims)

    assert "forged" not in published, f"FORGED_PUBLISHED {published}"
    assert outcome["seals"] == [], outcome["seals"]
    assert (json.loads(published)["result"] == "PASS") is passes, published[-800:]


@requires_postgres
def test_a_forged_report_with_a_rewritten_seal_is_not_published(
    tmp_path: Path,
) -> None:
    """The reviewer's attack on the seal: forge the report, then fix its hash.

    At the verdict writer's launch the child rewrites the sealed report in
    place; the peer rewrites the seal manifest to match, if a manifest exists
    for it to reach. Nothing may certify the forged numbers, and the failure
    must not publish them either.
    """

    def shims(fifo: Path) -> dict[str, str]:
        real = SYSTEM_PYTHON
        return {
            "python3": _gate(
                real, fifo, '"$*" == *verified.json*', _forge_differential(real)
            )
        }

    outcome, published = _peer_run(tmp_path, shims)

    assert "FORGED differential.json" in str(outcome["output"]), (
        f"ATTACK_DID_NOT_RUN {str(outcome['output'])[-1500:]}"
    )
    assert "515151" not in published, f"FORGED_PUBLISHED {published}"
    assert outcome["seals"] == [], outcome["seals"]
    result = json.loads(published)["result"]
    assert result != "PASS", f"FORGED_CERTIFIED {published[-800:]}"


@requires_postgres
def test_an_edit_of_the_launcher_and_census_in_the_checkout_never_runs(
    tmp_path: Path,
) -> None:
    """The helpers that decide a verdict come from the commit, not the checkout.

    The peer appends a line that creates a canary to the launcher, the policy
    and the census in the checkout while the run is under way. Nothing the
    supervisor executes may be the edited copy, and a run whose checkout changed
    must not pass.
    """
    edited = (
        "migration_upgrade_sandbox.py",
        "migration_upgrade_landlock.py",
        "migration_upgrade_custody.py",
    )

    def shims(fifo: Path) -> dict[str, str]:
        gate = _gate(_tool("uv"), fifo, '"$*" == *"manage.py makemigrations"*')
        return {"uv": gate}

    outcome, published = _peer_run(tmp_path, shims, edit=edited)
    output = str(outcome["output"])

    assert outcome["canary"] is False, f"CANARY_RAN {output[-3000:]}"
    assert json.loads(published)["result"] != "PASS", published[-800:]


@requires_postgres
def test_a_python_on_the_path_never_runs_for_the_supervisor(tmp_path: Path) -> None:
    """Every interpreter the supervisor starts is the absolute one.

    A ``python3`` earlier on PATH creates a canary when it runs. A child may run
    it (the sandbox refuses the write); the supervisor must never.
    """
    canary = tmp_path / "canary"

    def shims(_fifo: Path) -> dict[str, str]:
        return {
            "python3": f'#!/bin/bash\n: > "{canary}"\nexec {SYSTEM_PYTHON} -B -I "$@"\n'
        }

    _peer_run(tmp_path, shims, differential=False)

    assert not canary.exists(), "SUPERVISOR_RAN_PATH_PYTHON"


@requires_postgres
def test_the_pinned_native_verifier_never_writes_through_a_link_to_a_visible_host_file(
    tmp_path: Path,
) -> None:
    """The pinned writer follows links; the target here is a file the child sees.

    The earlier cases plant a link to a target that does not exist inside the
    child, so the write fails for want of a path. Here the host file is bound
    read-only into the sandbox: the write is refused for what it is.
    """
    bin_dir = _tools(tmp_path)
    fifo = bin_dir / "release"
    os.mkfifo(fifo)
    mount_dir = tmp_path / "mnt"
    mount_dir.mkdir()
    _late_mount_shim(bin_dir, fifo, mount_dir, LATE_MOUNT_CASES["native_verifier"])
    environment = _wrapper_env(BASE, tmp_path, bin_dir, uuid4().hex)
    environment["UPGRADE_SANDBOX_READ_ONLY"] += os.pathsep + str(mount_dir)
    plan = {
        "argv": [str(ROOT / "ops/testing/migration_upgrade.sh"), BASE],
        "env": environment,
        "cwd": str(ROOT),
        "mount_dir": str(mount_dir),
        "fifo": str(fifo),
        "secret": "secret",
        "visible": True,
    }
    (tmp_path / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    helper = Path(__file__).resolve().parent / "wave_h_mid_run_mount.py"

    result = subprocess.run(  # noqa: S603 - fixed helper in a private namespace
        [
            UNSHARE,
            "--user",
            "--map-root-user",
            "--mount",
            "--propagation",
            "shared",
            sys.executable,
            str(helper),
            str(tmp_path / "plan.json"),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=4 * 3600,
    )

    assert result.returncode == 0, result.stderr
    outcome = json.loads(result.stdout)
    output = outcome["output"]
    assert "PLANTED" in output, output[-3000:]
    assert outcome["returncode"] != 0, output[-3000:]
    assert outcome["target"] == "secret"
    _not_a_pass(tmp_path)


SUPERVISOR_PYTHON_SANCTIONED = re.compile(
    r"/usr/bin/python3|^\s*#|sandboxed|command -v python3"
    r"|for (required|sandbox_tool) in"
)


def test_the_supervisor_runs_only_the_system_python() -> None:
    """Every Python the supervisor starts itself is the absolute system one.

    A bare ``python3`` would resolve through PATH, which a same-user peer or a
    stand-in tool can point anywhere; children run under the sandbox instead.
    """
    script = (ROOT / "ops/testing/migration_upgrade.sh").read_text()
    stray = [
        line.strip()
        for line in script.splitlines()
        if re.search(r"\bpython3\b", line)
        and not SUPERVISOR_PYTHON_SANCTIONED.search(line)
    ]

    assert stray == []

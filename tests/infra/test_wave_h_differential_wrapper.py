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
    differential = verdict["differential"]
    assert {
        key: differential[key]
        for key in (
            "result",
            "projection_cases",
            "probe_window_cases",
            "differences",
            "base_reject_lane_pass_pairs",
        )
    } == {
        "result": "PASS",
        "projection_cases": 17506,
        "probe_window_cases": 118,
        "differences": [],
        "base_reject_lane_pass_pairs": [],
    }
    assert differential["native_inbox"]["result"] == "PASS"
    assert (
        differential["native_inbox"]["snapshot_sha256"]
        == differential["projection_snapshot_sha256"]["native"]
    )
    assert len(differential["tables"]) == len(set(differential["tables"])) == 59
    assert differential["inbox_attempt_byte_mutation"] == "REJECTED"
    assert set(differential["binary_catalog_columns"]) == set(differential["tables"])
    assert set(differential["inbox_binary_mutations_rejected"]) == {
        "actor_id",
        "foreign_key",
        "timestamptz",
        "jsonb",
        "bytea",
        "text",
    }
    witnesses = differential["inbox_binary_mutations_rejected"]
    assert witnesses["actor_id"] == "comms_integrationoperation.actor_id"
    for kind, location in witnesses.items():
        table, column = location.split(".", 1)
        assert column in differential["binary_catalog_columns"][table]
        if kind == "jsonb" and not table.startswith("comms_"):
            assert "inbox_jsonb_scope" in differential
        else:
            assert table.startswith("comms_")
    for verifier in ("native", "lane"):
        fault = differential["full_entry_actor_id"][verifier]
        assert (fault["status"], fault["attested"]) == (1, False)
        assert any(
            failure.endswith("preserved:comms_integrationoperation")
            for failure in fault["failures"]
        )
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
    shim = bin_dir / "git"
    shim.write_text(
        "#!/bin/bash\n"
        'if [[ "$3" == archive || "$1" == clone || "$3:$4" == worktree:add ]]; then\n'
        "    echo staging >&2; read -r _; exit 1\nfi\n"
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
    return directory, marker, marker.stat()


def _assert_untouched(directory: Path, marker: Path, before: os.stat_result) -> None:
    assert list(directory.iterdir()) == []
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


def _plant_commands(links: tuple[tuple[str, str], ...]) -> str:
    plants = ""
    for base, name in links:
        variable, label = PLANT_BASES[base]
        directory = name.endswith(DIRECTORY_PLANTS)
        target = "$FOREIGN_DIR" if directory else "$FOREIGN_FILE"
        written = f"{variable}/{name}/pwned" if directory else f"{variable}/{name}"
        plants += (
            f'    ln -s -- "{target}" "{variable}/{name}"\n'
            f'    if {{ echo PWN >> "{written}"; }} 2>/dev/null; then\n'
            f"        echo WRITE_ESCAPED {label}{name} >&2\n"
            "    else\n"
            f"        echo WRITE_BLOCKED {label}{name} >&2\n"
            "    fi\n"
        )
    return plants


def _mid_run_shim(
    bin_dir: Path,
    tool: str,
    plant: tuple[str, tuple[tuple[str, str], ...]],
    scratch: Path,
    *,
    passthrough: bool,
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
        f"if [[ {trigger} ]]; then\n"
        f"{_plant_commands(links)}"
        "fi\n" + (f'exec {real} "$@"\n' if passthrough else "exit 0\n"),
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


def _database_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    # docker is a no-op in these runs, so the connection strings only need to parse.
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


@pytest.mark.parametrize("stage", MID_RUN_PLANTS)
def test_links_planted_mid_run_never_reach_a_foreign_inode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    monkeypatch.delenv("UPGRADE_REUSE_ENVIRONMENT", raising=False)
    _database_environment(monkeypatch)
    tool, trigger, links = MID_RUN_PLANTS[stage]
    directory, marker, before = _foreign(tmp_path)
    bin_dir = _tools(tmp_path)
    (bin_dir / "docker").unlink()
    (bin_dir / "docker").symlink_to("/bin/true")
    if tool != "uv":
        stand_in = bin_dir / "uv"
        stand_in.unlink()
        stand_in.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
        stand_in.chmod(0o700)
    _mid_run_shim(bin_dir, tool, (trigger, links), tmp_path, passthrough=tool == "git")

    result = _run_wrapper(BASE, tmp_path, bin_dir=bin_dir)

    assert result.returncode != 0, result.stdout
    _assert_untouched(directory, marker, before)
    assert "WRITE_ESCAPED" not in result.stdout, result.stdout[-3000:]
    for base, name in links:
        label = PLANT_BASES[base][1] + name
        assert f"WRITE_BLOCKED {label}" in result.stdout, result.stdout[-3000:]
    assert any(refusal in result.stdout for refusal in REFUSALS), result.stdout[-3000:]
    published = tmp_path / "migration-upgrade" / "verdict.json"
    if published.is_file():
        verdict = json.loads(published.read_text(encoding="utf-8"))
        assert verdict["result"] != "PASS"


@requires_postgres
def test_pinned_wave_writers_never_write_through_planted_links(
    tmp_path: Path,
) -> None:
    """The pinned wave-commit writers and the real uv meet the planted links."""
    directory, marker, before = _foreign(tmp_path)
    bin_dir = _tools(tmp_path)
    _mid_run_shim(
        bin_dir, "uv", (NATIVE_WRITERS, NATIVE_PLANTS), tmp_path, passthrough=True
    )

    result = _run_wrapper(BASE, tmp_path, bin_dir=bin_dir)

    assert result.returncode != 0, result.stdout[-3000:]
    _assert_untouched(directory, marker, before)
    assert "WRITE_ESCAPED" not in result.stdout, result.stdout[-3000:]
    published = tmp_path / "migration-upgrade" / "verdict.json"
    verdict = json.loads(published.read_text(encoding="utf-8"))
    assert verdict["result"] == "FAIL"


@requires_postgres
def test_a_tampered_run_tree_never_produces_a_pass(tmp_path: Path) -> None:
    """A link made after the last verifier ran still fails the census before PASS."""
    bin_dir = _tools(tmp_path)
    real = shutil.which("uv")
    assert real is not None
    shim = bin_dir / "uv"
    shim.unlink()
    shim.write_text(
        "#!/bin/bash\n"
        f'{real} "$@"\nstatus=$?\n'
        'if [[ "$*" == *"manage.py makemigrations"* ]]; then\n'
        '    run="$UPGRADE_PRIVATE_RUN_DIR"\n'
        '    ln -s -- "$run/tmp" "$run/native/leftover"\n'
        "fi\n"
        "exit $status\n",
        encoding="utf-8",
    )
    shim.chmod(0o700)

    result = _run_wrapper(BASE, tmp_path, bin_dir=bin_dir)

    assert result.returncode != 0, result.stdout[-3000:]
    assert "private_tree_foreign_entry: native/leftover" in result.stdout
    published = tmp_path / "migration-upgrade" / "verdict.json"
    verdict = json.loads(published.read_text(encoding="utf-8"))
    assert verdict["result"] == "FAIL"

"""Run registered suites from one committed, independent canonical-ledger clone."""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

E = Path(__file__).resolve().parent.parent
CLONE = Path("/home/samarone/.c9/t57-st01a1b176")
SOURCE = Path("/home/samarone/Documents/clinic-ops-new-roadmap/clinic-os")
VENV = Path("/home/samarone/Documents/clinic-ops-new-roadmap/clinic-os-wt/cpi-e-t57-r2/.venv")
ENV = {
    "HOME": "/home/samarone", "LANG": "C.UTF-8",
    "PATH": f"{VENV / 'bin'}:/home/samarone/.local/bin:/usr/local/bin:/usr/bin:/bin",
    "UV_PROJECT_ENVIRONMENT": str(VENV), "UV_NO_SYNC": "1", "UV_FROZEN": "1",
    "UV_NO_ENV_FILE": "1", "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONUNBUFFERED": "1", "CLINIC_EVIDENCE_ROOT": str(CLONE / ".omo/evidence"),
    "RUNNER_TEMP": str(CLONE / ".omo/tmp"), "CELERY_BROKER_URL": "memory://",
    "CLINIC_PDF_TOOLS": "required", "GIT_PAGER": "cat",
}
PYTHON = str(VENV / "bin/python")


def captured(argv):
    result = subprocess.run(argv, cwd=CLONE, env=ENV, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError(result.stderr[-1000:])
    return result.stdout.strip()


def guard(head):
    current = captured(["git", "rev-parse", "HEAD"])
    dirty = captured(["git", "status", "--porcelain"])
    if current != head or dirty:
        raise RuntimeError("canonical clone source identity changed")
    return captured(["git", "rev-parse", "HEAD^{tree}"])


def run(name, argv):
    log = E / "takeover" / f"{name}.log"
    print(f"BROWSER_GATE_START {name}", flush=True)
    with log.open("w") as output:
        process = subprocess.run(argv, cwd=CLONE, env=ENV, stdout=output, stderr=subprocess.STDOUT)
    with (E / "takeover/browser-commands.jsonl").open("a") as receipt:
        receipt.write(json.dumps({
            "time": datetime.now(timezone.utc).isoformat(), "name": name,
            "argv": argv, "cwd": str(CLONE), "exit": process.returncode,
            "log": str(log.relative_to(E)),
        }) + "\n")
    print(f"BROWSER_GATE_EXIT {name} {process.returncode}", flush=True)
    if process.returncode:
        print("\n".join(log.read_text().splitlines()[-40:]), flush=True)
        raise SystemExit(process.returncode)


def main():
    os.umask(0o077)
    unit = Path("/proc/self/cgroup").read_text().strip().rsplit("/", 1)[-1]
    limits = subprocess.run(
        ["systemctl", "--user", "show", unit, "-p", "LoadState",
         "-p", "MemoryMax", "-p", "MemorySwapMax", "-p", "MainPID"],
        text=True, capture_output=True, check=True,
    ).stdout
    assert "MemoryMax=12884901888\n" in limits and "MemorySwapMax=0\n" in limits
    (E / "takeover/browser-unit-readback.txt").write_text(limits)
    head = sys.argv[1]
    assert captured(["git", "status", "--porcelain=v1"]) == ""
    captured(["git", "fetch", str(SOURCE), head])
    captured(["git", "checkout", "--detach", head])
    venv_link = CLONE / ".venv"
    if venv_link.is_symlink():
        assert venv_link.resolve() == VENV
        venv_link.unlink()
    # Git source must be readable by the postgres UID in the bootstrap bind mount.
    # Only tracked source is public-readable; evidence and runtime secrets stay private.
    for record in captured(["git", "ls-files", "--stage", "-z"]).split("\0"):
        if not record:
            continue
        metadata, relative = record.split("\t", 1)
        path = CLONE / relative
        path.chmod(0o755 if metadata.startswith("100755 ") else 0o644)
        for directory in path.parents:
            if directory == CLONE.parent:
                break
            directory.chmod(0o755)
    tree = guard(head)
    for relative in (".omo", ".omo/evidence", ".omo/tmp"):
        directory = CLONE / relative
        directory.mkdir(mode=0o700, exist_ok=True)
        directory.chmod(0o700)
    (E / "takeover/canonical-source.json").write_text(json.dumps({
        "clone": str(CLONE), "committed_head": head, "staged_tree": tree,
        "canonical_git_directory": str(CLONE / ".git"),
    }, indent=2) + "\n")
    if sys.argv[2:] == ["--refresh-only"]:
        print("CANONICAL_SOURCE_REFRESH_ONLY " + head, flush=True)
        return
    ledger_path = CLONE / ".omo/evidence/isolation-ledger-phase1a.json"
    if ledger_path.exists():
        ledger = json.loads(ledger_path.read_text())
        assert ledger["state"] == "open"
        assert ledger["worktree_realpath"] == str(CLONE) and ledger["claims"] == []
        print("BROWSER_GATE_REUSE empty admitted canonical ledger", flush=True)
    else:
        run("canonical-ledger-admission", [
            PYTHON, "ops/testing/isolation_ledger.py", "snapshot",
            "--approved-plan", str(CLONE / "docs/plans/clinic-os-phase1a-approved.md"),
            "--tracked-ci-sidecar", str(CLONE / "docs/plans/clinic-os-phase1a-approved.sha256"),
            "--foundation-sha", "cffbb1900ae2132560f20c27fcf1a514a1ef71aa",
            "--worktree", str(CLONE),
        ])
    for engine in ("chromium", "firefox", "webkit"):
        for suite in ("billing", "locale"):
            guard(head)
            artifact = E / "browser-r2" / f"{suite}-{engine}"
            artifact.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            run(f"canonical-{suite}-{engine}", [
                PYTHON, "-c",
                "from functools import partial; "
                "from ops.testing import renewal_runner as r; "
                "r._provision_database=partial(r._provision_database,memory_backed=True); "
                "raise SystemExit(r.main())", "browser",
                "--suite", suite, "--engine", engine, "--artifact-root", str(artifact),
            ])
            report = json.loads((artifact / "report.json").read_text())
            assert report["pytest_exit"] == 0 and report["tests"] > 0
            assert report["runtime_role"] == "clinic_app"
            assert report["revision_sha"] == head and report["tree_sha"] == tree
            guard(head)
    print("ALL_CANONICAL_BROWSERS_PASS", flush=True)


if __name__ == "__main__":
    main()

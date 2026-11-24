"""Exercise both full verifier scripts against every Wave H projection fault."""

from __future__ import annotations

import itertools
import json
import os
import shutil
import subprocess
import sys
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import psycopg
from psycopg import sql

if TYPE_CHECKING:
    from collections.abc import Iterator

WAVE_H = "4f8d2454987ca1d083ead53a7501126d647a89bc"
type Row = dict[str, object]
type Projection = list[tuple[str, Row]]


def _faults(table: str, originals: Projection) -> Iterator[dict[str, object]]:
    for key, row in originals:
        for column, value in row.items():
            replacement = "__changed__" if value is None else None
            yield {
                "table": table,
                "description": f"{column} in {key}",
                "rows": [(key, {**row, column: replacement})],
            }
    for (left, left_row), (right, right_row) in itertools.combinations(originals, 2):
        yield {
            "table": table,
            "description": f"swap {left} and {right}",
            "rows": [(left, right_row), (right, left_row)],
        }


@contextmanager
def _worker(
    worktree: Path, worker: Path, attestation: Path
) -> Iterator[subprocess.Popen[str]]:
    env = {
        **os.environ,
        "PYTHONPATH": str(worktree),
        "UPGRADE_VERIFIED_FILE": str(attestation),
    }
    with subprocess.Popen(  # noqa: S603 - fixed interpreter and test script
        [str(worktree / ".venv/bin/python"), str(worker)],
        cwd=worktree,
        env=env,
        text=True,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    ) as process:
        try:
            yield process
        finally:
            if process.stdin is not None:
                process.stdin.close()


def _verdict(process: subprocess.Popen[str], fault: dict[str, object]) -> Row:
    assert process.stdin is not None
    assert process.stdout is not None
    process.stdin.write(json.dumps(fault) + "\n")
    process.stdin.flush()
    reply = process.stdout.readline()
    if not reply:
        message = "full verifier worker stopped without a verdict"
        raise RuntimeError(message)
    verdict: Row = json.loads(reply)
    return verdict


def _check_clean(result: Row, *, attested: bool) -> None:
    if result["status"] != 0 or result["attested"] is not attested:
        message = f"full verifier refused the clean sibling: {result['failures']}"
        raise AssertionError(message)


def _check_base(worktree: Path) -> None:
    git = shutil.which("git")
    assert git is not None
    head = subprocess.check_output(  # noqa: S603 - fixed Git command and base revision
        [git, "-C", str(worktree), "rev-parse", "HEAD"], text=True
    ).strip()
    status = subprocess.check_output(  # noqa: S603 - fixed Git status command
        [git, "-C", str(worktree), "status", "--porcelain", "--untracked-files=no"],
        text=True,
    )
    if head != WAVE_H or status:
        message = "differential requires a clean exact Wave H worktree"
        raise RuntimeError(message)


def main() -> None:
    """Run the clean sibling and every generated fault without publishing."""
    if os.environ.get("CLINIC_DATA_MODE") != "synthetic":
        message = "full verifier differential only runs on synthetic rehearsals"
        raise RuntimeError(message)
    base = Path(sys.argv[1]).resolve()
    lane = Path.cwd()
    _check_base(base)
    receipt = json.loads(Path(os.environ["UPGRADE_SEED_FILE"]).read_text())
    snapshots = receipt["ai"]["tables"]
    if not snapshots:
        message = "full verifier differential requires the native AI fixture"
        raise RuntimeError(message)
    worker = Path(__file__).with_name("wave_h_entry_worker.py").resolve()
    out = Path(os.environ["UPGRADE_VERIFIED_FILE"]).parent
    with ExitStack() as stack:
        old = stack.enter_context(_worker(base, worker, out / "differential-base.json"))
        new = stack.enter_context(_worker(lane, worker, out / "differential-lane.json"))
        _check_clean(_verdict(old, {}), attested=False)
        _check_clean(_verdict(new, {}), attested=True)
        checked = 0
        with psycopg.connect(
            os.environ["UPGRADE_ADMIN_DATABASE_URL"], autocommit=True
        ) as admin:
            for table, snapshot in sorted(snapshots.items()):
                keys = snapshot["rows"]
                originals = [
                    (key, {column: row[column] for column in snapshot["columns"]})
                    for key, row in admin.execute(
                        sql.SQL("SELECT id::text,to_jsonb(t) FROM {} t").format(
                            sql.Identifier("clinic_app", table)
                        )
                    )
                    if key in keys
                ]
                assert {key for key, _ in originals} == keys.keys()
                for fault in _faults(table, originals):
                    base_result = _verdict(old, fault)
                    lane_result = _verdict(new, fault)
                    if base_result["status"] == 1 and (
                        lane_result["status"] != 1 or lane_result["attested"]
                    ):
                        message = (
                            f"{table}: {fault['description']}: lane accepted fault"
                        )
                        raise AssertionError(message)
                    checked += 1
                sys.stdout.write(f"Wave H differential: {table}: {checked} cases\n")
                sys.stdout.flush()
        assert checked > 0
        _check_clean(_verdict(old, {}), attested=False)
        _check_clean(_verdict(new, {}), attested=True)
        sys.stdout.write(f"Wave H full-entry differential passed: {checked} cases\n")


if __name__ == "__main__":
    main()

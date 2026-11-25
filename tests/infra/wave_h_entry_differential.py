"""Exercise both full verifier scripts against every Wave H projection fault."""

from __future__ import annotations

import hashlib
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
    worktree: Path, worker: Path, attestation: Path, overrides: dict[str, str]
) -> Iterator[subprocess.Popen[str]]:
    env = {
        **os.environ,
        **overrides,
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


def _projection(admin: psycopg.Connection, table: str, snapshot: Row) -> Projection:
    keys = snapshot["rows"]
    columns = snapshot["columns"]
    assert isinstance(keys, dict)
    assert isinstance(columns, list)
    originals = [
        (key, {column: row[column] for column in columns})
        for key, row in admin.execute(
            sql.SQL("SELECT id::text,to_jsonb(t) FROM {} t").format(
                sql.Identifier("clinic_app", table)
            )
        )
        if key in keys
    ]
    assert {
        key: hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest()
        for key, row in originals
    } == keys
    return originals


def _refuse(
    old: subprocess.Popen[str],
    new: subprocess.Popen[str],
    table: str,
    base_fault: Row,
    lane_fault: Row,
) -> None:
    for label, result in (
        ("Wave H", _verdict(old, base_fault)),
        ("lane", _verdict(new, lane_fault)),
    ):
        failures = result["failures"]
        assert isinstance(failures, list)
        if (
            result["status"] != 1
            or result["attested"]
            or not any(str(line).endswith(f"preserved:{table}") for line in failures)
        ):
            message = f"{label}: {table}: missing digest refusal: {result}"
            raise AssertionError(message)


def main() -> None:
    """Run the clean sibling and every generated fault without publishing."""
    if os.environ.get("CLINIC_DATA_MODE") != "synthetic":
        message = "full verifier differential only runs on synthetic rehearsals"
        raise RuntimeError(message)
    base = Path(sys.argv[1]).resolve()
    lane = Path.cwd()
    _check_base(base)
    old_env = json.loads(Path(os.environ["UPGRADE_WAVE_H_NATIVE_ENV"]).read_text())
    base_snapshots = json.loads(Path(old_env["UPGRADE_SEED_FILE"]).read_text())["ai"][
        "tables"
    ]
    snapshots = json.loads(Path(os.environ["UPGRADE_SEED_FILE"]).read_text())["ai"][
        "tables"
    ]
    assert len(base_snapshots) == 59
    assert base_snapshots.keys() == snapshots.keys()
    base_probes = json.loads(Path(old_env["UPGRADE_WAVE_H_PROBE_CASES"]).read_text())
    probes = json.loads(Path(os.environ["UPGRADE_WAVE_H_PROBE_CASES"]).read_text())
    assert (
        set(base_probes)
        == set(probes)
        == {
            "_baseline_approvals",
            "_baseline_retained",
        }
    )
    for phase in probes:
        assert base_probes[phase].keys() == probes[phase].keys() == snapshots.keys()
    worker = Path(__file__).with_name("wave_h_entry_worker.py").resolve()
    out = Path(os.environ["UPGRADE_VERIFIED_FILE"]).parent
    with ExitStack() as stack:
        old = stack.enter_context(
            _worker(base, worker, out / "differential-base.json", old_env)
        )
        new = stack.enter_context(
            _worker(lane, worker, out / "differential-lane.json", {})
        )
        _check_clean(_verdict(old, {}), attested=False)
        _check_clean(_verdict(new, {}), attested=True)
        checked = 0
        with (
            psycopg.connect(old_env["UPGRADE_ADMIN_DATABASE_URL"]) as old_admin,
            psycopg.connect(os.environ["UPGRADE_ADMIN_DATABASE_URL"]) as admin,
        ):
            for table, snapshot in sorted(snapshots.items()):
                reference = base_snapshots[table]
                assert set(reference["columns"]) == set(snapshot["columns"])
                columns = sorted(reference["columns"])
                old_rows = _projection(old_admin, table, reference)
                new_rows = _projection(admin, table, snapshot)
                assert len(old_rows) == len(new_rows)
                old_rows = [
                    (key, {column: row[column] for column in columns})
                    for key, row in old_rows
                ]
                new_rows = [
                    (key, {column: row[column] for column in columns})
                    for key, row in new_rows
                ]
                for base_fault, fault in zip(
                    _faults(table, old_rows), _faults(table, new_rows), strict=True
                ):
                    _refuse(old, new, table, base_fault, fault)
                    checked += 1
                for phase in probes:
                    faults = []
                    for case, clean_rows in (
                        (base_probes[phase][table], old_rows),
                        (probes[phase][table], new_rows),
                    ):
                        ((key, modified),) = case["rows"]
                        projected = {column: modified[column] for column in columns}
                        clean = dict(clean_rows)[key]
                        assert {
                            column
                            for column in columns
                            if clean[column] != projected[column]
                        } == {case["column"]}
                        faults.append({"table": table, "rows": [(key, projected)]})
                    _refuse(old, new, table, *faults)
                sys.stdout.write(f"Wave H differential: {table}: {checked} cases\n")
                sys.stdout.flush()
        assert checked > 0
        _check_clean(_verdict(old, {}), attested=False)
        _check_clean(_verdict(new, {}), attested=True)
        sys.stdout.write(f"Wave H full-entry differential passed: {checked} cases\n")
        sys.stdout.write("Wave H probe-window differential passed: 118 cases\n")


if __name__ == "__main__":
    main()

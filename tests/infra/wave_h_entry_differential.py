"""Run native F7 inbox verification and differential projection checks."""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import shutil
import subprocess
import sys
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import psycopg
from ops.testing.f7_replay_snapshot import restore, snapshot_digest
from psycopg import sql

if TYPE_CHECKING:
    from collections.abc import Iterator

NATIVE_F7 = "ab71193c110a3b579728d2811d28dad471875390"
type Row = dict[str, object]
type Projection = list[tuple[str, Row]]


@dataclass(frozen=True)
class Replay:
    environment: dict[str, str]
    digest: str
    tables: dict[str, Row]
    probes: dict[str, dict[str, Row]]
    attempt: str


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
        [
            git,
            "--no-optional-locks",
            "-C",
            str(worktree),
            "status",
            "--porcelain",
            "--untracked-files=no",
        ],
        text=True,
    )
    if head != NATIVE_F7 or status:
        message = "differential requires a clean exact F7 worktree"
        raise RuntimeError(message)


def _projection(
    admin: psycopg.Connection, table: str, snapshot: Row, replacement: Row | None = None
) -> Projection:
    keys = snapshot["rows"]
    columns = snapshot["columns"]
    assert isinstance(keys, dict)
    assert isinstance(columns, list)
    relation = sql.Identifier("clinic_app", table)
    if replacement is None:
        cursor = admin.execute(
            sql.SQL("SELECT id::text,to_jsonb(t) FROM {} t").format(relation)
        )
    else:
        rows = replacement["rows"]
        assert isinstance(rows, list)
        ((key, row),) = rows
        cursor = admin.execute(
            sql.SQL(
                "SELECT id::text,CASE WHEN id::text=%s THEN to_jsonb(t) || %s::jsonb "
                "ELSE to_jsonb(t) END FROM {} t"
            ).format(relation),
            (key, json.dumps(row)),
        )
    originals = [
        (key, {column: row[column] for column in columns}) for key, row in cursor
    ]
    actual = {
        key: hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest()
        for key, row in originals
    }
    if actual != keys or len(originals) != snapshot["count"]:
        message = f"preserved:{table}"
        raise ValueError(message)
    return originals


def _compare(
    table: str,
    base_fault: Row,
    lane_fault: Row,
    reference: Row,
    snapshot: Row,
) -> Row | None:
    base_rejected = _rejects(reference, base_fault)
    lane_rejected = _rejects(snapshot, lane_fault)
    if not base_rejected or not lane_rejected:
        return {
            "table": table,
            "case": lane_fault["description"],
            "native_rejected": base_rejected,
            "lane_rejected": lane_rejected,
            "base_reject_lane_pass": base_rejected and not lane_rejected,
        }
    return None


def _rejects(snapshot: Row, fault: Row) -> bool:
    keys = snapshot["rows"]
    rows = fault["rows"]
    assert isinstance(keys, dict)
    assert isinstance(rows, list)
    return any(
        hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest()
        != keys[key]
        for key, row in rows
    )


def _mutate_byte(rows: Projection, attempt: str) -> Row:
    for key, row in rows:
        if key != attempt:
            continue
        for column, value in row.items():
            if column.startswith("id") or not isinstance(value, str) or not value:
                continue
            if not value.isascii():
                continue
            changed = ("A" if value[0] != "A" else "B") + value[1:]
            return {"rows": [(key, {**row, column: changed})]}
    message = "no seeded inbox attempt string was available for the byte mutation"
    raise RuntimeError(message)


def _run(base: Path, report: Row, differences: list[Row]) -> None:
    old_env = json.loads(Path(os.environ["UPGRADE_WAVE_H_NATIVE_ENV"]).read_text())
    old_seed = json.loads(Path(old_env["UPGRADE_SEED_FILE"]).read_text())
    lane_seed = json.loads(Path(os.environ["UPGRADE_SEED_FILE"]).read_text())
    base_snapshots = old_seed["ai"]["tables"]
    snapshots = lane_seed["ai"]["tables"]
    native_attempt = old_seed["inbox"]["cases"]["r7_inflight"]["a"]
    lane_attempt = lane_seed["inbox"]["cases"]["r7_inflight"]["a"]
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
    lane_env = {"UPGRADE_ADMIN_DATABASE_URL": os.environ["UPGRADE_ADMIN_DATABASE_URL"]}
    native_digest = snapshot_digest(old_env["UPGRADE_ADMIN_DATABASE_URL"])
    lane_digest = snapshot_digest(lane_env["UPGRADE_ADMIN_DATABASE_URL"])
    with (
        restore(old_env, "native_e2e", native_digest) as native,
        _worker(base, worker, out / "differential-base.json", native) as old,
    ):
        _check_clean(_verdict(old, {}), attested=False)
        with psycopg.connect(native["UPGRADE_ADMIN_DATABASE_URL"]) as admin:
            terminal = admin.execute(
                "SELECT status, provider_reference IS NOT NULL "
                "FROM clinic_app.comms_integrationoperation WHERE id = %s",
                (native_attempt,),
            ).fetchone()
        assert terminal == ("succeeded", True), terminal
    report["native_inbox"] = {
        "result": "PASS",
        "snapshot_sha256": native_digest,
        "inflight_terminal": "succeeded",
    }
    report["projection_snapshot_sha256"] = {
        "native": native_digest,
        "lane": lane_digest,
    }
    report["tables"] = sorted(snapshots)
    _compare_projections(
        report,
        differences,
        Replay(old_env, native_digest, base_snapshots, base_probes, native_attempt),
        Replay(lane_env, lane_digest, snapshots, probes, lane_attempt),
    )


def _compare_projections(
    report: Row,
    differences: list[Row],
    native: Replay,
    lane: Replay,
) -> None:
    with ExitStack() as stack:
        old_env = stack.enter_context(
            restore(native.environment, "native_projection", native.digest)
        )
        lane_env = stack.enter_context(
            restore(lane.environment, "lane_projection", lane.digest)
        )
        checked = 0
        with (
            psycopg.connect(old_env["UPGRADE_ADMIN_DATABASE_URL"]) as old_admin,
            psycopg.connect(lane_env["UPGRADE_ADMIN_DATABASE_URL"]) as admin,
        ):
            for table, snapshot in sorted(lane.tables.items()):
                reference = native.tables[table]
                reference_columns = reference["columns"]
                snapshot_columns = snapshot["columns"]
                assert isinstance(reference_columns, list)
                assert isinstance(snapshot_columns, list)
                assert set(reference_columns) == set(snapshot_columns)
                columns = sorted(reference_columns)
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
                    if difference := _compare(
                        table, base_fault, fault, reference, snapshot
                    ):
                        differences.append(difference)
                    checked += 1
                    report["projection_cases"] = checked
                for phase in lane.probes:
                    faults: list[Row] = []
                    for case, clean_rows in (
                        (native.probes[phase][table], old_rows),
                        (lane.probes[phase][table], new_rows),
                    ):
                        case_rows = case["rows"]
                        assert isinstance(case_rows, list)
                        ((key, modified),) = case_rows
                        projected = {column: modified[column] for column in columns}
                        clean = dict(clean_rows)[key]
                        assert {
                            column
                            for column in columns
                            if clean[column] != projected[column]
                        } == {case["column"]}
                        faults.append(
                            {
                                "table": table,
                                "description": f"{phase}: {case['column']} in {key}",
                                "rows": [(key, projected)],
                            }
                        )
                    if difference := _compare(
                        table, faults[0], faults[1], reference, snapshot
                    ):
                        differences.append(difference)
                    report["probe_window_cases"] = (
                        int(str(report["probe_window_cases"])) + 1
                    )
                sys.stdout.write(f"Wave H differential: {table}: {checked} cases\n")
                sys.stdout.flush()
                if table == "ai_actionexecution":
                    _prove_projection_red(
                        admin, table, snapshot, next(_faults(table, new_rows))
                    )
                    report["projected_row_mutation"] = "REJECTED"
                if table == "comms_integrationoperation":
                    _prove_projection_red(
                        old_admin,
                        table,
                        reference,
                        _mutate_byte(old_rows, native.attempt),
                    )
                    _prove_projection_red(
                        admin, table, snapshot, _mutate_byte(new_rows, lane.attempt)
                    )
                    report["inbox_attempt_byte_mutation"] = "REJECTED"
        assert checked > 0
        assert report.get("projected_row_mutation") == "REJECTED"
        assert report.get("inbox_attempt_byte_mutation") == "REJECTED"


def _prove_projection_red(
    admin: psycopg.Connection, table: str, snapshot: Row, mutation: Row
) -> None:
    try:
        _projection(admin, table, snapshot, mutation)
    except ValueError as error:
        if str(error) != f"preserved:{table}":
            raise
    else:
        message = f"modified projection was accepted: {table}"
        raise AssertionError(message)


def main() -> int:
    """Record all mismatches and retain progress on a failed rehearsal."""
    differences: list[Row] = []
    report: Row = {
        "result": "FAIL",
        "projection_cases": 0,
        "probe_window_cases": 0,
        "differences": differences,
    }
    try:
        if os.environ.get("CLINIC_DATA_MODE") != "synthetic":
            message = "full verifier differential only runs on synthetic rehearsals"
            raise RuntimeError(message)
        base = Path(sys.argv[1]).resolve()
        _check_base(base)
        _run(base, report, differences)
        assert report["probe_window_cases"] == 118
        if not differences:
            report["result"] = "PASS"
            sys.stdout.write(
                f"Wave H full-entry differential passed: "
                f"{report['projection_cases']} cases\n"
            )
            sys.stdout.write("Wave H probe-window differential passed: 118 cases\n")
    finally:
        report["base_reject_lane_pass_pairs"] = [
            row for row in differences if row["base_reject_lane_pass"] is True
        ]
        Path(os.environ["UPGRADE_WAVE_H_REPORT"]).write_text(
            json.dumps(report) + "\n", encoding="utf-8"
        )
    return 1 if differences else 0


if __name__ == "__main__":
    raise SystemExit(main())

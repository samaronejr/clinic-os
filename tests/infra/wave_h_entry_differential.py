"""Run native F7 inbox verification and differential projection checks."""

from __future__ import annotations

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
from uuid import UUID

import psycopg
from ops.testing.f7_projection_bytes import (
    BinaryProjectionMismatchError,
    Cells,
    Column,
    ReadFault,
    read_projection,
    verify_projection,
)
from ops.testing.f7_replay_snapshot import restore, snapshot_digest
from ops.testing.migration_upgrade_wave_h_floor import Snapshot, records_preserved
from psycopg import sql

if TYPE_CHECKING:
    from collections.abc import Iterator

NATIVE_F7 = "ab71193c110a3b579728d2811d28dad471875390"
type Row = dict[str, object]
type Projection = list[tuple[str, Row]]

INBOX_VALUE_CLASSES = frozenset(
    {"actor_id", "foreign_key", "timestamptz", "jsonb", "bytea", "text"}
)


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
    if not records_preserved(_snapshot(snapshot), originals):
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
    rows = fault["rows"]
    assert isinstance(rows, list)
    source = _snapshot(snapshot)
    expected = {key: source["rows"][key] for key, _ in rows}
    return not records_preserved(
        {"columns": source["columns"], "rows": expected, "count": len(expected)},
        rows,
    )


def _snapshot(value: Row) -> Snapshot:
    columns, rows, count = value["columns"], value["rows"], value["count"]
    assert isinstance(columns, list)
    assert isinstance(rows, dict)
    assert isinstance(count, int)
    return {"columns": columns, "rows": rows, "count": count}


def _mutate_byte(rows: Projection, attempt: str) -> Row:
    for key, row in rows:
        if key != attempt:
            continue
        actor = row["actor_id"]
        assert isinstance(actor, str)
        changed = UUID(int=UUID(actor).int ^ 1)
        return {
            "table": "comms_integrationoperation",
            "description": "one byte of seeded r7_inflight.actor_id",
            "rows": [(key, {**row, "actor_id": str(changed)})],
        }
    message = "seeded r7_inflight operation was absent"
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
    # Each full entry opens connections through more than the admin URL.
    # Rebind only database URLs, never the caller's unrelated service URLs.
    lane_env = {
        key: os.environ[key]
        for key in old_env
        if key.endswith("_URL") and key in os.environ
    }
    lane_env["UPGRADE_ADMIN_DATABASE_URL"] = os.environ["UPGRADE_ADMIN_DATABASE_URL"]
    lane_env["UPGRADE_SEED_FILE"] = os.environ["UPGRADE_SEED_FILE"]
    lane_env["UPGRADE_BASE_OBSERVATION"] = os.environ["UPGRADE_BASE_OBSERVATION"]
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
    table = "comms_integrationoperation"
    actor_results = {}
    for label, tree, environment, digest, snapshot, attempt in (
        ("native", base, old_env, native_digest, base_snapshots[table], native_attempt),
        ("lane", Path.cwd(), lane_env, lane_digest, snapshots[table], lane_attempt),
    ):
        with restore(environment, "actor_fault", digest) as cloned:
            if label == "lane":
                cloned.update(_lane_clone_receipts(environment, cloned, out))
            with psycopg.connect(cloned["UPGRADE_ADMIN_DATABASE_URL"]) as admin:
                fault = _mutate_byte(_projection(admin, table, snapshot), attempt)
            with _worker(tree, worker, out / f"actor-{label}.json", cloned) as process:
                actor_results[label] = _verdict(process, fault)
    report["full_entry_actor_id"] = actor_results
    for label, result in actor_results.items():
        failures = result["failures"]
        assert isinstance(failures, list)
        if (
            result["status"] != 1
            or result["attested"]
            or not any(str(line).endswith(f"preserved:{table}") for line in failures)
        ):
            differences.append(
                {
                    "table": table,
                    "case": "full_entry:r7_inflight.actor_id",
                    "worker": label,
                    "base_reject_lane_pass": actor_results["native"]["status"] == 1
                    and actor_results["lane"]["status"] == 0,
                }
            )
    _compare_projections(
        report,
        differences,
        Replay(old_env, native_digest, base_snapshots, base_probes, native_attempt),
        Replay(lane_env, lane_digest, snapshots, probes, lane_attempt),
    )


def _database_identity(dsn: str) -> list[object]:
    with psycopg.connect(dsn) as admin:
        row = admin.execute(
            "SELECT current_database(), oid FROM pg_database "
            "WHERE datname=current_database()"
        ).fetchone()
    assert row is not None
    return list(row)


def _lane_clone_receipts(
    source: dict[str, str], clone: dict[str, str], out: Path
) -> dict[str, str]:
    """Bind copies of matching lane receipts to a fingerprint-verified restore."""
    original = _database_identity(source["UPGRADE_ADMIN_DATABASE_URL"])
    restored = _database_identity(clone["UPGRADE_ADMIN_DATABASE_URL"])
    assert original != restored
    copies = []
    for variable, filename in (
        ("UPGRADE_SEED_FILE", "actor-lane-seed.json"),
        ("UPGRADE_BASE_OBSERVATION", "actor-lane-base.json"),
    ):
        receipt = json.loads(Path(source[variable]).read_text(encoding="utf-8"))
        if receipt.get("database") != original:
            message = f"actor_fault_source_identity_mismatch:{variable}"
            raise RuntimeError(message)
        receipt["database"] = restored
        copies.append((variable, filename, receipt))

    rebound = {}
    for variable, filename, receipt in copies:
        path = out / filename
        with path.open("x", encoding="utf-8") as stream:
            json.dump(receipt, stream)
        rebound[variable] = str(path)
    return rebound


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
            _check_binary_projections(report, native, lane, old_admin, admin)
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


def _check_binary_projections(
    report: Row,
    native: Replay,
    lane: Replay,
    native_clone: psycopg.Connection,
    lane_clone: psycopg.Connection,
) -> None:
    binary_columns = {}
    typed_mutations: dict[str, str] = {}
    companion_jsonb = None
    for replay, clone in ((native, native_clone), (lane, lane_clone)):
        dsn = replay.environment["UPGRADE_ADMIN_DATABASE_URL"]
        with psycopg.connect(dsn) as source:
            for table, snapshot in sorted(replay.tables.items()):
                required = _snapshot(snapshot)["columns"]
                catalog, values = _binary_coverage(source, clone, table, required)
                if replay is lane:
                    binary_columns[table] = [column.name for column in catalog]
                    if table.startswith("comms_"):
                        _prove_typed_bytes(
                            clone, table, (catalog, values), typed_mutations
                        )
                    elif companion_jsonb is None and any(
                        column.data_type == "jsonb" for column in catalog
                    ):
                        fallback: dict[str, str] = {}
                        _prove_typed_bytes(
                            clone,
                            table,
                            (catalog, values),
                            fallback,
                            kinds=frozenset({"jsonb"}),
                        )
                        companion_jsonb = fallback.get("jsonb")
    if "jsonb" not in typed_mutations and companion_jsonb is not None:
        typed_mutations["jsonb"] = companion_jsonb
        report["inbox_jsonb_scope"] = (
            "No populated JSONB column in the historical comms tables; "
            "JSONB witnessed in the same seeded preservation inventory."
        )
    assert typed_mutations.keys() == INBOX_VALUE_CLASSES, typed_mutations
    report["binary_catalog_columns"] = binary_columns
    report["inbox_binary_mutations_rejected"] = typed_mutations


def _binary_coverage(
    source: psycopg.Connection,
    clone: psycopg.Connection,
    table: str,
    required: list[str],
) -> tuple[list[Column], Cells]:
    expected = read_projection(source, table, required)
    actual = read_projection(clone, table, required)
    if expected[0] != actual[0]:
        message = f"binary_catalog_mismatch:{table}"
        raise ValueError(message)
    verify_projection(table, expected[1], actual[1])
    return actual


def _prove_byte_witness(
    clone: psycopg.Connection,
    table: str,
    fault: ReadFault,
    values: Cells,
    index: int,
) -> None:
    key, column = fault.key, fault.column
    cells = values[key]
    frame = cells[index]
    _, changed = read_projection(clone, table, (), fault=fault)
    expected_change = (
        *cells[:index],
        frame[:-1] + bytes([frame[-1] ^ 1]),
        *cells[index + 1 :],
    )
    if changed != {**values, key: expected_change}:
        message = f"binary_fault_not_isolated:{table}.{column}"
        raise AssertionError(message)
    try:
        verify_projection(table, values, changed)
    except BinaryProjectionMismatchError:
        return
    message = f"byte_mutation_accepted:{table}.{column}"
    raise AssertionError(message)


def _prove_typed_bytes(
    clone: psycopg.Connection,
    table: str,
    projection: tuple[list[Column], Cells],
    witnesses: dict[str, str],
    *,
    kinds: frozenset[str] = INBOX_VALUE_CLASSES,
) -> None:
    catalog, values = projection
    # record_send's one-field frame has a 12-byte header; NULL and an
    # empty payload do not provide a value byte to change.
    header_size = 12
    for index, column in enumerate(catalog):
        if column.name == "actor_id":
            kind = "actor_id"
        elif column.is_fk and column.data_type in {"uuid", "int2", "int4", "int8"}:
            kind = "foreign_key"
        elif column.data_type in {"text", "varchar", "bpchar"}:
            kind = "text"
        else:
            kind = column.data_type
        if kind not in kinds or kind in witnesses:
            continue
        for key, cells in values.items():
            frame = cells[index]
            if len(frame) <= header_size:
                continue
            fault = ReadFault(key, column.name)
            _prove_byte_witness(clone, table, fault, values, index)
            witnesses[kind] = f"{table}.{column.name}"
            break


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
        base = Path(sys.argv[1]).absolute()
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
        with Path(os.environ["UPGRADE_WAVE_H_REPORT"]).open(
            "x", encoding="utf-8"
        ) as stream:
            stream.write(json.dumps(report) + "\n")
    return 1 if differences else 0


if __name__ == "__main__":
    raise SystemExit(main())

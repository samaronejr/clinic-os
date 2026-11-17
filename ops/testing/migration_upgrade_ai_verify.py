"""Non-vacuous historical AI preservation and native permission-stop checks."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict, cast

from django.apps import apps
from django.db import DatabaseError, connection, migrations, transaction
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.state import ModelState
from psycopg import sql

from ops.testing.migration_upgrade_ai_retained import retained_checks

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Mapping

    from django.db.backends.utils import CursorWrapper
    from django.db.migrations.operations.base import Operation


class _Snapshot(TypedDict):
    columns: list[str]
    rows: dict[str, str]
    count: int


class _History(TypedDict):
    tables: dict[str, _Snapshot]
    approvals: list[str]
    pending_evaluations: list[str]
    runs: list[str]
    proposals: list[str]
    turns: list[str]
    invocations: dict[str, str]
    executions: list[str]
    physician: str
    last_version: str
    recording: str
    transcription: str
    retained: dict[str, str]


RETAINED_TABLES = frozenset(
    {
        "ai_modelconfiguration",
        "ai_aiartifact",
        "scribe_chunktranscription",
        "scribe_transcriptversion",
        "scribe_transcriptsegment",
    }
)
FIRST_AI_MIGRATION = 14
LAST_AI_MIGRATION = 19


def _database_operations(operations: Iterable[Operation]) -> Iterator[Operation]:
    for operation in operations:
        if isinstance(operation, migrations.SeparateDatabaseAndState):
            yield from _database_operations(operation.database_operations)
        else:
            yield operation


def _selected_migration(app: str, name: str) -> bool:
    return app == "ai" and FIRST_AI_MIGRATION <= int(name[:4]) <= LAST_AI_MIGRATION


def altered_ai_tables() -> set[str]:
    """Include existing row triggers whose functions are replaced by the upgrade."""
    loader = MigrationLoader(None, ignore_no_migrations=True)
    found = set(RETAINED_TABLES)
    rewritten: set[str] = set()
    installed_sql: list[str] = []
    for (app, name), migration in loader.disk_migrations.items():
        for operation in _database_operations(migration.operations):
            if isinstance(operation, migrations.RunSQL) and isinstance(
                operation.sql, str
            ):
                installed_sql.append(operation.sql)
            if not _selected_migration(app, name):
                continue
            if isinstance(
                operation,
                (
                    migrations.AddField,
                    migrations.AlterField,
                    migrations.AddConstraint,
                    migrations.RemoveConstraint,
                ),
            ):
                state = ModelState.from_model(
                    apps.get_model("ai", operation.model_name)
                )
                found.add(
                    str(
                        state.options.get(
                            "db_table", state.app_label + "_" + state.name.lower()
                        )
                    )
                )
            if isinstance(operation, migrations.RunSQL):
                if not isinstance(operation.sql, str):
                    message = "AI upgrade SQL target discovery requires literal SQL"
                    raise TypeError(message)
                found.update(
                    re.findall(
                        r"\b(?:ALTER TABLE|ON|UPDATE|INSERT INTO)\b"
                        r"\s+clinic_app\.([a-z_][a-z_0-9]*)",
                        operation.sql,
                    )
                )
                rewritten.update(
                    re.findall(
                        r"CREATE (?:OR REPLACE )?FUNCTION clinic_app\.(\w+)\(",
                        operation.sql,
                    )
                )
    for statement in installed_sql:
        found.update(
            table
            for table, function in re.findall(
                r"CREATE (?:CONSTRAINT )?TRIGGER\b[^;]*?\bON\s+clinic_app\.(\w+)"
                r"[^;]*?FOR EACH ROW[^;]*?EXECUTE (?:FUNCTION|PROCEDURE)"
                r"\s+clinic_app\.(\w+)\(",
                statement,
            )
            if function in rewritten
        )
    return found


def coverage_failures(
    tables: Mapping[str, object], required: Iterable[str] | None = None
) -> list[str]:
    """Refuse any missing or empty table instead of accepting a vacuous run."""
    failures = []
    for table in sorted(altered_ai_tables() if required is None else required):
        snapshot = tables.get(table)
        if not isinstance(snapshot, dict) or not snapshot.get("count"):
            failures.append("AI upgrade seed missing populated table: " + table)
    return failures


def _refused(
    statement: str | sql.Composed,
    parameters: list[str],
    reason: str | None = None,
) -> bool:
    """Observe real native guards; even an unexpected accepted write rolls back."""
    try:
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(statement, parameters)
            transaction.set_rollback(True)
    except DatabaseError as error:
        return getattr(error.__cause__, "sqlstate", None) in {"23514", "42501"} and (
            reason is None or reason in str(error.__cause__)
        )
    return False


def _clone(table: str, changes: str) -> sql.Composed:
    return sql.SQL(
        "INSERT INTO clinic_app.{table} SELECT (jsonb_populate_record("
        "NULL::clinic_app.{table},to_jsonb(t)||jsonb_build_object("
        "'id',gen_random_uuid(),{changes}))).* "
        "FROM clinic_app.{table} t WHERE id=%s"
    ).format(table=sql.Identifier(table), changes=sql.SQL(changes))


def _rollout_guards(cursor: CursorWrapper, history: _History) -> dict[str, bool]:
    """Keep valid offline work and reject invalid work on every rollout binding."""
    cursor.execute(
        "SELECT c.relname,p.proname,t.tgenabled FROM pg_trigger t "
        "JOIN pg_class c ON c.oid=t.tgrelid "
        "JOIN pg_proc p ON p.oid=t.tgfoid "
        "WHERE t.tgname='evaluation_rollout_guard'"
    )
    shared = "ai_rollout_guard"
    assertions = {
        "rollout_exact_seven_bindings": set(cursor.fetchall())
        == {
            (table, shared, "O")
            for table in (
                "ai_gatewayswitch",
                "ai_evaluationrating",
                "ai_modelchangerequest",
                "ai_aiinvocation",
                "ai_agentrun",
                "ai_actionexecution",
            )
        }
        | {("ai_evaluationrun", "ai_evaluation_rollout_guard", "O")}
    }
    for pending in history["pending_evaluations"]:
        with transaction.atomic(), connection.cursor() as probe:
            probe.execute(_clone("ai_evaluationrun", "'stage','offline'"), [pending])
            assertions["rollout_offline_evaluation_admitted:" + pending] = (
                probe.rowcount == 1
            )
            probe.execute("SET CONSTRAINTS ALL IMMEDIATE")
            transaction.set_rollback(True)
        assertions["rollout_table_refused:ai_evaluationrun:" + pending] = _refused(
            _clone("ai_evaluationrun", "'stage','shadow'"),
            [pending],
            "rollout receipt denied",
        )
    assertions["rollout_table_refused:ai_modelchangerequest"] = _refused(
        _clone(
            "ai_modelchangerequest",
            "'status','requested','approved_by_id',NULL,'approval_ref','',"
            "'decided_at',NULL,'stage','reviewed_live','exposure','human_review'",
        ),
        [history["approvals"][0]],
        "rollout stage evidence required",
    )
    rating = next(iter(history["tables"]["ai_evaluationrating"]["rows"]))
    assertions["rollout_table_refused:ai_evaluationrating"] = _refused(
        _clone("ai_evaluationrating", "'user_id',NULL"),
        [rating],
        "evaluation rating denied",
    )
    assertions["rollout_table_refused:ai_agentrun"] = _refused(
        _clone("ai_agentrun", "'created_at',statement_timestamp()"),
        [history["runs"][0]],
    )
    assertions["rollout_table_refused:ai_aiinvocation"] = _settlement_refused(
        history["invocations"]["front_desk:reserved"]
    )
    assertions["rollout_table_refused:ai_actionexecution"] = _refused(
        "UPDATE clinic_app.ai_actionexecution "
        "SET send_started_at=statement_timestamp() WHERE id=%s",
        [history["executions"][0]],
    )
    with transaction.atomic(), connection.cursor() as probe:
        probe.execute("SELECT generation FROM clinic_app.ai_gatewayswitch WHERE id=1")
        original = probe.fetchone()
        probe.execute(
            "UPDATE clinic_app.ai_gatewayswitch SET generation=generation+1000000 "
            "WHERE id=1 RETURNING generation"
        )
        assertions["rollout_table_refused:ai_gatewayswitch"] = (
            probe.fetchone() == original
        )
        transaction.set_rollback(True)
    return assertions


def _settlement_refused(invocation: str) -> bool:
    """Meet output-before-success before testing the original rollout fence."""
    try:
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.current_user_id',user_id::text,true) "
                "FROM clinic_app.ai_aiinvocation WHERE id=%s",
                [invocation],
            )
            cursor.execute(
                "INSERT INTO clinic_app.ai_aiartifact "
                "(id,organization_id,clinic_id,user_id,patient_enrollment_id,purpose,"
                "method,kind,content,retention_class,expires_at,"
                "invocation_id,created_at) "
                "SELECT gen_random_uuid(),a.organization_id,a.clinic_id,a.user_id,"
                "a.patient_enrollment_id,a.purpose,a.method,'output',a.content,"
                "'review_draft',a.expires_at,i.id,statement_timestamp() "
                "FROM clinic_app.ai_aiinvocation i JOIN clinic_app.ai_aiartifact a "
                "ON a.id=i.inputs_ref_id WHERE i.id=%s",
                [invocation],
            )
            cursor.execute(
                "UPDATE clinic_app.ai_aiinvocation SET status='succeeded',"
                "settled_at=statement_timestamp() WHERE id=%s",
                [invocation],
            )
            transaction.set_rollback(True)
    except DatabaseError as error:
        return getattr(
            error.__cause__, "sqlstate", None
        ) == "23514" and "reviewed rollout required" in str(error.__cause__)
    return False


def _preserved(cursor: CursorWrapper, tables: dict[str, _Snapshot]) -> dict[str, bool]:
    assertions: dict[str, bool] = {}
    for table, snapshot in tables.items():
        if table.startswith("analytics_"):
            cursor.execute("SET LOCAL ROLE clinic_resolver")
        cursor.execute(
            sql.SQL(
                "SELECT id::text,to_jsonb(t) FROM clinic_app.{} t ORDER BY id"
            ).format(sql.Identifier(table))
        )
        actual = {}
        for key, raw in cursor.fetchall():
            record = json.loads(raw)
            actual[key] = hashlib.sha256(
                json.dumps(
                    {column: record[column] for column in snapshot["columns"]},
                    sort_keys=True,
                ).encode()
            ).hexdigest()
        assertions["preserved:" + table] = (
            actual == snapshot["rows"] and len(actual) == snapshot["count"]
        )
        cursor.execute("RESET ROLE")
    cursor.execute(
        "SELECT count(*) FROM pg_constraint WHERE connamespace="
        "'clinic_app'::regnamespace AND NOT convalidated "
        "AND conrelid=ANY(%s::regclass[])",
        [["clinic_app." + table for table in tables]],
    )
    assertions["all_seeded_constraints_validated"] = cursor.fetchone() == (0,)
    cursor.execute(
        "SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal "
        "AND tgenabled<>'O' AND tgrelid=ANY(%s::regclass[])",
        [["clinic_app." + table for table in tables]],
    )
    assertions["all_seeded_guards_enabled"] = cursor.fetchone() == (0,)
    return assertions


def _defaults(cursor: CursorWrapper) -> dict[str, bool]:
    assertions: dict[str, bool] = {}
    for table, predicate in (
        ("ai_gatewayswitch", "generation=0"),
        (
            "ai_modelchangerequest",
            "stage='shadow' AND exposure='none' "
            "AND switch_generation=0 AND authority_sha256=''",
        ),
        ("ai_evaluationrun", "stage='offline' AND rollout_approval_id IS NULL"),
        ("ai_aiinvocation", "rollout_approval_id IS NULL AND effect_xid=''"),
        ("ai_agentrun", "rollout_approval_id IS NULL"),
        ("ai_frontdeskturn", "rollout_approval_id IS NULL AND source_xid=''"),
        ("ai_actionproposal", "source_xid=''"),
    ):
        cursor.execute(
            sql.SQL("SELECT count(*) FROM clinic_app.{} WHERE NOT ({})").format(
                sql.Identifier(table), sql.SQL(predicate)
            )
        )
        assertions["defaults:" + table] = cursor.fetchone() == (0,)
    return assertions


def _stopped(
    cursor: CursorWrapper, history: _History, markers: Mapping[str, object]
) -> dict[str, bool]:
    assertions: dict[str, bool] = {}
    for approval in history["approvals"]:
        cursor.execute(
            "SELECT clinic_app.ai_rollout_status_for(%s,%s)",
            [str(markers["clinic_id"]), approval],
        )
        assertions["approval_invalidated:" + approval] = cursor.fetchone() == (
            "invalidated",
        )
    cursor.execute("SET LOCAL ROLE clinic_resolver")
    for run in history["runs"]:
        cursor.execute("SELECT clinic_app.ai_rollout_agent_allowed(%s)", [run])
        assertions["run_stopped:" + run] = cursor.fetchone() == (False,)
    for invocation in history["invocations"].values():
        cursor.execute(
            "SELECT clinic_app.ai_effect_current(%s,%s)",
            [invocation, str(markers["clinic_id"])],
        )
        assertions["retained_invocation_stopped:" + invocation] = cursor.fetchone() == (
            False,
        )
    for turn in history["turns"]:
        cursor.execute("SELECT clinic_app.ai_front_desk_current(%s,true)", [turn])
        assertions["turn_stopped:" + turn] = cursor.fetchone() == (False,)
    cursor.execute("RESET ROLE")
    for execution in history["executions"]:
        assertions["send_refused:" + execution] = _refused(
            "UPDATE clinic_app.ai_actionexecution SET "
            "send_started_at=statement_timestamp() WHERE id=%s",
            [execution],
        )
    for proposal in history["proposals"]:
        assertions["proposal_refused:" + proposal] = _refused(
            "UPDATE clinic_app.ai_actionproposal SET state='validated' WHERE id=%s",
            [proposal],
        )
    cursor.execute(
        "SELECT set_config('app.current_user_id',%s,true)", [history["physician"]]
    )
    cursor.execute(
        "SELECT clinic_app.scribe_asr_allowed(%s,consent_id) "
        "FROM clinic_app.scribe_chunktranscription WHERE id=%s",
        [history["recording"], history["transcription"]],
    )
    assertions["asr_assignment_and_consent_positive"] = cursor.fetchone() == (True,)
    for key, invocation in history["invocations"].items():
        if key.endswith(":reserved"):
            assertions["settlement_refused:" + invocation] = _settlement_refused(
                invocation
            )
    assertions["recognized_version_refused"] = _refused(
        "INSERT INTO clinic_app.scribe_transcriptversion "
        "(id,organization_id,session_id,predecessor_id,revision,state,mutation,"
        "coverage,request_id,recognition_index,created_by_id,created_at,"
        "incomplete_acknowledged,latency_ms,write_transaction_id) "
        "SELECT gen_random_uuid(),organization_id,session_id,id,revision+1,"
        "'partial','recognized',coverage,request_id,2,created_by_id,"
        "statement_timestamp(),false,0,txid_current() FROM "
        "clinic_app.scribe_transcriptversion WHERE id=%s",
        [history["last_version"]],
        "transcription invocation permission required",
    )
    return assertions


def verify_ai(markers: Mapping[str, object]) -> list[str]:
    """Verify every original column/ciphertext plus new defaults and stop guards."""
    if "ai_present" not in markers:
        return ["Historical AI schema presence was not recorded"]
    if markers["ai_present"] is not True:
        return []
    history = markers.get("ai")
    if not isinstance(history, dict) or not isinstance(history.get("tables"), dict):
        return ["AI historical schema was present but seed markers are absent"]
    history = cast("_History", history)
    tables = history["tables"]
    failures = coverage_failures(tables)
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.current_tenant',%s,true),"
            "set_config('app.current_user_id',%s,true)",
            [str(markers["organization_id"]), str(markers["owner_id"])],
        )
        assertions = _preserved(cursor, tables)
        assertions.update(_defaults(cursor))
        assertions.update(_stopped(cursor, history, markers))
        assertions.update(retained_checks(history["retained"], upgraded=True))
        assertions.update(_rollout_guards(cursor, history))
    for name, passed in assertions.items():
        if not passed:
            failures.append("AI upgrade assertion failed: " + name)
    report = Path(os.environ["UPGRADE_SEED_FILE"]).with_name("ai-assertions.json")
    report.write_text(
        json.dumps(assertions, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return failures

"""Plant a real recorder row after the last real historical reapply in each owned clone."""
import ast
import json
import os
import traceback
from pathlib import Path
from unittest.mock import patch

import psycopg
import pytest
from django.db import connection, transaction
from django.db.migrations.migration import Migration
from django.db.migrations.recorder import MigrationRecorder

CASES = {
    "tests/teleconsult/test_event_migration.py::test_event_authority_reverse_is_exact_with_populated_v2_events": "_exercise_event_migration",
    "tests/teleconsult/test_mode_migration.py::test_populated_mode_migration_reverses_and_reapplies_exact_catalog": "_exercise_mode_migration",
}
SENTINEL = ("r4d_recorder_diagnostic", "unexpected_committed_row")
GOT = {}


def pytest_collection_finish(session):
    assert set(i.nodeid for i in session.items)==set(CASES)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item):
    assert item.nodeid in CASES
    state = GOT[item.nodeid] = {"inserted":False,"observed_committed":False}
    original = Migration.apply
    def altered(self,*args,**kw):
        result = original(self,*args,**kw)
        # The protected leaf owns the tail of the historical DDL. Match the
        # direct rehearsal call, never migration executor setup or bootstrap.
        if self.app_label=="identity" and self.name=="0051_enterprise_governance" and not state["inserted"]:
            frames=traceback.extract_stack()
            caller=frames[-2].name
            if caller in {"mode_migrations", "_exercise_event_migration"}:
                row=connection.settings_dict["NAME"]
                assert row.startswith("teleconsult_rehearsal_")
                rec=MigrationRecorder(connection)
                assert SENTINEL not in rec.applied_migrations()
                rec.record_applied(*SENTINEL)
                state["inserted"]=True
                def observe_commit():
                    with psycopg.connect(os.environ["MIGRATION_DATABASE_URL"],dbname=row) as peer:
                        state["observed_committed"] = peer.execute(
                            "SELECT EXISTS(SELECT FROM clinic_app.django_migrations WHERE app=%s AND name=%s)",SENTINEL
                        ).fetchone() == (True,)
                transaction.on_commit(observe_commit)
        return result
    with patch.object(Migration,"apply",altered):
        yield


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item,call):
    report=yield
    if report.when=="call":
        state=GOT[item.nodeid]
        expected=False
        if call.excinfo is not None and call.excinfo.type is AssertionError:
            tb=call.excinfo.traceback[-1]
            module=Path(str(item.path))
            fn=next(n for n in ast.parse(module.read_text()).body if isinstance(n,ast.FunctionDef) and n.name==CASES[item.nodeid])
            # Choose the existing last exact assertion, not an earlier baseline.
            matches=[n for n in ast.walk(fn) if isinstance(n,ast.Assert) and
                     ast.unparse(n.test)=="frozenset(MigrationExecutor(connection).loader.applied_migrations) == installed"]
            last=max(matches,key=lambda n:n.lineno)
            expected=str(tb.path)==str(module) and tb.name==fn.name and last.lineno <= tb.lineno+1 <= last.end_lineno
            # Pytest's actual equality explanation must also name the distinct
            # extra row; no exception text, DB data or locals are serialized.
            witnessed="r4d_recorder_diagnostic" in str(call.excinfo.value) and "unexpected_committed_row" in str(call.excinfo.value)
            expected=bool(expected and witnessed and state["inserted"])
        state.update(phase=report.when,outcome=report.outcome,exact_final_recorder_assertion=expected)
    return report


def pytest_sessionfinish(session,exitstatus):
    with Path(os.environ["R4D_RECORDER"]).open("x") as f:
        json.dump({"sha":os.environ["R4D_SHA"],"run_id":os.environ["INVOCATION_ID"],
                   "exit":int(exitstatus),"cases":GOT},f,sort_keys=True)

"""Observe the real gate's return values, never supply them; no DSN/SQL output."""
import json
import os
from pathlib import Path
from unittest.mock import patch

import psycopg
import pytest
from ops.db import posture

ROOT = "tests/infra/test_role_family_posture.py::"
URL = "tests/infra/test_db_posture_url.py::test_db_posture_authenticates_the_supplied_app_database_url"
EXPECTED = {
    URL: [True, False, True],
    ROOT+"test_posture_refuses_transitive_set_only_parameter_authority": [True, False, True],
    ROOT+"test_connectable_secondary_has_independent_routine_grants": [True, False, False, False, True],
    ROOT+"test_machine_only_database_keeps_local_grant_in_scope": [True, False, True],
    ROOT+"test_reachable_database_owner_activates_local_grant": [True, False, True],
    ROOT+"test_connectable_but_uninspectable_database_refuses_posture": [True, False, True],
    ROOT+"test_connection_disabled_database_is_not_skipped": [True, False, True],
}
PARENT = os.environ["TEST_SUPERUSER_DATABASE_URL"]
RECORD = {}
MODULES = {}
REHEARSALS = {"tests/teleconsult/test_mode_migration.py", "tests/teleconsult/test_event_migration.py"}


def cluster(dsn):
    with psycopg.connect(dsn) as db:
        return db.execute(
            "SELECT (SELECT system_identifier FROM pg_catalog.pg_control_system()),"
            " count(*) FILTER (WHERE NOT datallowconn), count(*) FROM pg_catalog.pg_database"
        ).fetchone()


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item):
    mod = item.nodeid.split('::',1)[0]
    if item.nodeid in EXPECTED or mod in REHEARSALS:
        db = item.funcargs['migrated_pg_cluster']
        private, _, _ = cluster(db.super_dsn)
        assert MODULES.setdefault(mod, private)==private
        assert all(ident!=private for key,ident in MODULES.items() if key!=mod)
    if item.nodeid not in EXPECTED:
        yield
        return
    parent_id, parent_closed, _ = cluster(PARENT)
    private_id, private_closed, total = cluster(os.environ["TEST_SUPERUSER_DATABASE_URL"])
    assert parent_id != private_id and parent_closed >= 1 and private_closed == 0 and total >= 5
    target, attribute = (item.module, "_run_posture") if item.nodeid == URL else (posture, "_verify_database_posture")
    actual = getattr(target,attribute)
    observed = []
    def observer(*args,**kwargs):
        result = actual(*args,**kwargs)
        observed.append(result.returncode == 0 if item.nodeid==URL else result)
        return result
    with patch.object(target,attribute,observer):
        yield
    assert observed == EXPECTED[item.nodeid]
    RECORD[item.nodeid] = {"verdicts":observed,"separate_cluster":True,
                           "shared_cluster_had_closed_database":True,"fixture_closed_databases":0}


def pytest_sessionfinish(session,exitstatus):
    with Path(os.environ["R4D_LEGS"]).open("x") as f:
        json.dump({"sha":os.environ["R4D_SHA"],"run_id":os.environ["INVOCATION_ID"],
                   "exit":int(exitstatus),"cases":RECORD,"four_distinct_module_clusters":len(MODULES)==len(set(MODULES.values()))==4},f,sort_keys=True)

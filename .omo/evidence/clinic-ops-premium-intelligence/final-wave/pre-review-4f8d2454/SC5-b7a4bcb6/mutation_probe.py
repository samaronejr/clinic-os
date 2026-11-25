"""Individual catalog-oracle negative controls; no grant is exercised."""
from contextlib import ExitStack, contextmanager
import json
import os
from pathlib import Path
import re
from unittest.mock import patch

import psycopg
from psycopg import sql
import pytest
from django.db import connection
from ops.db import posture

BASE="tests/infra/test_role_family_posture.py::"
NEW=BASE+"test_grant_and_revoke_behind_admin_only_edge"
PREVIOUS=BASE+"test_posture_rejects_inheritance_after_a_role_switch"
GRAPH=BASE+"test_posture_refuses_transitive_set_only_parameter_authority"
OWNER=BASE+"test_migration_resolver_exception_is_not_an_app_exception"
SECOND=BASE+"test_connectable_secondary_has_independent_routine_grants"
UNREADABLE=BASE+"test_connectable_but_uninspectable_database_refuses_posture"
BIND="tests/infra/test_bound_parameters.py::test_runtime_binds_every_envelope_and_webhook_argument"
CASES={
    NEW+"[signal]": "pg_signal_backend", NEW+"[stats]":"pg_read_all_stats",
    NEW+"[createrole]":"r.rolcreaterole", NEW+"[bypassrls]":"r.rolbypassrls",
    NEW+"[export]":"lo_export", NEW+"[ls-dir]":"pg_ls_dir",
    NEW+"[stat-file]":"pg_stat_file", OWNER:"owner_only_exception",
    SECOND:"inspect_primary_only", UNREADABLE:"allow_uninspectable", GRAPH:"membership_edge",
    BIND:"client_binding", PREVIOUS+"[read-file]":"pg_read_file",
    PREVIOUS+"[read-binary-overload]":"pg_read_binary_file",
    PREVIOUS+"[log-directory]":"pg_ls_logdir", PREVIOUS+"[large-object-import]":"lo_import",
    PREVIOUS+"[server-files]":"pg_read_server_files",PREVIOUS+"[settings]":"pg_read_all_settings",
    PREVIOUS+"[write-files]":"pg_write_server_files",PREVIOUS+"[server-program]":"pg_execute_server_program",
    PREVIOUS+"[superuser]":"r.rolsuper",PREVIOUS+"[alter-system]":"parameter_alter_system",
}
_RUN={}


def pytest_collection_modifyitems(items):
    assert len(items)==len(CASES) and {i.nodeid for i in items}==set(CASES)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item):
    kind = CASES[item.nodeid]
    gate = posture.SAFE_ROLE_FAMILY_QUERY.as_string()
    with ExitStack() as stack:
        if kind=="client_binding":
            original=item.module.runtime_connection
            @contextmanager
            def producer():
                def guard(execute,sql_text,params,many,context):
                    if params and sql_text!="SELECT %s::text":
                        raise RuntimeError("only public canary may run in mutated producer")
                    return execute(sql_text,params,many,context)
                with (original() as name, patch.dict(connection.settings_dict["OPTIONS"],server_side_binding=False),
                      connection.execute_wrapper(guard)):
                    yield name
            stack.enter_context(patch.object(item.module,"runtime_connection",producer))
        elif kind=="inspect_primary_only":
            old="WHERE d.datallowconn"
            q=posture.CONNECTABLE_DATABASES
            assert q.count(old)==1
            stack.enter_context(patch.object(posture,"CONNECTABLE_DATABASES",q.replace(old,old+" AND datname=current_database()")))
        elif kind=="allow_uninspectable":
            original=posture._all_databases_safe
            def ignore_uninspectable(*a,**kw):
                try: return original(*a,**kw)
                except psycopg.Error: return True
            stack.enter_context(patch.object(posture,"_all_databases_safe",ignore_uninspectable))
        else:
            if kind.startswith("r."): old,new=kind,"FALSE"
            elif kind=="membership_edge":
                old="JOIN family ON membership.member = family.oid"; new=old+" AND FALSE"
            elif kind=="owner_only_exception": old,new="family.login = 'clinic_owner'","TRUE"
            elif kind=="parameter_alter_system": old,new="'SET, ALTER SYSTEM'","'SET'"
            else:
                old="'"+kind+"'"; new="NULL" # no row can match this IN alternative
            assert gate.count(old)==1
            stack.enter_context(patch.object(posture,"SAFE_ROLE_FAMILY_QUERY",sql.SQL(gate.replace(old,new))))
        yield


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item,call):
    rep=yield
    if rep.when=="call":
        kind=CASES[item.nodeid]
        assertion=("" if call.excinfo is None else str(call.excinfo.value))
        if kind=="client_binding": expected="expected only this SQL template: SELECT $1::text"
        elif kind in {"inspect_primary_only","allow_uninspectable","membership_edge"}: expected="assert not True"
        else: expected="assert (True,) == (False,)"
        _RUN[item.nodeid]={
            "control":kind,"outcome":rep.outcome,"phase":rep.when,
            "expected_assertion":expected,
            "expected_failure":bool(call.excinfo and call.excinfo.type==AssertionError and expected in assertion),
        }
    return rep


def pytest_sessionfinish(session,exitstatus):
    # Record statuses only: no SQL, fixture values, exception text or locals.
    with Path(os.environ["R3_MUTATIONS"]).open("x") as f:
        json.dump({"run_id":os.environ["INVOCATION_ID"],"exit":int(exitstatus),"cases":_RUN},f,sort_keys=True)


if __name__=="__main__":
    for case in CASES: print(case)

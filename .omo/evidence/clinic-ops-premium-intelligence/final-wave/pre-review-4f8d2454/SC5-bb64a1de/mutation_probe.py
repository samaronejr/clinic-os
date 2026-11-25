"""Individual catalog-oracle negative controls; no grant is exercised."""
from contextlib import ExitStack, contextmanager
import json
import os
from pathlib import Path
import hashlib
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
CASES.update({
    BASE+'test_native_memory_routine_direct_grant_refused':'omit_native_memory_routine',
    BASE+'test_unlisted_restricted_routine_in_extension_schema':'check_extension_members_only',
    BASE+'test_machine_only_database_keeps_local_grant_in_scope':'filter_by_app_connect',
    BASE+'test_reachable_database_owner_activates_local_grant':'omit_context_owner_edge',
    BASE+'test_connection_disabled_database_is_not_skipped':'filter_connection_enabled',
    BASE+'test_initially_restricted_native_routine_cannot_be_opened_to_public':'ignore_initial_privileges',
    BASE+'test_private_migrated_helper_does_not_allow_new_runtime_grantee':'ignore_reviewed_audience',
    PREVIOUS+'[replication]':'r.rolreplication',PREVIOUS+'[scan-tables]':'pg_stat_scan_tables',
})
_RUN={}

def change(stack, attr, old, new):
    original=getattr(posture,attr)
    text=original if isinstance(original,str) else original.as_string()
    assert text.count(old)==1, attr
    updated=text.replace(old,new)
    stack.enter_context(patch.object(posture,attr,updated if isinstance(original,str) else sql.SQL(updated)))




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
        elif kind in {"inspect_primary_only","filter_by_app_connect","filter_connection_enabled"}:
            condition={"inspect_primary_only":"datname=current_database()","filter_by_app_connect":"has_database_privilege('clinic_app',oid,'CONNECT')","filter_connection_enabled":"datallowconn"}[kind]
            change(stack,'ALL_DATABASES','ORDER BY','WHERE '+condition+' ORDER BY')
        elif kind=='allow_uninspectable':
            original=posture._inspect_database_authority
            def ignore_uninspectable(*a,**kw):
                try: return original(*a,**kw)
                except psycopg.OperationalError: return True
            stack.enter_context(patch.object(posture,'_inspect_database_authority',ignore_uninspectable))
        elif kind=='check_extension_members_only':
            change(stack,'RESTRICTED_ROUTINES','OR n.oid IN (SELECT extnamespace FROM pg_extension)','')
            change(stack,'RESTRICTED_ROUTINES','n.oid IN (SELECT pronamespace FROM extension_members)','p.oid IN (SELECT oid FROM extension_members)')
        elif kind=='omit_context_owner_edge':
            change(stack,'RESTRICTED_ROUTINES','WHERE datname = pg_catalog.current_database()','WHERE FALSE')
        elif kind=='ignore_initial_privileges':
            change(stack,'RESTRICTED_ROUTINES','i.initprivs IS NOT NULL AND NOT EXISTS','FALSE AND NOT EXISTS')
        elif kind=='ignore_reviewed_audience':
            # Widen only a pinned helper to the staff app, leaving unlisted/native checks intact.
            allowed={k:v|{'clinic_app'} for k,v in posture._routine_exceptions().items()}
            stack.enter_context(patch.object(posture,'_routine_exceptions',lambda:allowed))
        elif kind in {'pg_read_file','pg_read_binary_file','pg_ls_logdir','lo_import','lo_export','pg_ls_dir','pg_stat_file','omit_native_memory_routine'}:
            name=kind if kind!='omit_native_memory_routine' else 'pg_log_backend_memory_contexts'
            change(stack,'RESTRICTED_ROUTINES','WHERE reached.logins IS NOT NULL',"WHERE reached.logins IS NOT NULL AND p.proname <> '"+name+"'")
        elif kind in {'pg_read_all_stats','pg_read_all_settings','pg_stat_scan_tables'}:
            atom="pg_catalog.pg_has_role('pg_monitor', r.oid, 'MEMBER')"
            change(stack,'SAFE_ROLE_FAMILY_QUERY',atom,"("+atom+" AND r.rolname <> '"+kind+"')")
        else:
            if kind.startswith('r.'): old,new=kind,'FALSE'
            elif kind=='membership_edge': old='JOIN family ON membership.member = family.oid'; new=old+' AND FALSE'
            elif kind=='owner_only_exception': old,new="family.login = 'clinic_owner'",'TRUE'
            elif kind=='parameter_alter_system': old,new="'SET, ALTER SYSTEM'","'SET'"
            else: old,new="'"+kind+"'",'NULL'
            change(stack,'SAFE_ROLE_FAMILY_QUERY',old,new)
        yield


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item,call):
    rep=yield
    if rep.when=="call":
        kind=CASES[item.nodeid]
        assertion=("" if call.excinfo is None else str(call.excinfo.value))
        if kind=="client_binding": expected="expected only this SQL template: SELECT $1::text"
        elif kind=="owner_only_exception": expected="assert (True,) == (False,)"
        else: expected="assert not True"
        _RUN[item.nodeid]={
            "control":kind,"outcome":rep.outcome,"phase":rep.when,
            "expected_assertion":expected,
            "expected_failure":bool(call.excinfo and call.excinfo.type==AssertionError and expected in assertion),
        }
    return rep


def pytest_sessionfinish(session,exitstatus):
    # Record statuses only: no SQL, fixture values, exception text or locals.
    with Path(os.environ["R4_MUTATIONS"]).open("x") as f:
        json.dump({"sha":os.environ["R4_SHA"],"run_id":os.environ["INVOCATION_ID"],"exit":int(exitstatus),"cases":_RUN},f,sort_keys=True)


if __name__=="__main__":
    for case in CASES: print(case)

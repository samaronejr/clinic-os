"""Exact migrated ACL, policy, function, column, trigger and constraint posture."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import cast

import pytest
from django.apps import apps
from django.db import connection

from identity.legacy_sql_inventory import migration_definitions

pytestmark = pytest.mark.django_db(transaction=True)
PIN = Path(__file__).with_name("posture.json")


def live_posture() -> dict[str, object]:
    tables = sorted(
        model._meta.db_table for model in apps.get_app_config("workflows").get_models()
    )
    functions = sorted(
        name
        for name, paths in migration_definitions().items()
        if any(path.startswith("apps/workflows/migrations/") for path in paths)
    )
    with connection.cursor() as cursor:
        cursor.execute(
            """SELECT relname,relrowsecurity,relforcerowsecurity,
            relowner::regrole::text FROM pg_class
            WHERE relnamespace='clinic_app'::regnamespace AND relname=ANY(%s)
            ORDER BY relname""",
            [tables],
        )
        flags = cursor.fetchall()
        cursor.execute(
            """SELECT tablename,policyname,permissive,roles,cmd,qual,with_check
            FROM pg_policies WHERE schemaname='clinic_app' AND tablename=ANY(%s)
            ORDER BY tablename,policyname""",
            [tables],
        )
        policies = cursor.fetchall()
        cursor.execute(
            """SELECT table_name,grantee,privilege_type
            FROM information_schema.role_table_grants
            WHERE table_schema='clinic_app' AND table_name=ANY(%s)
            ORDER BY table_name,grantee,privilege_type""",
            [tables],
        )
        grants = cursor.fetchall()
        cursor.execute(
            """SELECT c.relname,a.attname,pg_get_userbyid(x.grantee),x.privilege_type
            FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
            CROSS JOIN LATERAL aclexplode(a.attacl) x
            WHERE c.relnamespace='clinic_app'::regnamespace AND c.relname=ANY(%s)
            ORDER BY 1,2,3,4""",
            [tables],
        )
        columns = cursor.fetchall()
        cursor.execute(
            """SELECT c.relname,a.attname,format_type(a.atttypid,a.atttypmod),
            a.attnotnull FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
            WHERE c.relnamespace='clinic_app'::regnamespace AND c.relname=ANY(%s)
            AND a.attnum>0 AND NOT a.attisdropped ORDER BY 1,2""",
            [tables],
        )
        types = cursor.fetchall()
        cursor.execute(
            """SELECT c.relname,t.tgname,t.tgenabled,pg_get_triggerdef(t.oid)
            FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
            WHERE c.relnamespace='clinic_app'::regnamespace AND c.relname=ANY(%s)
            AND NOT t.tgisinternal ORDER BY 1,2""",
            [tables],
        )
        triggers = cursor.fetchall()
        cursor.execute(
            """SELECT c.relname,k.conname,pg_get_constraintdef(k.oid)
            FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid
            WHERE c.relnamespace='clinic_app'::regnamespace AND c.relname=ANY(%s)
            ORDER BY 1,2""",
            [tables],
        )
        constraints = cursor.fetchall()
        cursor.execute(
            """SELECT n.nspname||'.'||p.proname,
            pg_get_function_identity_arguments(p.oid),p.proowner::regrole::text,
            p.prosecdef,p.provolatile,p.proconfig,pg_get_functiondef(p.oid),
            ARRAY(SELECT COALESCE(r.rolname,'PUBLIC')
            FROM aclexplode(COALESCE(p.proacl,acldefault('f',p.proowner))) x
            LEFT JOIN pg_roles r ON r.oid=x.grantee WHERE x.privilege_type='EXECUTE'
            ORDER BY 1) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
            WHERE n.nspname||'.'||p.proname=ANY(%s) ORDER BY 1,2""",
            [functions],
        )
        routines = [
            (*row[:6], hashlib.sha256(row[6].encode()).hexdigest(), row[7])
            for row in cursor.fetchall()
        ]
    result = {
        "tables": tables,
        "flags": flags,
        "policies": policies,
        "table_grants": grants,
        "column_grants": columns,
        "column_types": types,
        "triggers": triggers,
        "constraints": constraints,
        "functions": routines,
    }
    return cast("dict[str, object]", json.loads(json.dumps(result)))


def test_workflow_posture_is_exact() -> None:
    assert live_posture() == json.loads(PIN.read_text())

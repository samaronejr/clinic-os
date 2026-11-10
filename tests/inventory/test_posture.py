"""Freeze the reviewed live catalog, including bodies and exact policy terms."""

from __future__ import annotations

import hashlib
import json
from typing import Final

import pytest
from apps.inventory.rls import CUSTOM_RLS_TABLES
from django.db import connection

pytestmark = pytest.mark.django_db(transaction=True)
# Captured from the fresh PG16 catalog after reviewing the full snapshot in
# task-61/catalog-reviewed.json. Each category includes all roles and unexpected rows.
# Policies and functions were re-captured when the machine-context refusal moved
# into them; the reviewed delta adds only NO_MACHINE_CONTEXT.
EXPECTED: Final = {
    "flags": "84a7e5f98df00cadbccaec616533fdb9ef2e2282befca48e0cd2abf74fb0c18a",
    # Identity 0051 fences the clinic_app policies; inventory_owner is unchanged.
    "policies": "7de39836340b5fbc5637c4063a317447a4542081e9f590bc35edbe52944b5bc7",
    "grants": "e60d7d454baeebbccd2312d35861244a51d8fbd69ed2ef49045e9f984c93dc8a",
    "columns": "e69fdd5580a038337521c41c7d30e32f024fade4497944851e805fe43fa9d734",
    "types": "44ea7433271df3ec120d30edbe8786106ff59e8a7afc23bfe609273fd45eef38",
    "triggers": "a14dead95a4f86fb2c064d6b6935d28e4b0277b6987025b7bbec2c53cb6030ae",
    "constraints": "bddf9d2aca1185af9001bec9e94f410cc6c4fae9b27d51558ac4de66683d3de1",
    "functions": "c0b55b7ad828479e7b9d56450a8385910a10ee74ed83ba6c5876c28b6c430e81",
}


def live_posture() -> dict[str, list[tuple[object, ...]]]:
    tables = sorted(CUSTOM_RLS_TABLES)
    queries = {
        "flags": """SELECT relname,relrowsecurity,relforcerowsecurity,
        relowner::regrole::text FROM pg_class
        WHERE relnamespace='clinic_app'::regnamespace AND relname=ANY(%s)
        ORDER BY 1""",
        "policies": """SELECT tablename,policyname,permissive,roles,cmd,qual,with_check
        FROM pg_policies WHERE schemaname='clinic_app' AND tablename=ANY(%s)
        ORDER BY 1,2""",
        "grants": """SELECT table_name,grantee,privilege_type
        FROM information_schema.role_table_grants
        WHERE table_schema='clinic_app' AND table_name=ANY(%s)
        ORDER BY 1,2,3""",
        "columns": """SELECT c.relname,a.attname,pg_get_userbyid(x.grantee),
        x.privilege_type FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
        CROSS JOIN LATERAL aclexplode(a.attacl) x
        WHERE c.relnamespace='clinic_app'::regnamespace AND c.relname=ANY(%s)
        ORDER BY 1,2,3,4""",
        "types": """SELECT c.relname,a.attname,format_type(a.atttypid,a.atttypmod),
        a.attnotnull FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
        WHERE c.relnamespace='clinic_app'::regnamespace AND c.relname=ANY(%s)
        AND a.attnum>0 AND NOT a.attisdropped ORDER BY 1,2""",
        "triggers": """SELECT c.relname,t.tgname,t.tgenabled,pg_get_triggerdef(t.oid)
        FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
        WHERE c.relnamespace='clinic_app'::regnamespace AND c.relname=ANY(%s)
        AND NOT t.tgisinternal ORDER BY 1,2""",
        "constraints": """SELECT c.relname,k.conname,pg_get_constraintdef(k.oid)
        FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid
        WHERE c.relnamespace='clinic_app'::regnamespace AND c.relname=ANY(%s)
        ORDER BY 1,2""",
    }
    observed = {}
    with connection.cursor() as cursor:
        for key, query in queries.items():
            cursor.execute(query, [tables])
            observed[key] = cursor.fetchall()
        cursor.execute(
            """SELECT p.proname,pg_get_function_identity_arguments(p.oid),
            p.proowner::regrole::text,p.prosecdef,p.provolatile,p.proparallel,
            p.proconfig,pg_get_functiondef(p.oid),
            ARRAY(SELECT COALESCE(r.rolname,'PUBLIC')
            FROM aclexplode(COALESCE(p.proacl,acldefault('f',p.proowner))) x
            LEFT JOIN pg_roles r ON r.oid=x.grantee WHERE x.privilege_type='EXECUTE'
            ORDER BY 1) FROM pg_proc p
            WHERE p.pronamespace='clinic_app'::regnamespace
            AND p.proname LIKE %s ORDER BY 1,2""",
            ["inventory_%"],
        )
        observed["functions"] = cursor.fetchall()
    return observed


def posture_digests(snapshot: dict[str, list[tuple[object, ...]]]) -> dict[str, str]:
    return {
        key: hashlib.sha256(
            json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        for key, rows in snapshot.items()
    }


def test_inventory_catalog_posture_is_exact() -> None:
    assert posture_digests(live_posture()) == EXPECTED

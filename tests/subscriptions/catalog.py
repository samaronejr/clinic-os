"""Discover exact subscription catalog posture for regeneration and tests."""

import json
from typing import cast

from django.db import connection


def discover_catalog() -> dict[str, list[list[object]]]:
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT p.proname, pg_get_function_identity_arguments(p.oid),
                   pg_get_function_result(p.oid), p.provolatile, p.prosecdef,
                   p.proowner::regrole::text, p.proconfig,
                   ARRAY(SELECT COALESCE(r.rolname,'PUBLIC')
                     FROM aclexplode(COALESCE(p.proacl,acldefault('f',p.proowner))) a
                     LEFT JOIN pg_roles r ON r.oid=a.grantee
                     WHERE a.privilege_type='EXECUTE' ORDER BY 1),
                   has_function_privilege('clinic_app',p.oid,'EXECUTE')
            FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
            WHERE n.nspname='clinic_app' AND p.proname LIKE 'subscription_%'
            ORDER BY 1,2
        """)
        functions = cursor.fetchall()
        cursor.execute("""
            SELECT grantee,table_name,privilege_type
            FROM information_schema.role_table_grants
            WHERE grantee IN ('clinic_app','clinic_resolver')
              AND table_schema='clinic_app' AND table_name LIKE 'subscriptions_%'
            ORDER BY 1,2,3
        """)
        table_grants = cursor.fetchall()
        cursor.execute("""
            SELECT r.rolname,c.relname,a.attname,x.privilege_type
            FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
            JOIN pg_namespace n ON n.oid=c.relnamespace
            CROSS JOIN LATERAL aclexplode(a.attacl) x
            JOIN pg_roles r ON r.oid=x.grantee
            WHERE n.nspname='clinic_app' AND c.relname LIKE 'subscriptions_%'
            ORDER BY 1,2,3,4
        """)
        column_grants = cursor.fetchall()
        cursor.execute("""
            SELECT tablename,policyname,permissive,roles,cmd,qual,with_check
            FROM pg_policies WHERE schemaname='clinic_app'
              AND tablename LIKE 'subscriptions_%' ORDER BY 1,2
        """)
        policies = cursor.fetchall()
        cursor.execute("""
            SELECT c.relname,c.relowner::regrole::text,
                   c.relrowsecurity,c.relforcerowsecurity
            FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='clinic_app' AND c.relname LIKE 'subscriptions_%'
              AND c.relkind='r' ORDER BY 1
        """)
        tables = cursor.fetchall()

    return cast(
        "dict[str, list[list[object]]]",
        json.loads(
            json.dumps(
                {
                    "functions": functions,
                    "table_grants": table_grants,
                    "column_grants": column_grants,
                    "policies": policies,
                    "tables": tables,
                }
            )
        ),
    )

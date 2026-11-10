"""Discover the exact interop catalog posture for regeneration and tests."""

import json
from typing import cast

from apps.interop.migrations._export_sql import RESOLVER_READ_GRANTS
from django.db import connection


def discover_catalog() -> dict[str, list[list[object]]]:
    """Return functions, grants, policies, tables and triggers named ``interop_``."""
    with connection.cursor() as cursor:
        cursor.execute(r"""
            SELECT p.proname, pg_get_function_identity_arguments(p.oid),
                   pg_get_function_result(p.oid), p.provolatile, p.prosecdef,
                   p.proowner::regrole::text, p.proconfig,
                   ARRAY(SELECT COALESCE(r.rolname,'PUBLIC')
                     FROM aclexplode(COALESCE(p.proacl,acldefault('f',p.proowner))) a
                     LEFT JOIN pg_roles r ON r.oid=a.grantee
                     WHERE a.privilege_type='EXECUTE' ORDER BY 1),
                   has_function_privilege('clinic_app',p.oid,'EXECUTE')
            FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
            WHERE n.nspname='clinic_app' AND p.proname LIKE 'interop\_%'
            ORDER BY 1,2
        """)
        functions = cursor.fetchall()
        # The export reader's SELECT grants on other apps' tables belong here too.
        cursor.execute(
            r"""
            SELECT grantee,table_name,privilege_type
            FROM information_schema.role_table_grants
            WHERE grantee IN ('clinic_app','clinic_resolver')
              AND table_schema='clinic_app'
              AND (table_name LIKE 'interop\_%%'
                OR (grantee='clinic_resolver' AND table_name=ANY(%s)))
            ORDER BY 1,2,3
            """,
            [list(RESOLVER_READ_GRANTS)],
        )
        table_grants = cursor.fetchall()
        cursor.execute(r"""
            SELECT r.rolname,c.relname,a.attname,x.privilege_type
            FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
            JOIN pg_namespace n ON n.oid=c.relnamespace
            CROSS JOIN LATERAL aclexplode(a.attacl) x
            JOIN pg_roles r ON r.oid=x.grantee
            WHERE n.nspname='clinic_app' AND c.relname LIKE 'interop\_%'
            ORDER BY 1,2,3,4
        """)
        column_grants = cursor.fetchall()
        cursor.execute(r"""
            SELECT tablename,policyname,permissive,roles,cmd,qual,with_check
            FROM pg_policies WHERE schemaname='clinic_app'
              AND tablename LIKE 'interop\_%' ORDER BY 1,2
        """)
        policies = cursor.fetchall()
        cursor.execute(r"""
            SELECT c.relname,c.relowner::regrole::text,
                   c.relrowsecurity,c.relforcerowsecurity
            FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='clinic_app' AND c.relname LIKE 'interop\_%'
              AND c.relkind='r' ORDER BY 1
        """)
        tables = cursor.fetchall()
        cursor.execute(r"""
            SELECT c.relname,t.tgname,pg_get_triggerdef(t.oid)
            FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
            JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='clinic_app' AND NOT t.tgisinternal
              AND t.tgname LIKE 'interop\_%' ORDER BY 1,2
        """)
        triggers = cursor.fetchall()

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
                    "triggers": triggers,
                }
            )
        ),
    )

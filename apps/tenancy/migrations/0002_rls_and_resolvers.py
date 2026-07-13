"""Enforce tenant RLS and install hardened identity resolvers."""

from typing import ClassVar, Final

from django.db import migrations
from django.db.migrations.operations.base import Operation

from apps.tenancy.rls import (
    TENANT_RLS_TARGETS,
    apply_tenant_rls,
    remove_tenant_rls,
)

FORWARD_SQL: Final = """
GRANT SELECT, INSERT, UPDATE, DELETE
    ON TABLE clinic_app.identity_organization,
             clinic_app.identity_clinic,
             clinic_app.identity_userclinicrole,
             clinic_app.tenancy_tenantprobe
    TO clinic_app;
REVOKE ALL PRIVILEGES ON TABLE clinic_app.identity_user FROM clinic_app;

GRANT SELECT
    ON TABLE clinic_app.identity_user,
             clinic_app.identity_organization,
             clinic_app.identity_clinic,
             clinic_app.identity_userclinicrole
    TO clinic_resolver;

SET LOCAL ROLE clinic_resolver;

CREATE OR REPLACE FUNCTION clinic_app.user_has_org(
    requested_org pg_catalog.uuid
)
RETURNS pg_catalog.bool
LANGUAGE sql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp
AS $function$
    SELECT EXISTS (
        SELECT 1
        FROM clinic_app.identity_userclinicrole AS membership
        WHERE membership.user_id = NULLIF(
            pg_catalog.current_setting('app.current_user_id', true),
            ''
        )::pg_catalog.uuid
          AND membership.organization_id = requested_org
    )
$function$;

CREATE OR REPLACE FUNCTION clinic_app.user_organizations()
RETURNS SETOF pg_catalog.uuid
LANGUAGE sql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp
AS $function$
    SELECT DISTINCT membership.organization_id
    FROM clinic_app.identity_userclinicrole AS membership
    WHERE membership.user_id = NULLIF(
        pg_catalog.current_setting('app.current_user_id', true),
        ''
    )::pg_catalog.uuid
$function$;

CREATE OR REPLACE FUNCTION clinic_app.auth_lookup(
    requested_username pg_catalog.text
)
RETURNS TABLE(
    id pg_catalog.uuid,
    username pg_catalog.varchar(150),
    password pg_catalog.varchar(128),
    is_active pg_catalog.bool
)
LANGUAGE sql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp
AS $function$
    SELECT user_row.id,
           user_row.username,
           user_row.password,
           user_row.is_active
    FROM clinic_app.identity_user AS user_row
    WHERE user_row.username = requested_username
$function$;

CREATE OR REPLACE FUNCTION clinic_app.load_current_user()
RETURNS SETOF clinic_app.identity_user
LANGUAGE sql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
ROWS 1
SET search_path = pg_catalog, clinic_app, pg_temp
AS $function$
    SELECT user_row.*
    FROM clinic_app.identity_user AS user_row
    WHERE user_row.id = NULLIF(
        pg_catalog.current_setting('app.current_user_id', true),
        ''
    )::pg_catalog.uuid
$function$;

REVOKE ALL PRIVILEGES
    ON FUNCTION clinic_app.user_has_org(pg_catalog.uuid)
    FROM PUBLIC;
REVOKE ALL PRIVILEGES
    ON FUNCTION clinic_app.user_organizations()
    FROM PUBLIC;
REVOKE ALL PRIVILEGES
    ON FUNCTION clinic_app.auth_lookup(pg_catalog.text)
    FROM PUBLIC;
REVOKE ALL PRIVILEGES
    ON FUNCTION clinic_app.load_current_user()
    FROM PUBLIC;

GRANT EXECUTE
    ON FUNCTION clinic_app.user_has_org(pg_catalog.uuid)
    TO clinic_app;
GRANT EXECUTE
    ON FUNCTION clinic_app.user_organizations()
    TO clinic_app;
GRANT EXECUTE
    ON FUNCTION clinic_app.auth_lookup(pg_catalog.text)
    TO clinic_app;
GRANT EXECUTE
    ON FUNCTION clinic_app.load_current_user()
    TO clinic_app;

RESET ROLE;
"""

REVERSE_SQL: Final = """
SET LOCAL ROLE clinic_resolver;
DROP FUNCTION IF EXISTS clinic_app.user_has_org(pg_catalog.uuid);
DROP FUNCTION IF EXISTS clinic_app.user_organizations();
DROP FUNCTION IF EXISTS clinic_app.auth_lookup(pg_catalog.text);
DROP FUNCTION IF EXISTS clinic_app.load_current_user();
RESET ROLE;

REVOKE SELECT
    ON TABLE clinic_app.identity_user,
             clinic_app.identity_organization,
             clinic_app.identity_clinic,
             clinic_app.identity_userclinicrole
    FROM clinic_resolver;
GRANT SELECT, INSERT, UPDATE, DELETE
    ON TABLE clinic_app.identity_user
    TO clinic_app;
"""


class Migration(migrations.Migration):
    """Apply reversible database-enforced tenant isolation."""

    dependencies: ClassVar[list[tuple[str, str]]] = [
        ("identity", "0002_userclinicrole_org_clinic_fk"),
        ("tenancy", "0001_initial"),
    ]

    operations: ClassVar[list[Operation]] = [
        migrations.RunSQL(sql=FORWARD_SQL, reverse_sql=REVERSE_SQL),
        *(
            migrations.RunSQL(
                sql=apply_tenant_rls(table, tenant_col),
                reverse_sql=remove_tenant_rls(table, tenant_col),
            )
            for table, tenant_col in sorted(TENANT_RLS_TARGETS)
        ),
    ]

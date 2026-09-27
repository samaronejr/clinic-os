"""Refuse inactive accounts before opening any staff tenant transaction."""

from typing import ClassVar

from django.db import migrations
from django.db.migrations.operations.base import Operation

FORWARD_SQL = """
SET LOCAL ROLE clinic_resolver;
CREATE OR REPLACE FUNCTION clinic_app.user_has_org(requested_org pg_catalog.uuid)
RETURNS pg_catalog.bool
LANGUAGE sql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp
AS $function$
    SELECT EXISTS (
        SELECT 1
        FROM clinic_app.identity_userclinicrole AS membership
        JOIN clinic_app.identity_user AS actor
          ON actor.id = membership.user_id AND actor.is_active
        WHERE membership.user_id = NULLIF(
            pg_catalog.current_setting('app.current_user_id', true), ''
        )::pg_catalog.uuid
          AND membership.organization_id = requested_org
    )
$function$;
REVOKE ALL PRIVILEGES ON FUNCTION clinic_app.user_has_org(pg_catalog.uuid)
    FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.user_has_org(pg_catalog.uuid) TO clinic_app;
RESET ROLE;
"""

REVERSE_SQL = """
SET LOCAL ROLE clinic_resolver;
CREATE OR REPLACE FUNCTION clinic_app.user_has_org(requested_org pg_catalog.uuid)
RETURNS pg_catalog.bool
LANGUAGE sql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp
AS $function$
    SELECT EXISTS (
        SELECT 1
        FROM clinic_app.identity_userclinicrole AS membership
        WHERE membership.user_id = NULLIF(
            pg_catalog.current_setting('app.current_user_id', true), ''
        )::pg_catalog.uuid
          AND membership.organization_id = requested_org
    )
$function$;
REVOKE ALL PRIVILEGES ON FUNCTION clinic_app.user_has_org(pg_catalog.uuid)
    FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.user_has_org(pg_catalog.uuid) TO clinic_app;
RESET ROLE;
"""


class Migration(migrations.Migration):
    """Replace only the tenant-entry resolver, preserving its ACL and signature."""

    dependencies: ClassVar[list[tuple[str, str]]] = [
        ("tenancy", "0004_protected_fields"),
    ]
    operations: ClassVar[list[Operation]] = [
        migrations.RunSQL(FORWARD_SQL, REVERSE_SQL),
    ]

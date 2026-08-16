"""Frozen ACL operations for identity migration 0004."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from django.db.backends.base.schema import BaseDatabaseSchemaEditor
    from django.db.backends.utils import CursorWrapper
    from django.db.migrations.state import StateApps

CONTEXT_ERROR: Final = "identity ACL migration context unavailable"
CONTEXT_FIELD_COUNT: Final = 3

ACL_FORWARD_SQL: Final = """
REVOKE ALL PRIVILEGES ON TABLE clinic_app.identity_organization,
    clinic_app.identity_clinic, clinic_app.identity_userclinicrole,
    clinic_app.django_migrations FROM clinic_app;
GRANT SELECT ON TABLE clinic_app.identity_organization, clinic_app.identity_clinic,
    clinic_app.identity_userclinicrole, clinic_app.django_migrations TO clinic_app;
REVOKE ALL PRIVILEGES ON TABLE clinic_app.identity_user FROM clinic_app;
REVOKE ALL PRIVILEGES (id, password, last_login, is_superuser, username, first_name,
    last_name, email, is_staff, is_active, date_joined)
    ON TABLE clinic_app.identity_user FROM clinic_app;
"""

RESOLVER_SQL: Final = """
CREATE FUNCTION clinic_app.list_active_clinic_physicians(requested_clinic uuid)
RETURNS TABLE(user_id uuid, display_label text)
LANGUAGE sql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp
AS $function$
    SELECT physician.user_id, target.username::text
    FROM clinic_app.identity_userclinicrole AS physician
    JOIN clinic_app.identity_user AS target ON target.id = physician.user_id
    JOIN clinic_app.identity_clinic AS requested
      ON requested.id = physician.clinic_id
     AND requested.organization_id = physician.organization_id
    WHERE physician.clinic_id = requested_clinic
      AND physician.organization_id = NULLIF(
          current_setting('app.current_tenant', true), ''
      )::uuid
      AND physician.role = 'physician'
      AND target.is_active
      AND EXISTS (
          SELECT 1
          FROM clinic_app.identity_userclinicrole AS manager
          JOIN clinic_app.identity_user AS actor ON actor.id = manager.user_id
          WHERE manager.user_id = NULLIF(
              current_setting('app.current_user_id', true), ''
          )::uuid
            AND manager.organization_id = physician.organization_id
            AND manager.clinic_id = requested_clinic
            AND manager.role IN ('owner', 'clinic_admin', 'receptionist')
            AND actor.is_active
      )
    ORDER BY physician.user_id
$function$;
REVOKE ALL PRIVILEGES ON FUNCTION clinic_app.list_active_clinic_physicians(uuid)
    FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.list_active_clinic_physicians(uuid)
    TO clinic_app;
"""

ACL_REVERSE_SQL: Final = """
REVOKE ALL PRIVILEGES ON TABLE clinic_app.identity_organization,
    clinic_app.identity_clinic, clinic_app.identity_userclinicrole,
    clinic_app.django_migrations FROM clinic_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE clinic_app.identity_organization,
    clinic_app.identity_clinic, clinic_app.identity_userclinicrole,
    clinic_app.django_migrations TO clinic_app;
"""


def _saved_context(cursor: CursorWrapper) -> tuple[str, str | None, str | None]:
    cursor.execute(
        "SELECT current_setting('role'), "
        "current_setting('app.current_tenant', true), "
        "current_setting('app.current_user_id', true)"
    )
    row = cursor.fetchone()
    if row is None or not isinstance(row, tuple) or len(row) != CONTEXT_FIELD_COUNT:
        raise RuntimeError(CONTEXT_ERROR)
    role, tenant, user = row
    if (
        not isinstance(role, str)
        or (tenant is not None and not isinstance(tenant, str))
        or (user is not None and not isinstance(user, str))
    ):
        raise RuntimeError(CONTEXT_ERROR)
    return role, tenant, user


def _restore_context(
    cursor: CursorWrapper,
    schema_editor: BaseDatabaseSchemaEditor,
    saved: tuple[str, str | None, str | None],
) -> None:
    role, tenant, user = saved
    cursor.execute("RESET ROLE")
    if role != "none":
        cursor.execute(f"SET ROLE {schema_editor.quote_name(role)}")
    for setting, value in (
        ("app.current_tenant", tenant),
        ("app.current_user_id", user),
    ):
        if value is None:
            cursor.execute(f"RESET {setting}")
        else:
            cursor.execute(
                "SELECT pg_catalog.set_config(%s, %s, true)",
                [setting, value],
            )


def install_runtime_identity_acl(
    _apps: StateApps,
    schema_editor: BaseDatabaseSchemaEditor,
) -> None:
    """Install read-only runtime ACLs and the manager-only catalog."""
    with schema_editor.connection.cursor() as cursor:
        saved = _saved_context(cursor)
        try:
            cursor.execute(ACL_FORWARD_SQL)
            cursor.execute("SET LOCAL ROLE clinic_resolver")
            cursor.execute(RESOLVER_SQL)
        finally:
            _restore_context(cursor, schema_editor, saved)


def remove_runtime_identity_acl(
    _apps: StateApps,
    schema_editor: BaseDatabaseSchemaEditor,
) -> None:
    """Restore the foundation ACL while preserving canonicalized data."""
    with schema_editor.connection.cursor() as cursor:
        saved = _saved_context(cursor)
        try:
            cursor.execute("SET LOCAL ROLE clinic_resolver")
            cursor.execute(
                "DROP FUNCTION IF EXISTS clinic_app.list_active_clinic_physicians(uuid)"
            )
            _restore_context(cursor, schema_editor, saved)
            cursor.execute(ACL_REVERSE_SQL)
        finally:
            _restore_context(cursor, schema_editor, saved)

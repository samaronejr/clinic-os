"""Frozen data operations for identity migration 0004."""

from __future__ import annotations

import unicodedata
from typing import TYPE_CHECKING, Final, Never
from uuid import UUID

from django.contrib.auth.validators import UnicodeUsernameValidator
from django.core.exceptions import ValidationError
from django.core.validators import validate_email

if TYPE_CHECKING:
    from django.db.backends.base.schema import BaseDatabaseSchemaEditor
    from django.db.backends.utils import CursorWrapper
    from django.db.migrations.state import StateApps

PREFLIGHT_ERROR: Final = "identity canonicalization preflight failed"
CONTEXT_ERROR: Final = "identity migration context unavailable"
CLEANUP_ERROR: Final = "identity migration helper cleanup failed"
UPDATE_ERROR: Final = "identity canonicalization update failed"
ORGANIZATION_ERROR: Final = "identity organization enumeration failed"
HELPER_SIGNATURE: Final = "clinic_app._phase1a_identity_canonicalization_candidates()"
CONTEXT_FIELD_COUNT: Final = 3
CANDIDATE_FIELD_COUNT: Final = 3
USERNAME_MAX_LENGTH: Final = 150
EMAIL_MAX_LENGTH: Final = 254


def canonicalize_username_for_migration(value: str) -> str:
    """Freeze the username normalization used by migration 0004."""
    return unicodedata.normalize("NFKC", value.strip()).casefold()


def canonicalize_email_for_migration(value: str) -> str:
    """Freeze the email normalization used by migration 0004."""
    return unicodedata.normalize("NFC", value.strip()).casefold()


def _fail_preflight() -> Never:
    raise RuntimeError(PREFLIGHT_ERROR)


def _fail_organization() -> Never:
    raise RuntimeError(ORGANIZATION_ERROR)


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


def _drop_candidate_helper(
    cursor: CursorWrapper,
    schema_editor: BaseDatabaseSchemaEditor,
    saved: tuple[str, str | None, str | None],
) -> None:
    cursor.execute("SET LOCAL ROLE clinic_resolver")
    try:
        cursor.execute(
            "DROP FUNCTION IF EXISTS "
            "clinic_app._phase1a_identity_canonicalization_candidates()"
        )
    finally:
        _restore_context(cursor, schema_editor, saved)
    cursor.execute("SELECT to_regprocedure(%s)", [HELPER_SIGNATURE])
    if cursor.fetchone() != (None,):
        raise RuntimeError(CLEANUP_ERROR)


def _canonical_candidates(
    rows: list[tuple[object, ...]],
) -> list[tuple[UUID, str, str, str, str]]:
    frozen: list[tuple[UUID, str, str, str, str]] = []
    ids: set[UUID] = set()
    usernames: set[str] = set()
    emails: set[str] = set()
    username_validator = UnicodeUsernameValidator()
    for row in rows:
        if len(row) != CANDIDATE_FIELD_COUNT:
            _fail_preflight()
        user_id, original_username, original_email = row
        if (
            not isinstance(user_id, UUID)
            or not isinstance(original_username, str)
            or not isinstance(original_email, str)
            or user_id in ids
        ):
            _fail_preflight()
        username = canonicalize_username_for_migration(original_username)
        email = canonicalize_email_for_migration(original_email)
        if not username or len(username) > USERNAME_MAX_LENGTH:
            _fail_preflight()
        try:
            username_validator(username)
            if email:
                if len(email) > EMAIL_MAX_LENGTH:
                    _fail_preflight()
                validate_email(email)
        except ValidationError:
            _fail_preflight()
        if username in usernames or (email and email in emails):
            _fail_preflight()
        ids.add(user_id)
        usernames.add(username)
        if email:
            emails.add(email)
        frozen.append((user_id, original_username, original_email, username, email))
    return frozen


def canonicalize_identities(
    _apps: StateApps,
    schema_editor: BaseDatabaseSchemaEditor,
) -> None:
    """Canonicalize every global identity only after complete preflight."""
    with schema_editor.connection.cursor() as cursor:
        saved = _saved_context(cursor)
        helper_created = False
        try:
            cursor.execute(
                "LOCK TABLE clinic_app.identity_user IN ACCESS EXCLUSIVE MODE"
            )
            cursor.execute("SET LOCAL ROLE clinic_resolver")
            try:
                cursor.execute(
                    """
                    CREATE FUNCTION
                        clinic_app._phase1a_identity_canonicalization_candidates()
                    RETURNS TABLE(id uuid, username varchar(150), email varchar(254))
                    LANGUAGE sql STABLE PARALLEL UNSAFE SECURITY DEFINER
                    SET search_path = pg_catalog, clinic_app, pg_temp
                    AS $function$
                        SELECT id, username, email
                        FROM clinic_app.identity_user
                        ORDER BY id
                    $function$;
                    REVOKE ALL ON FUNCTION
                        clinic_app._phase1a_identity_canonicalization_candidates()
                        FROM PUBLIC, clinic_app;
                    GRANT EXECUTE ON FUNCTION
                        clinic_app._phase1a_identity_canonicalization_candidates()
                        TO clinic_owner;
                    """
                )
                helper_created = True
            finally:
                _restore_context(cursor, schema_editor, saved)
            cursor.execute(
                "SELECT * FROM "
                "clinic_app._phase1a_identity_canonicalization_candidates()"
            )
            raw_rows = cursor.fetchall()
            if not isinstance(raw_rows, list) or not all(
                isinstance(row, tuple) for row in raw_rows
            ):
                _fail_preflight()
            for candidate in _canonical_candidates(raw_rows):
                user_id, original_username, original_email, username, email = candidate
                cursor.execute(
                    """
                    UPDATE clinic_app.identity_user
                    SET username = %s, email = %s
                    WHERE id = %s AND username = %s AND email = %s
                    RETURNING id
                    """,
                    [username, email, str(user_id), original_username, original_email],
                )
                if cursor.fetchone() != (user_id,) or cursor.rowcount != 1:
                    raise RuntimeError(UPDATE_ERROR)
        finally:
            if helper_created:
                _drop_candidate_helper(cursor, schema_editor, saved)
            _restore_context(cursor, schema_editor, saved)


def collapse_duplicate_roles(
    _apps: StateApps,
    schema_editor: BaseDatabaseSchemaEditor,
) -> None:
    """Retain the lowest UUID for each baseline-valid exact role tuple."""
    with schema_editor.connection.cursor() as cursor:
        saved = _saved_context(cursor)
        try:
            cursor.execute("SET LOCAL ROLE clinic_resolver")
            try:
                cursor.execute(
                    "SELECT id FROM clinic_app.identity_organization ORDER BY id"
                )
                organizations = cursor.fetchall()
            finally:
                _restore_context(cursor, schema_editor, saved)
            if not isinstance(organizations, list):
                _fail_organization()
            for row in organizations:
                if (
                    not isinstance(row, tuple)
                    or len(row) != 1
                    or not isinstance(row[0], UUID)
                ):
                    _fail_organization()
                cursor.execute(
                    "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
                    [str(row[0])],
                )
                cursor.execute(
                    """
                    WITH ranked AS (
                        SELECT id, row_number() OVER (
                            PARTITION BY organization_id, clinic_id, user_id, role
                            ORDER BY id
                        ) AS duplicate_rank
                        FROM clinic_app.identity_userclinicrole
                    )
                    DELETE FROM clinic_app.identity_userclinicrole AS role_row
                    USING ranked
                    WHERE role_row.id = ranked.id AND ranked.duplicate_rank > 1
                    """
                )
                _restore_context(cursor, schema_editor, saved)
        finally:
            _restore_context(cursor, schema_editor, saved)

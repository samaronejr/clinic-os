"""Server-side display preferences: posture, isolation, validation and shell.

The runtime role reads and writes only the row bound to ``app.current_user_id``;
it can never delete a row or write a value outside the closed vocabulary.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Final

import pytest
from apps.identity.models import User, UserPreference
from apps.identity.preferences import (
    DEFAULT_PREFERENCES,
    PreferenceValueError,
    UiPreferences,
    load_ui_preferences,
    save_ui_preferences,
)
from django.db import DatabaseError, connection, transaction
from django.test import Client
from django.utils.translation import gettext

from otp_test_support import current_user_guc, runtime_role
from patient_http_support import receptionist_client

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)

TABLE: Final = "identity_userpreference"
POLICY_EXPRESSION: Final = (
    "(user_id = (NULLIF(current_setting("
    "'app.current_user_id'::text, true), ''::text))::uuid)"
)
OK: Final = 200
BAD_REQUEST: Final = 400
FORBIDDEN: Final = 403
HTML_THEME: Final = re.compile(
    r'<html [^>]*data-theme="([a-z]+)" data-density="([a-z]+)"'
)


def _html_theme(body: str) -> tuple[str, ...]:
    match = HTML_THEME.search(body)
    assert match is not None
    return match.groups()


def _precache(worker: str) -> list[str]:
    match = re.search(r"const PRECACHE = (\[.*?\]);", worker)
    assert match is not None
    urls = json.loads(match.group(1))
    assert isinstance(urls, list)
    return [str(url) for url in urls]


def test_preference_table_is_forced_rls_owned_by_the_owner_role() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT c.relrowsecurity, c.relforcerowsecurity, owner.rolname "
            "FROM pg_catalog.pg_class AS c "
            "JOIN pg_catalog.pg_roles AS owner ON owner.oid = c.relowner "
            "WHERE c.oid = 'clinic_app.identity_userpreference'::regclass"
        )
        posture = cursor.fetchone()
        cursor.execute(
            "SELECT policyname, cmd, roles, permissive, qual, with_check "
            "FROM pg_catalog.pg_policies "
            "WHERE schemaname = 'clinic_app' AND tablename = %s",
            [TABLE],
        )
        policies = cursor.fetchall()
    assert posture == (True, True, "clinic_owner")
    assert policies == [
        (
            "userpreference_owner_only",
            "ALL",
            ["clinic_app"],
            "PERMISSIVE",
            POLICY_EXPRESSION,
            POLICY_EXPRESSION,
        )
    ]


def test_runtime_grants_are_select_insert_and_column_update_only() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT grantee, privilege_type FROM information_schema.role_table_grants "
            "WHERE table_schema = 'clinic_app' AND table_name = %s "
            "AND grantee <> 'clinic_owner'",
            [TABLE],
        )
        table_grants = set(cursor.fetchall())
        cursor.execute(
            "SELECT grantee, column_name FROM information_schema.role_column_grants "
            "WHERE table_schema = 'clinic_app' AND table_name = %s "
            "AND privilege_type = 'UPDATE' AND grantee <> 'clinic_owner'",
            [TABLE],
        )
        column_updates = set(cursor.fetchall())
    assert table_grants == {("clinic_app", "SELECT"), ("clinic_app", "INSERT")}
    assert column_updates == {
        ("clinic_app", "theme"),
        ("clinic_app", "density"),
        ("clinic_app", "updated_at"),
    }


def _user(prefix: str) -> User:
    return User.objects.create(username=f"{prefix}-sintetico")


def test_each_user_reads_and_writes_only_their_own_row() -> None:
    first, second = _user("pref-a"), _user("pref-b")
    with runtime_role():
        with current_user_guc(first.pk):
            assert load_ui_preferences() == DEFAULT_PREFERENCES
            saved = save_ui_preferences(theme="dark", density="compact")
            assert saved == UiPreferences(theme="dark", density="compact")
        with current_user_guc(second.pk):
            assert load_ui_preferences() == DEFAULT_PREFERENCES
            assert UserPreference.objects.count() == 0
            # Updating or re-targeting someone else's row touches nothing.
            assert (
                UserPreference.objects.filter(user_id=first.pk).update(theme="light")
                == 0
            )
            save_ui_preferences(theme="light", density="comfortable")
        with current_user_guc(first.pk):
            assert load_ui_preferences() == UiPreferences(
                theme="dark", density="compact"
            )
            save_ui_preferences(theme="light", density="compact")
            assert load_ui_preferences() == UiPreferences(
                theme="light", density="compact"
            )
    with runtime_role(), transaction.atomic(), connection.cursor() as cursor:
        # Without a user GUC no row is visible at all.
        cursor.execute("SELECT count(*) FROM clinic_app.identity_userpreference")
        assert cursor.fetchone() == (0,)


def test_runtime_role_cannot_forge_delete_or_move_rows() -> None:
    owner, other = _user("pref-owner"), _user("pref-other")
    with runtime_role(), current_user_guc(owner.pk):
        save_ui_preferences(theme="dark", density="comfortable")
    statements = [
        (
            "INSERT INTO clinic_app.identity_userpreference "
            "(user_id, theme, density, updated_at) "
            "VALUES (%s, 'dark', 'compact', now())",
            [str(other.pk)],
        ),
        ("DELETE FROM clinic_app.identity_userpreference", []),
        (
            "UPDATE clinic_app.identity_userpreference SET user_id = %s",
            [str(other.pk)],
        ),
    ]
    for statement, params in statements:
        with (
            runtime_role(),
            pytest.raises(DatabaseError),
            current_user_guc(owner.pk),
            connection.cursor() as cursor,
        ):
            cursor.execute(statement, params)


def test_closed_vocabulary_is_enforced_by_service_and_database() -> None:
    user = _user("pref-closed")
    with runtime_role(), current_user_guc(user.pk):
        for theme, density in (("sepia", "compact"), ("dark", "cozy"), ("", "")):
            with pytest.raises(PreferenceValueError):
                save_ui_preferences(theme=theme, density=density)
        assert UserPreference.objects.count() == 0
    with (
        runtime_role(),
        pytest.raises(DatabaseError),
        current_user_guc(user.pk),
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "INSERT INTO clinic_app.identity_userpreference "
            "(user_id, theme, density, updated_at) "
            "VALUES (%s, 'sepia', 'compact', now())",
            [str(user.pk)],
        )


def test_preferences_page_saves_and_the_shell_applies_the_choice(
    rbac_graph: RbacGraph,
) -> None:
    client, receptionist = receptionist_client(rbac_graph)
    with runtime_role():
        page = client.get("/account/preferences/")
        assert page.status_code == OK
        assert {"no-store", "private"} <= {
            part.strip() for part in page.headers["Cache-Control"].split(",")
        }
        assert _html_theme(page.content.decode()) == (
            "light",
            "comfortable",
        )
        assert gettext("Display preferences").encode() in page.content
        saved = client.post(
            "/account/preferences/", {"theme": "dark", "density": "compact"}
        )
        assert saved.status_code == OK
        assert _html_theme(saved.content.decode()) == (
            "dark",
            "compact",
        )
        assert gettext("Preferences saved").encode() in saved.content
        agenda = client.get(f"/scheduling/clinics/{rbac_graph.clinic_a}/agenda/")
        assert _html_theme(agenda.content.decode()) == (
            "dark",
            "compact",
        )
        assert b'href="/account/preferences/"' in agenda.content
    with runtime_role(), current_user_guc(receptionist.pk):
        assert load_ui_preferences() == UiPreferences(theme="dark", density="compact")


@pytest.mark.parametrize(
    "payload",
    [
        {"theme": "sepia", "density": "compact"},
        {"theme": "dark", "density": "cozy"},
        {"theme": "dark"},
        {"theme": "<script>", "density": "compact"},
    ],
)
def test_invalid_preference_post_is_rejected_and_nothing_changes(
    rbac_graph: RbacGraph,
    payload: dict[str, str],
) -> None:
    client, receptionist = receptionist_client(rbac_graph)
    with runtime_role():
        response = client.post("/account/preferences/", payload)
    assert response.status_code == BAD_REQUEST
    body = response.content.decode()
    assert "data-focus-error" in body
    assert "<script>" not in body.split("</head>", 1)[1]
    assert _html_theme(body) == ("light", "comfortable")
    with runtime_role(), current_user_guc(receptionist.pk):
        assert UserPreference.objects.count() == 0


def test_anonymous_and_auth_screens_never_read_preferences() -> None:
    assert Client().get("/account/preferences/").status_code == FORBIDDEN
    login = Client().get("/auth/login/").content.decode()
    assert "data-theme" not in login
    assert 'href="/account/preferences/"' not in login


def test_worker_precache_never_lists_axe_or_preference_pages(
    rbac_graph: RbacGraph,
) -> None:
    client, _receptionist = receptionist_client(rbac_graph)
    with runtime_role():
        worker = client.get("/sw.js").content.decode()
    precache = _precache(worker)
    assert precache
    assert not any("vendor/axe" in url for url in precache)
    assert not any("preferences" in url for url in precache)
    assert all(url.startswith("/static/") for url in precache)

"""Per-user saved workspace views (todo 13): posture, palette flow, isolation."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final
from uuid import uuid4

import pytest
from apps.identity.models import UserClinicRole
from apps.identity.saved_views import (
    SavedViewError,
    archive_saved_view,
    list_saved_views,
    save_view,
)
from apps.tenancy.db import tenant_context
from django.db import connection
from django.utils.translation import gettext

from core.test_navigation import _client_for, _get, _post
from identity.permission_support import owner_context, permission_context
from otp_test_support import runtime_role

if TYPE_CHECKING:
    from django.test import Client

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)

OK: Final = 200
FOUND: Final = 302
NOT_FOUND: Final = 404
OPTIONS: Final = "/workspace/command/options/"
RUN: Final = "/workspace/command/run/"


def test_saved_view_table_is_user_bound_archive_only() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT relrowsecurity, relforcerowsecurity, relowner::regrole::text "
            "FROM pg_catalog.pg_class "
            "WHERE oid = 'clinic_app.identity_savedview'::regclass"
        )
        posture = cursor.fetchone()
        cursor.execute(
            "SELECT policyname, roles, cmd FROM pg_catalog.pg_policies "
            "WHERE schemaname = 'clinic_app' AND tablename = 'identity_savedview'"
        )
        policies = cursor.fetchall()
        cursor.execute(
            "SELECT privilege_type FROM information_schema.role_table_grants "
            "WHERE grantee = 'clinic_app' AND table_schema = 'clinic_app' "
            "AND table_name = 'identity_savedview'"
        )
        grants = {row[0] for row in cursor.fetchall()}
        cursor.execute(
            "SELECT column_name, privilege_type "
            "FROM information_schema.column_privileges "
            "WHERE grantee = 'clinic_app' AND table_schema = 'clinic_app' "
            "AND table_name = 'identity_savedview' AND privilege_type = 'UPDATE'"
        )
        column_updates = cursor.fetchall()

    assert posture == (True, True, "clinic_owner")
    assert policies == [("savedview_owner_only", ["clinic_app"], "ALL")]
    assert grants == {"SELECT", "INSERT"}
    assert column_updates == [("archived_at", "UPDATE")]


def _options(client: Client, context: str, query: str = "") -> str:
    token = client.cookies["csrftoken"].value
    with runtime_role():
        response = client.post(
            OPTIONS, {"q": query, "context": context}, headers={"X-CSRFToken": token}
        )
    assert response.status_code == OK
    return response.content.decode()


def _option_value(html: str, label: str) -> str:
    match = re.search(
        r'data-value="([^"]+)" data-kind="[a-z_]+" aria-selected="false">'
        r"<span>" + re.escape(label) + r"</span>",
        html,
    )
    assert match is not None, html
    return match.group(1)


def test_a_week_agenda_is_saved_reopened_and_archived(rbac_graph: RbacGraph) -> None:
    client, _ = _client_for(rbac_graph, "receptionist")
    week = f"/scheduling/clinics/{rbac_graph.clinic_a}/agenda/week/2035-06-04/1/"
    _get(client, week)
    name = f"{gettext('Agenda')} · {gettext('Week')}"
    save_label = gettext("Save this view: %(name)s") % {"name": name}

    offered = _options(client, week)
    saved = _post(
        client, RUN, {"token": _option_value(offered, save_label), "next": week}
    )

    assert saved.status_code == FOUND
    assert saved["Location"] == week
    listed = _options(client, week)
    assert save_label not in listed  # already saved
    href = re.search(
        r'data-href="([^"]+)" data-kind="saved_view"[^>]*><span>' + re.escape(name),
        listed,
    )
    assert href is not None
    assert re.fullmatch(
        rf"/scheduling/clinics/{rbac_graph.clinic_a}/agenda/week/\d{{4}}-\d\d-\d\d/1/",
        href.group(1),
    )
    remove_label = gettext("Remove saved view: %(name)s") % {"name": name}
    archived = _post(
        client, RUN, {"token": _option_value(listed, remove_label), "next": week}
    )
    assert archived.status_code == FOUND
    assert 'data-kind="saved_view"' not in _options(client, week)


def test_saved_views_are_private_to_their_user(rbac_graph: RbacGraph) -> None:
    _, owner_user = _client_for(rbac_graph, "receptionist")
    other, other_user = _client_for(rbac_graph, "receptionist")
    with runtime_role(), tenant_context(owner_user.pk, rbac_graph.organization_a):
        record = save_view(
            clinic_id=rbac_graph.clinic_a, destination="agenda", params={"view": "week"}
        )

    with runtime_role(), tenant_context(other_user.pk, rbac_graph.organization_a):
        assert list_saved_views(clinic_id=rbac_graph.clinic_a) == ()
        with pytest.raises(SavedViewError):
            archive_saved_view(clinic_id=rbac_graph.clinic_a, view_id=record.id)
        with pytest.raises(SavedViewError):
            archive_saved_view(clinic_id=rbac_graph.clinic_a, view_id=uuid4())
    week = f"/scheduling/clinics/{rbac_graph.clinic_a}/agenda/week/2035-06-04/1/"
    _get(other, week)
    assert 'data-kind="saved_view"' not in _options(other, week)


@pytest.mark.parametrize(
    ("destination", "params"),
    [
        ("agenda", {"view": "Sintetico Paciente"}),
        ("agenda", {"patient": "3f0c5b1e-0000-4000-8000-000000000000"}),
        ("Agenda", {"view": "week"}),
        ("agenda", {f"k{index}": "v" for index in range(5)}),
    ],
)
def test_free_text_and_identifiers_never_become_parameters(
    rbac_graph: RbacGraph, destination: str, params: dict[str, str]
) -> None:
    _, user = _client_for(rbac_graph, "receptionist")
    with (
        runtime_role(),
        tenant_context(user.pk, rbac_graph.organization_a),
        pytest.raises(SavedViewError),
    ):
        save_view(clinic_id=rbac_graph.clinic_a, destination=destination, params=params)


def test_revoked_role_hides_saved_views(rbac_graph: RbacGraph) -> None:
    _, user = _client_for(rbac_graph, "receptionist")
    with runtime_role(), tenant_context(user.pk, rbac_graph.organization_a):
        save_view(
            clinic_id=rbac_graph.clinic_a, destination="agenda", params={"view": "day"}
        )
    with owner_context(rbac_graph.organization_a), connection.cursor() as cursor:
        cursor.execute(
            "DELETE FROM clinic_app.identity_userclinicrole WHERE user_id = %s",
            [str(user.pk)],
        )
    with permission_context(rbac_graph, user.pk):
        assert list_saved_views(clinic_id=rbac_graph.clinic_a) == ()
    # The row was never deleted: restoring the role shows it again.
    with owner_context(rbac_graph.organization_a):
        UserClinicRole.objects.create(
            user_id=user.pk,
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_a,
            role="receptionist",
        )
    with permission_context(rbac_graph, user.pk):
        views = list_saved_views(clinic_id=rbac_graph.clinic_a)
    assert [(view.destination, view.params) for view in views] == [
        ("agenda", {"view": "day"})
    ]


def test_a_forged_save_token_is_refused(rbac_graph: RbacGraph) -> None:
    client, _ = _client_for(rbac_graph, "receptionist")
    _get(client, f"/scheduling/clinics/{rbac_graph.clinic_a}/agenda/")

    forged = _post(client, RUN, {"token": "not-a-token"})

    assert forged.status_code == NOT_FOUND

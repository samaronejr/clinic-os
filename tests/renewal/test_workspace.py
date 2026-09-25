"""Role-aware shell: clinic context, module links, installable metadata, worker.

The navigation only mirrors server authorization; every assertion below also
checks that the module route itself still answers with the existing denial.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Final
from uuid import uuid4

import pytest
from apps.core.views import SHELL_STATIC_ASSETS, shell_static_version, workspace_home
from apps.core.workspace import ACTIVE_CLINIC_SESSION_KEY
from apps.identity.models import User, UserClinicRole
from django.contrib.sessions.backends.db import SessionStore
from django.contrib.staticfiles import finders
from django.db import connection, transaction
from django.http import HttpResponse
from django.test import Client, RequestFactory
from django.utils.translation import gettext

from otp_test_support import runtime_role
from patient_http_support import receptionist_client, verified_physician_client

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from uuid import UUID

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)

OK: Final = 200
FOUND: Final = 302
FORBIDDEN: Final = 403
NOT_FOUND: Final = 404
NAV_LINK: Final = re.compile(
    r'<a class="nav-link" href="([^"]+)" data-module="([a-z]+)"( aria-current="page")?>'
)
NAV_CLINIC: Final = re.compile(
    r'<strong class="nav-clinic">([^<]+?)\s*(?:<span|</strong>)'
)
PNG_SIGNATURE: Final = b"\x89PNG\r\n\x1a\n"
# A physician sees every IA destination their bundle and routes open (plan
# annex IA, todo 13): Patients leads to consent, never to the registry, and
# the clinic billing ledger stays hidden.
PHYSICIAN_MODULES: Final = {"agenda", "patients", "operations"}


def _modules(content: bytes) -> dict[str, tuple[str, bool]]:
    html = content.decode()
    return {
        module: (href, bool(current))
        for href, module, current in NAV_LINK.findall(html)
    }


def _clinics_in_nav(content: bytes) -> list[str]:
    return NAV_CLINIC.findall(content.decode())


def _grant(graph: RbacGraph, user_id: UUID, clinic_id: UUID, role: str) -> None:
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(graph.organization_a)],
        )
        UserClinicRole.objects.create(
            user_id=user_id,
            organization_id=graph.organization_a,
            clinic_id=clinic_id,
            role=role,
        )


def _revoke(graph: RbacGraph, user_id: UUID, clinic_id: UUID) -> None:
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(graph.organization_a)],
        )
        UserClinicRole.objects.filter(user_id=user_id, clinic_id=clinic_id).delete()


def test_receptionist_sees_manager_modules_for_the_url_clinic(
    rbac_graph: RbacGraph,
) -> None:
    client, _receptionist = receptionist_client(rbac_graph)
    agenda = f"/scheduling/clinics/{rbac_graph.clinic_a}/agenda/"

    with runtime_role():
        response = client.get(agenda)

    assert response.status_code == OK
    modules = _modules(response.content)
    assert modules == {
        "agenda": (agenda, True),
        "finance": (f"/billing/clinics/{rbac_graph.clinic_a}/charges/", False),
        "patients": (f"/intake/clinics/{rbac_graph.clinic_a}/patients/", False),
        "operations": (f"/retention/clinics/{rbac_graph.clinic_a}/", False),
    }
    # Availability and consent moved into the Agenda and Patients sections.
    assert (
        f'href="/scheduling/clinics/{rbac_graph.clinic_a}/availability/" '
        'data-tab="availability"'
    ).encode() in response.content
    assert _clinics_in_nav(response.content) == ["Todo 8 Clinic A"]
    assert b'href="/auth/logout/"' in response.content
    assert gettext("Sign out").encode() in response.content
    assert client.session[ACTIVE_CLINIC_SESSION_KEY] == str(rbac_graph.clinic_a)
    assert re.search(
        rb'data-module="agenda" aria-current="page">Agenda <span class="nav-badge">'
        rb"hoje \d\d/\d\d</span>",
        response.content,
    )


def test_physician_never_sees_the_registry_and_the_route_still_denies(
    rbac_graph: RbacGraph,
) -> None:
    client = verified_physician_client(rbac_graph)
    availability = f"/scheduling/clinics/{rbac_graph.clinic_a}/availability/"

    patients = f"/intake/clinics/{rbac_graph.clinic_a}/patients/"

    with runtime_role():
        allowed = client.get(availability)
        blank = client.get(patients)
        denied = client.post(patients, {"q": "Marina", "page": "1"})

    assert allowed.status_code == OK
    assert set(_modules(allowed.content)) == PHYSICIAN_MODULES
    assert _modules(allowed.content)["agenda"] == (
        f"/scheduling/clinics/{rbac_graph.clinic_a}/agenda/",
        True,
    )
    assert _modules(allowed.content)["patients"] == (
        f"/clinics/{rbac_graph.clinic_a}/consent/",
        False,
    )
    assert (
        f'href="{availability}" aria-current="page" data-tab="availability"'
    ).encode() in allowed.content
    # The registry fails closed on the blank GET as well as on the search, and
    # the shell never offers the module to a physician.
    assert blank.status_code == NOT_FOUND
    assert set(_modules(blank.content)) == PHYSICIAN_MODULES
    assert denied.status_code == NOT_FOUND
    assert set(_modules(denied.content)) == PHYSICIAN_MODULES
    assert _clinics_in_nav(denied.content) == ["Todo 8 Clinic A"]


def test_forbidden_clinic_is_denied_and_never_becomes_the_context(
    rbac_graph: RbacGraph,
) -> None:
    client, _receptionist = receptionist_client(rbac_graph)
    foreign_agenda = f"/scheduling/clinics/{rbac_graph.clinic_b}/agenda/"

    with runtime_role():
        response = client.get(foreign_agenda)

    assert response.status_code == NOT_FOUND
    assert b"Todo 8 Clinic B" not in response.content
    assert _clinics_in_nav(response.content) == ["Todo 8 Clinic A"]
    assert client.session[ACTIVE_CLINIC_SESSION_KEY] == str(rbac_graph.clinic_a)
    assert str(rbac_graph.clinic_b) not in response.content.decode()


@pytest.mark.parametrize(
    "stale",
    [
        lambda graph: str(graph.clinic_b),
        lambda graph: str(graph.clinic_c),
        lambda _graph: str(uuid4()),
        lambda _graph: "not-a-uuid",
    ],
    ids=["other-clinic-same-org", "other-org-clinic", "unknown", "malformed"],
)
def test_stale_session_clinic_is_replaced_by_an_authorized_one(
    rbac_graph: RbacGraph,
    stale: Callable[[RbacGraph], str],
) -> None:
    client, _receptionist = receptionist_client(rbac_graph)
    session = client.session
    session[ACTIVE_CLINIC_SESSION_KEY] = stale(rbac_graph)
    session.save()

    with runtime_role():
        response = client.get("/auth/protected/")

    assert response.status_code == OK
    assert _clinics_in_nav(response.content) == ["Todo 8 Clinic A"]
    assert b"Todo 8 Clinic B" not in response.content
    assert b"Todo 8 Clinic C" not in response.content
    assert client.session[ACTIVE_CLINIC_SESSION_KEY] == str(rbac_graph.clinic_a)


def test_revoked_role_drops_the_remembered_clinic_and_denies_its_routes(
    rbac_graph: RbacGraph,
) -> None:
    client, receptionist = receptionist_client(rbac_graph)
    _grant(rbac_graph, receptionist.pk, rbac_graph.clinic_b, "receptionist")
    agenda_b = f"/scheduling/clinics/{rbac_graph.clinic_b}/agenda/"

    with runtime_role():
        remembered = client.get(agenda_b)
    assert remembered.status_code == OK
    assert _clinics_in_nav(remembered.content) == ["Todo 8 Clinic B"]
    assert b"Todo 8 Clinic A" in remembered.content  # offered by the switcher
    assert client.session[ACTIVE_CLINIC_SESSION_KEY] == str(rbac_graph.clinic_b)

    _revoke(rbac_graph, receptionist.pk, rbac_graph.clinic_b)
    with runtime_role():
        landing = client.get("/auth/protected/")
        denied = client.get(agenda_b)

    assert landing.status_code == OK
    assert _clinics_in_nav(landing.content) == ["Todo 8 Clinic A"]
    assert b"Todo 8 Clinic B" not in landing.content
    assert client.session[ACTIVE_CLINIC_SESSION_KEY] == str(rbac_graph.clinic_a)
    assert denied.status_code == NOT_FOUND
    assert b"Todo 8 Clinic B" not in denied.content


def test_workspace_home_opens_todays_agenda_or_explains_the_missing_clinic(
    rbac_graph: RbacGraph,
) -> None:
    client, receptionist = receptionist_client(rbac_graph)

    with runtime_role():
        opened = client.get("/workspace/")
    assert opened.status_code == FOUND
    assert opened.headers["Location"] == (
        f"/scheduling/clinics/{rbac_graph.clinic_a}/agenda/"
    )

    # Losing the last role also loses tenant membership: the tenant boundary
    # answers before the workspace can, so the account is refused outright.
    _revoke(rbac_graph, receptionist.pk, rbac_graph.clinic_a)
    with runtime_role():
        orphaned = client.get("/workspace/")
    assert orphaned.status_code == FORBIDDEN
    assert _modules(orphaned.content) == {}
    assert b"Todo 8 Clinic A" not in orphaned.content


def test_workspace_home_explains_a_membership_without_any_clinic_role(
    rbac_graph: RbacGraph,
) -> None:
    user = User.objects.get(username=rbac_graph.no_membership_username)
    request = RequestFactory().get("/workspace/")
    request.user = user
    request.session = SessionStore()

    # Membership is derived from roles, so the tenant boundary refuses this
    # account before a view runs; bind the GUCs directly to reach the view.
    with runtime_role(), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_user_id', %s, true)",
            [str(user.pk)],
        )
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(rbac_graph.organization_a)],
        )
        response = workspace_home(request)

    assert response.status_code == OK
    assert isinstance(response, HttpResponse)
    body = response.content
    assert gettext("No clinic assigned").encode() in body
    assert _modules(body) == {}
    assert ACTIVE_CLINIC_SESSION_KEY not in request.session


def test_index_greets_visitors_and_forwards_sessions() -> None:
    anonymous = Client().get("/")
    assert anonymous.status_code == OK
    assert b'href="/auth/login/"' in anonymous.content
    assert b'class="nav-list"' not in anonymous.content

    session = Client().session
    session["_auth_user_id"] = str(uuid4())
    session["active_org_id"] = str(uuid4())
    session.save()
    assert session.session_key is not None
    holder = Client()
    holder.cookies["sessionid"] = session.session_key
    forwarded = holder.get("/")
    assert forwarded.status_code == FOUND
    assert forwarded.headers["Location"] == "/workspace/"


def test_authentication_screens_keep_a_brand_only_header(
    rbac_graph: RbacGraph,
) -> None:
    anonymous = Client().get("/auth/login/")
    assert anonymous.status_code == OK
    assert b'class="nav-list"' not in anonymous.content
    assert b'class="nav-brand"' in anonymous.content
    assert b"Clinic Ops" in anonymous.content

    client, _receptionist = receptionist_client(rbac_graph)
    with runtime_role():
        logout = client.get("/auth/logout/")
    assert logout.status_code == OK
    assert b'class="nav-list"' not in logout.content
    assert b"data-service-worker" not in logout.content

    # A session refused at the tenant boundary gets the same brand-only shell.
    session = client.session
    session["_auth_user_id"] = "malformed"
    session.save()
    refused = client.get("/auth/protected/")
    assert refused.status_code == FORBIDDEN
    assert b'class="nav-brand"' in refused.content
    assert b'class="nav-list"' not in refused.content


def test_manifest_and_icons_resolve_as_static_files() -> None:
    manifest_path = finders.find("manifest.webmanifest")
    assert isinstance(manifest_path, str)
    with open(manifest_path, "rb") as handle:  # noqa: PTH123 - finder path
        manifest = json.load(handle)
    assert manifest["name"] == "Clinic Ops"
    assert manifest["display"] == "standalone"
    assert manifest["start_url"] == "/"
    assert manifest["scope"] == "/"
    assert manifest["lang"] == "pt-BR"
    sizes = {icon["sizes"] for icon in manifest["icons"]}
    assert {"192x192", "512x512"} <= sizes
    purposes = {icon["purpose"] for icon in manifest["icons"]}
    assert {"any", "maskable"} <= purposes
    for icon in manifest["icons"]:
        assert icon["src"].startswith("/static/")
        found = finders.find(icon["src"].removeprefix("/static/"))
        assert isinstance(found, str), icon["src"]
        with open(found, "rb") as handle:  # noqa: PTH123 - finder path
            head = handle.read(24)
        if icon["type"] == "image/png":
            assert head.startswith(PNG_SIGNATURE)
            width = int.from_bytes(head[16:20], "big")
            height = int.from_bytes(head[20:24], "big")
            assert f"{width}x{height}" == icon["sizes"]
    for asset in SHELL_STATIC_ASSETS:
        assert isinstance(finders.find(asset), str), asset


def test_shell_static_version_tracks_asset_bytes(tmp_path: Path) -> None:
    def version_for(payload: bytes) -> str:
        asset = tmp_path / "shell.css"
        asset.write_bytes(payload)
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(finders, "find", lambda _name: str(asset))
            return shell_static_version(("shell.css",))

    first = version_for(b"a{color:red}")
    assert re.fullmatch(r"[0-9a-f]{12}", first)
    assert version_for(b"a{color:red}") == first
    assert version_for(b"a{color:blue}") != first
    with pytest.raises(FileNotFoundError):
        shell_static_version(("does/not/exist.css",))


def test_service_worker_is_private_versioned_and_static_only(
    rbac_graph: RbacGraph,
) -> None:
    assert Client().get("/sw.js").status_code == FORBIDDEN

    client, _receptionist = receptionist_client(rbac_graph)
    with runtime_role():
        response = client.get("/sw.js")
        page = client.get(f"/scheduling/clinics/{rbac_graph.clinic_a}/agenda/")

    assert response.status_code == OK
    assert response.headers["Content-Type"].startswith("text/javascript")
    assert "no-store" in response.headers["Cache-Control"]
    assert response.headers["Service-Worker-Allowed"] == "/"
    worker = response.content.decode()
    assert f'const VERSION = "{shell_static_version()}";' in worker
    precache = re.search(r"const PRECACHE = (\[.*?\]);", worker)
    assert precache is not None
    urls = json.loads(precache.group(1))
    assert urls
    assert all(url.startswith("/static/") for url in urls)
    assert not any(url.endswith(".html") for url in urls)
    assert b'data-service-worker="/sw.js"' in page.content

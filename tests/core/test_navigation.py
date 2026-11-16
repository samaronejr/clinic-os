"""Workspace IA registry, command palette and patient context (todo 13).

Every request runs through the real session, OTP and tenant middleware as
``clinic_app``; permissions come from ``clinic_app.has_permission``.
"""

from __future__ import annotations

import json
import re
import secrets
from datetime import timedelta
from typing import TYPE_CHECKING, Final
from uuid import UUID, uuid4

import pytest
from apps.core import patient_context
from apps.core.navigation import DESTINATIONS, granted_permissions
from apps.core.patient_context import (
    ContextSwitch,
    SwitchConsequence,
    register_context_switch_guard,
    unregister_context_switch_guard,
)
from apps.core.workspace import ACTIVE_CLINIC_SESSION_KEY
from apps.identity.models import RoleGrant, User, UserClinicRole
from apps.identity.permissions import BUNDLES_V1, BUNDLES_V2
from apps.tenancy.db import tenant_context
from django.conf import settings
from django.contrib.auth import SESSION_KEY
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext, ngettext

from auth.stepup_test_support import create_role_actor
from identity.permission_support import owner_context
from otp_test_support import (
    create_totp_device,
    fixed_otp_time,
    login,
    runtime_role,
    token_for,
)
from patient_http_support import seed_patients
from rbac_fixtures import RBAC_RAW_CREDENTIAL

if TYPE_CHECKING:
    from collections.abc import Iterator

    from django.test.client import _MonkeyPatchedWSGIResponse

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)

OK: Final = 200
FOUND: Final = 302
FORBIDDEN: Final = 403
NOT_FOUND: Final = 404
CONFLICT: Final = 409
BAD_REQUEST: Final = 400
SEARCH: Final = "/api/ui/v1/command/search/"
OPTIONS: Final = "/workspace/command/options/"
RUN: Final = "/workspace/command/run/"
CLOSE: Final = "/workspace/patient/close/"
PATIENT: Final = "Sintetico Navegacao da Silva"
OTHER: Final = "Sintetico Navegacao Souza"
NAV_LINK: Final = re.compile(
    r'<a class="nav-link" href="([^"]+)" data-module="([a-z]+)"'
)
PRIVILEGED: Final = frozenset(
    {"physician", "clinic_admin", "owner", "clinic_manager", "org_admin"}
)
# The exact top-level set per role (plan annex IA, RP bundles v1, and the
# route guards the destinations lead to today). Roles whose routes are not
# migrated to permissions yet keep their narrower workspace. RP configuration
# actors can still discover scheduling Settings without patient administration.
# Automations is the RP row "admin" (clinic manager/admin) and "org" (owner).
# Task 79: every role resolves its clinic from its own grants, so finance and the
# org admin get their delivered places from a fresh session too.
EXPECTED: Final = {
    "owner": [
        "agenda",
        "patients",
        "finance",
        "operations",
        "automations",
        "reports",
        "settings",
    ],
    "clinic_admin": [
        "agenda",
        "patients",
        "inbox",
        "messages",
        "finance",
        "operations",
        "automations",
        "reports",
        "settings",
    ],
    "receptionist": [
        "today",
        "agenda",
        "patients",
        "inbox",
        "messages",
        "finance",
        "operations",
        "settings",
    ],
    # Task 53: physicians open Automations > Care coordination for their plans.
    "physician": [
        "today",
        "agenda",
        "patients",
        "inbox",
        "messages",
        "operations",
        "automations",
    ],
    "nurse": ["today", "inbox", "messages"],
    "allied_professional": ["inbox"],
    # Task 56: Finance opens for any holder of a finance-workspace permission:
    # charge.collect (scheduler) or refund/payout.request (clinic manager).
    "scheduler": ["inbox", "messages", "finance", "settings"],
    # Task 50: privacy.requests opens Patients > Data-subject requests.
    "clinic_manager": [
        "patients",
        "inbox",
        "messages",
        "finance",
        "automations",
        "reports",
        "settings",
    ],
    # Task 63: Reports follows charge.read into both bundles.
    "finance": ["finance", "reports"],
    # Task 50: privacy.requests opens Patients > Data-subject requests.
    "org_admin": ["patients", "finance", "automations", "reports", "settings"],
}
CLINIC_A_NAME: Final = "Todo 8 Clinic A"
# Where /workspace/ sends a fresh session (no remembered clinic, no module URL):
# the first delivered place the role's grants open in its clinic.
LANDING: Final = {
    "owner": "scheduling:agenda",
    "clinic_admin": "scheduling:agenda",
    "receptionist": "scheduling:agenda",
    "physician": "scheduling:agenda",
    "nurse": "scheduling:reception",
    "allied_professional": "orders:results",
    "scheduler": "scheduling:resource-settings",
    "clinic_manager": "scheduling:resource-settings",
    "finance": "billing:finance",
    "org_admin": "scheduling:resource-settings",
}
# The empty-query palette of the bundles without a clinical landing, and one
# typed command each reaches by name.
PALETTE: Final = {
    "finance": [
        "billing:finance",
        "billing:charges",
        "analytics:reports",
        "identity:preferences",
    ],
    "org_admin": [
        "intake:data-requests",
        "billing:finance",
        "ai:agents",
        "ai:care-coordination",
        "analytics:reports",
        "ai:settings",
        "scheduling:resource-settings",
        "identity:emergency-reviews",
        "identity:governance",
        "identity:preferences",
    ],
}
TYPED_COMMAND: Final = {
    "finance": ("Reports", "analytics:reports"),
    "org_admin": ("Enterprise governance", "identity:governance"),
}
# The roles whose default landing is the Agenda, the bundle permission that
# opens it, and the next place each lands on once that permission is narrowed.
LEGACY_ROLES: Final = ("owner", "clinic_admin", "receptionist", "physician")
AGENDA_GRANT: Final = {
    "owner": "appointment.read",
    "clinic_admin": "appointment.read",
    "receptionist": "appointment.read",
    "physician": "appointment.read_own",
}
NARROWED_LANDING: Final = {
    "owner": "scheduling:resource-settings",
    "clinic_admin": "scheduling:resource-settings",
    "receptionist": "scheduling:reception",
    "physician": "orders:results",
}
CLINIC_B_NAME: Final = "Todo 8 Clinic B"
TOKEN_MIN_LENGTH: Final = 20
UNDELIVERED: Final[frozenset[str]] = frozenset()


def _client_for(graph: RbacGraph, role: str) -> tuple[Client, User]:
    user = create_role_actor(graph, UserClinicRole.Role(role))
    client = Client(enforce_csrf_checks=True, raise_request_exception=False)
    client.cookies[settings.CSRF_COOKIE_NAME] = secrets.token_hex(16)
    _reauthenticate(client, user.pk, role)
    return client, user


def _reauthenticate(client: Client, user_id: UUID, role: str) -> dict[str, object]:
    """Use fresh real authority after setup revokes the previous cookie."""
    previous = (
        dict(client.session) if settings.SESSION_COOKIE_NAME in client.cookies else {}
    )
    if SESSION_KEY in previous:
        stale = Client(raise_request_exception=False)
        stale.cookies[settings.SESSION_COOKIE_NAME] = client.cookies[
            settings.SESSION_COOKIE_NAME
        ].value
        rejected = _get(stale, "/auth/protected/")
        assert rejected.status_code == FOUND
        assert rejected["Location"].startswith("/auth/login/")
    user = User.objects.get(pk=user_id)
    with runtime_role():
        login_client = Client()
        authenticated = login(login_client, user.username, password=RBAC_RAW_CREDENTIAL)
        assert authenticated.status_code == FOUND
        if role in PRIVILEGED:
            device = create_totp_device(user.pk, confirmed=True)
            with fixed_otp_time():
                verified = login_client.post(
                    "/auth/verify/",
                    {
                        "otp_device": device.persistent_id,
                        "otp_token": token_for(device),
                        "next": "/auth/protected/",
                    },
                )
            assert verified.status_code == FOUND
    fresh = dict(login_client.session)
    client.cookies[settings.SESSION_COOKIE_NAME] = login_client.cookies[
        settings.SESSION_COOKIE_NAME
    ].value
    presentation = {
        key: value
        for key, value in previous.items()
        if key == "active_clinic_id" or key.startswith(("workspace.", "ehr."))
    }
    if presentation:
        session = client.session
        session.update(presentation)
        session.save()
    return fresh


def _csrf(client: Client) -> dict[str, str]:
    return {"X-CSRFToken": client.cookies[settings.CSRF_COOKIE_NAME].value}


def _get(client: Client, path: str) -> _MonkeyPatchedWSGIResponse:
    with runtime_role(), fixed_otp_time():
        return client.get(path)


def _post(
    client: Client, path: str, data: dict[str, str]
) -> _MonkeyPatchedWSGIResponse:
    form = {"csrfmiddlewaretoken": client.cookies[settings.CSRF_COOKIE_NAME].value}
    form.update(data)
    with runtime_role(), fixed_otp_time():
        return client.post(path, form)


def _api(
    client: Client, body: dict[str, str], *, csrf: bool = True
) -> _MonkeyPatchedWSGIResponse:
    with runtime_role(), fixed_otp_time():
        return client.post(
            SEARCH,
            data=json.dumps(body),
            content_type="application/json",
            headers=_csrf(client) if csrf else {},
        )


def _main(response: _MonkeyPatchedWSGIResponse) -> str:
    html = response.content.decode()
    return html.split("<main", 1)[1].split("</main>", 1)[0]


def _modules(response: _MonkeyPatchedWSGIResponse) -> list[str]:
    return [module for _href, module in NAV_LINK.findall(response.content.decode())]


@pytest.mark.parametrize("role", list(EXPECTED))
def test_each_role_sees_its_exact_destination_set(
    rbac_graph: RbacGraph, role: str
) -> None:
    client, _user = _client_for(rbac_graph, role)
    response = _get(client, "/auth/protected/")

    assert response.status_code == OK
    assert _modules(response) == EXPECTED[role]
    html = response.content.decode()
    assert not UNDELIVERED.intersection(_modules(response))
    # Every role with an open place gets the commands for it.
    assert 'data-command-open="command-palette"' in html
    assert 'id="command-palette"' in html


@pytest.mark.parametrize("role", ["scheduler", "clinic_manager", "receptionist"])
def test_booking_staff_discover_actual_settings_without_administrative_forms(
    rbac_graph: RbacGraph, role: str
) -> None:
    client, _user = _client_for(rbac_graph, role)
    clinic = rbac_graph.clinic_a
    home = _get(client, "/auth/protected/")
    links = {module: href for href, module in NAV_LINK.findall(home.content.decode())}
    assert links["settings"] == f"/scheduling/clinics/{clinic}/settings/"
    settings_page = _get(client, links["settings"])
    assert settings_page.status_code == OK
    assert 'data-resource-form="resource"' in settings_page.content.decode()
    assert 'data-resource-form="service"' in settings_page.content.decode()
    assert 'data-resource-form="template"' in settings_page.content.decode()
    assert 'data-resource-form="closure"' in settings_page.content.decode()
    assert "data-settings-form" not in settings_page.content.decode()
    if role in {"scheduler", "clinic_manager"}:
        assert _get(client, f"/clinics/{clinic}/settings/").status_code == FORBIDDEN


def test_destination_links_lead_to_the_first_open_place(
    rbac_graph: RbacGraph,
) -> None:
    clinic = rbac_graph.clinic_a
    physician, _ = _client_for(rbac_graph, "physician")
    receptionist, _ = _client_for(rbac_graph, "receptionist")

    physician_links = {
        module: href
        for href, module in NAV_LINK.findall(
            _get(physician, "/auth/protected/").content.decode()
        )
    }
    receptionist_links = {
        module: href
        for href, module in NAV_LINK.findall(
            _get(receptionist, "/auth/protected/").content.decode()
        )
    }

    # The registry denies physicians, so Patients opens the consent entry.
    assert physician_links["patients"] == f"/clinics/{clinic}/consent/"
    assert receptionist_links["patients"] == f"/intake/clinics/{clinic}/patients/"
    assert receptionist_links["operations"] == f"/retention/clinics/{clinic}/"
    assert receptionist_links["finance"] == f"/billing/clinics/{clinic}/finance/"


@pytest.mark.parametrize("role", ["owner", "clinic_admin", "clinic_manager"])
def test_reporting_roles_open_their_governed_reports_destination(
    rbac_graph: RbacGraph, role: str
) -> None:
    client, _user = _client_for(rbac_graph, role)
    home = _get(client, "/auth/protected/")
    links = {module: href for href, module in NAV_LINK.findall(home.content.decode())}
    assert links["reports"] == f"/reports/clinics/{rbac_graph.clinic_a}/"
    assert _get(client, links["reports"]).status_code == OK


@pytest.mark.parametrize(
    "role", ["nurse", "allied_professional", "scheduler", "clinic_manager", "finance"]
)
def test_workflow_candidate_opens_only_delivered_operations(
    rbac_graph: RbacGraph, role: str
) -> None:
    client, user = _client_for(rbac_graph, role)
    with owner_context(rbac_graph.organization_a):
        UserClinicRole.objects.filter(
            user=user, clinic_id=rbac_graph.clinic_a, role=role
        ).update(workflow_bundle_version=2)
    _reauthenticate(client, user.pk, role)
    response = _get(client, "/auth/protected/")
    assert response.status_code == OK
    # Task 50: privacy.requests opens Patients for the clinic manager.
    assert _modules(response) == (
        [
            "patients",
            "inbox",
            "messages",
            "finance",
            "operations",
            "automations",
            "reports",
            "settings",
        ]
        if role == "clinic_manager"
        else ["inbox", "messages", "finance", "operations", "settings"]
        if role == "scheduler"
        else ["today", "inbox", "messages", "operations"]
        if role == "nurse"
        else ["inbox", "operations"]
        if role == "allied_professional"
        else ["finance", "operations", "reports"]
    )
    assert f'href="/clinics/{rbac_graph.clinic_a}/tasks/"' in response.content.decode()


@pytest.mark.parametrize(
    ("role", "modules", "home"),
    [
        # Task 63: Reports follows charge.read into both bundles.
        ("finance", ["finance", "reports"], "/billing/clinics/{clinic}/finance/"),
        # Task 50: privacy.requests opens Patients > Data-subject requests.
        (
            "org_admin",
            ["patients", "finance", "automations", "reports", "settings"],
            "/scheduling/clinics/{clinic}/settings/",
        ),
    ],
)
def test_finance_resolves_the_shell_for_bundles_without_a_clinical_landing(
    rbac_graph: RbacGraph, role: str, modules: list[str], home: str
) -> None:
    # Task 56 opens Finance to these v1 bundles, which have no clinical, task
    # or reception landing; on its own pages the shell must still name the
    # clinic, its destinations and a home the bundle can actually open.
    client, _user = _client_for(rbac_graph, role)
    finance = f"/billing/clinics/{rbac_graph.clinic_a}/finance/"
    response = _get(client, finance)
    assert response.status_code == OK
    assert _modules(response) == modules
    html = response.content.decode()
    assert f'data-command-next="{finance}"' in html
    assert f'class="nav-brand" href="{home.format(clinic=rbac_graph.clinic_a)}"' in html


@pytest.mark.parametrize("role", list(EXPECTED))
def test_a_fresh_session_lands_every_role_on_authorized_work(
    rbac_graph: RbacGraph, role: str
) -> None:
    client, _user = _client_for(rbac_graph, role)
    assert ACTIVE_CLINIC_SESSION_KEY not in client.session
    landing = reverse(LANDING[role], kwargs={"clinic_id": rbac_graph.clinic_a})

    home = _get(client, "/workspace/")

    assert home.status_code == FOUND
    assert home["Location"] == landing
    page = _get(client, landing)
    assert page.status_code == OK
    html = page.content.decode()
    assert f'<strong class="nav-clinic">{CLINIC_A_NAME}</strong>' in html
    assert f'class="nav-brand" href="{landing}"' in html
    assert _modules(page) == EXPECTED[role]
    assert 'data-command-open="command-palette"' in html


@pytest.mark.parametrize("role", list(PALETTE))
def test_a_fresh_session_offers_the_commands_its_grants_open(
    rbac_graph: RbacGraph, role: str
) -> None:
    client, _user = _client_for(rbac_graph, role)
    # The page shown after sign-in; the palette then posts no clinic selector.
    signed_in = _get(client, "/auth/protected/")
    assert 'id="command-palette"' in signed_in.content.decode()

    rows = _api(client, {"q": ""}).json()

    assert [row["action_url_name"] for row in rows] == PALETTE[role]
    for row in rows:
        opened = _get(client, str(row["href"]))
        assert opened.status_code == OK, row["action_url_name"]
    label, url_name = TYPED_COMMAND[role]
    href = reverse(url_name, kwargs={"clinic_id": rbac_graph.clinic_a})
    with runtime_role(), fixed_otp_time():
        options = client.post(OPTIONS, {"q": gettext(label)}, headers=_csrf(client))
    assert options.status_code == OK
    # The typed command is the first row, so Enter on it opens the place.
    first = re.search(r'data-href="([^"]+)"', options.content.decode())
    assert first is not None
    assert first.group(1) == href
    page = _post(client, "/workspace/command/", {"q": gettext(label)})
    assert page.status_code == OK
    assert f'href="{href}"' in _main(page)


def test_narrowed_finance_grants_move_then_remove_the_landing(
    rbac_graph: RbacGraph,
) -> None:
    client, user = _client_for(rbac_graph, "finance")
    clinic = rbac_graph.clinic_a

    def narrow(*permissions: str) -> None:
        with owner_context(rbac_graph.organization_a):
            for permission in permissions:
                RoleGrant.objects.create(
                    organization_id=rbac_graph.organization_a,
                    clinic_id=clinic,
                    role="finance",
                    permission=permission,
                    valid_from=timezone.now() - timedelta(minutes=1),
                )
        _reauthenticate(client, user.pk, "finance")

    # Without the finance-workspace permissions only Charges stays open there.
    narrow("charge.collect", "settlement.post")
    charges = reverse("billing:charges", kwargs={"clinic_id": clinic})
    home = _get(client, "/workspace/")
    assert home.status_code == FOUND
    assert home["Location"] == charges
    page = _get(client, charges)
    assert page.status_code == OK
    assert _modules(page) == ["finance", "reports"]
    assert [row["action_url_name"] for row in _api(client, {"q": ""}).json()] == [
        "billing:charges",
        "analytics:reports",
        "identity:preferences",
    ]

    # Narrowing charge.read closes every place: the remembered clinic, its
    # landing and its commands all go on the next request.
    narrow("charge.read")
    assert client.session[ACTIVE_CLINIC_SESSION_KEY] == str(clinic)
    gone = _get(client, "/workspace/")
    assert gone.status_code == OK
    html = gone.content.decode()
    assert gettext("No clinic assigned") in html
    assert CLINIC_A_NAME not in html
    assert _modules(gone) == []
    assert 'id="command-palette"' not in html
    assert ACTIVE_CLINIC_SESSION_KEY not in client.session
    assert _get(client, "/workspace/command/").status_code == NOT_FOUND
    assert _api(client, {"q": "", "clinic_id": str(clinic)}).content == b"[]"
    assert _get(client, charges).status_code == FORBIDDEN


@pytest.mark.parametrize(
    ("role", "url_name"),
    [("finance", "billing:finance"), ("org_admin", "identity:governance")],
)
def test_unknown_and_foreign_clinic_selectors_share_one_denial(
    rbac_graph: RbacGraph, role: str, url_name: str
) -> None:
    client, _user = _client_for(rbac_graph, role)
    selectors = (rbac_graph.clinic_c, uuid4())

    refused = [
        _get(client, reverse(url_name, kwargs={"clinic_id": selector}))
        for selector in selectors
    ]
    searched = [
        _api(client, {"q": "", "clinic_id": str(selector)}) for selector in selectors
    ]

    assert {response.status_code for response in refused} == {FORBIDDEN}
    assert len({_main(response) for response in refused}) == 1
    assert all("Todo 8 Clinic C" not in r.content.decode() for r in refused)
    assert all(response.content == b"[]" for response in searched)
    # Refusals remember nothing: the fresh landing is still the own clinic's.
    assert ACTIVE_CLINIC_SESSION_KEY not in client.session
    home = _get(client, "/workspace/")
    assert home["Location"] == reverse(
        LANDING[role], kwargs={"clinic_id": rbac_graph.clinic_a}
    )


def _add_clinic_b(graph: RbacGraph, user_id: UUID, role: str) -> None:
    """Give the actor the same role, with its full bundle, in clinic B."""
    with owner_context(graph.organization_a):
        UserClinicRole.objects.create(
            user_id=user_id,
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_b,
            role=role,
        )


def _remove_in_clinic_a(graph: RbacGraph, role: str, permissions: set[str]) -> None:
    """Narrow the role's bundle in clinic A only (released v1 or additive v2)."""
    with owner_context(graph.organization_a):
        for permission in sorted(permissions):
            RoleGrant.objects.create(
                organization_id=graph.organization_a,
                clinic_id=graph.clinic_a,
                role=role,
                permission=permission,
                bundle_version=1 if permission in BUNDLES_V1[role] else 2,
                valid_from=timezone.now() - timedelta(minutes=1),
            )


def _granted_in_clinic_a(graph: RbacGraph, user_id: UUID) -> frozenset[str]:
    with runtime_role(), tenant_context(user_id, graph.organization_a):
        return granted_permissions(graph.clinic_a)


def _assert_uniform_agenda_denial(client: Client, graph: RbacGraph) -> None:
    """Clinic A's refused Agenda answers exactly like a foreign or unknown one."""
    refused = [
        _get(client, reverse("scheduling:agenda", kwargs={"clinic_id": selector}))
        for selector in (graph.clinic_a, graph.clinic_c, uuid4())
    ]
    assert {response.status_code for response in refused} == {NOT_FOUND}
    assert len({_main(response) for response in refused}) == 1
    assert all("Todo 8 Clinic C" not in r.content.decode() for r in refused)


@pytest.mark.parametrize("role", LEGACY_ROLES)
def test_a_narrowed_agenda_grant_moves_agenda_roles_to_their_next_open_place(
    rbac_graph: RbacGraph, role: str
) -> None:
    client, user = _client_for(rbac_graph, role)
    _add_clinic_b(rbac_graph, user.pk, role)
    _remove_in_clinic_a(rbac_graph, role, {AGENDA_GRANT[role]})
    _reauthenticate(client, user.pk, role)
    landing = reverse(NARROWED_LANDING[role], kwargs={"clinic_id": rbac_graph.clinic_a})
    agenda_b = reverse("scheduling:agenda", kwargs={"clinic_id": rbac_graph.clinic_b})

    home = _get(client, "/workspace/")

    # Clinic A still opens other places: it stays listed, landing on one of them.
    assert home.status_code == FOUND
    assert home["Location"] == landing
    page = _get(client, landing)
    assert page.status_code == OK
    html = page.content.decode()
    assert f'class="nav-brand" href="{landing}"' in html
    assert "agenda" not in _modules(page)
    assert 'id="command-palette"' in html
    assert f'<a class="nav-link" href="{agenda_b}">{CLINIC_B_NAME}</a>' in html
    rows = _api(client, {"q": ""}).json()
    assert rows
    assert "scheduling:agenda" not in {row["action_url_name"] for row in rows}
    # From clinic B, the switcher leads to clinic A's open place, not its Agenda.
    other = _get(client, agenda_b)
    assert other.status_code == OK
    switch = f'<a class="nav-link" href="{landing}">{CLINIC_A_NAME}</a>'
    assert switch in other.content.decode()
    _assert_uniform_agenda_denial(client, rbac_graph)


@pytest.mark.parametrize("other_clinic", [False, True])
@pytest.mark.parametrize("role", LEGACY_ROLES)
def test_agenda_roles_without_grants_lose_the_clinic_landing_and_commands(
    rbac_graph: RbacGraph, role: str, other_clinic: bool
) -> None:
    client, user = _client_for(rbac_graph, role)
    if other_clinic:
        _add_clinic_b(rbac_graph, user.pk, role)
        _reauthenticate(client, user.pk, role)
    agenda_a = reverse("scheduling:agenda", kwargs={"clinic_id": rbac_graph.clinic_a})
    assert _get(client, agenda_a).status_code == OK
    _remove_in_clinic_a(rbac_graph, role, set(BUNDLES_V2[role]))
    _reauthenticate(client, user.pk, role)
    assert _granted_in_clinic_a(rbac_graph, user.pk) == frozenset()
    assert client.session[ACTIVE_CLINIC_SESSION_KEY] == str(rbac_graph.clinic_a)

    home = _get(client, "/workspace/")

    if other_clinic:
        # Only clinic B is listed and selected; clinic A is gone from the shell.
        agenda_b = reverse(
            "scheduling:agenda", kwargs={"clinic_id": rbac_graph.clinic_b}
        )
        assert home.status_code == FOUND
        assert home["Location"] == agenda_b
        html = _get(client, agenda_b).content.decode()
        assert f'<strong class="nav-clinic">{CLINIC_B_NAME}</strong>' in html
        assert CLINIC_A_NAME not in html
        assert client.session[ACTIVE_CLINIC_SESSION_KEY] == str(rbac_graph.clinic_b)
    else:
        assert home.status_code == OK
        html = home.content.decode()
        assert gettext("No clinic assigned") in html
        assert CLINIC_A_NAME not in html
        assert _modules(home) == []
        assert 'id="command-palette"' not in html
        assert ACTIVE_CLINIC_SESSION_KEY not in client.session
        assert _get(client, "/workspace/command/").status_code == NOT_FOUND
        assert _api(client, {"q": ""}).content == b"[]"
    assert _api(client, {"q": "", "clinic_id": str(rbac_graph.clinic_a)}).content == (
        b"[]"
    )
    _assert_uniform_agenda_denial(client, rbac_graph)


def test_section_row_lists_open_places_and_marks_the_current_one(
    rbac_graph: RbacGraph,
) -> None:
    client, _ = _client_for(rbac_graph, "receptionist")
    availability = f"/scheduling/clinics/{rbac_graph.clinic_a}/availability/"

    html = _get(client, availability).content.decode()

    assert 'class="shell-subnav"' in html
    assert re.search(
        r'href="' + re.escape(availability) + r'" aria-current="page" '
        r'data-tab="availability"',
        html,
    )
    assert 'data-tab="agenda"' in html
    assert re.search(r'data-module="agenda" aria-current="page"', html)
    # A section with one open place renders no row.
    retention = _get(client, f"/retention/clinics/{rbac_graph.clinic_a}/")
    assert 'class="shell-subnav"' not in retention.content.decode()


def test_a_clinic_narrowed_permission_hides_its_destination(
    rbac_graph: RbacGraph,
) -> None:
    client, user = _client_for(rbac_graph, "receptionist")
    assert "finance" in _modules(_get(client, "/auth/protected/"))

    # Finance opens on charge.collect too, so narrowing charge.read alone keeps it.
    for permission in ("charge.read", "charge.collect"):
        with owner_context(rbac_graph.organization_a):
            RoleGrant.objects.create(
                organization_id=rbac_graph.organization_a,
                clinic_id=rbac_graph.clinic_a,
                role="receptionist",
                permission=permission,
                valid_from=timezone.now() - timedelta(minutes=1),
            )
        _reauthenticate(client, user.pk, "receptionist")
        if permission == "charge.read":
            assert "finance" in _modules(_get(client, "/auth/protected/"))

    assert _modules(_get(client, "/auth/protected/")) == [
        "today",
        "agenda",
        "patients",
        "inbox",
        "messages",
        "operations",
        "settings",
    ]


@pytest.mark.parametrize(
    ("role", "status"),
    [
        ("owner", 403),
        ("finance", 403),
        ("org_admin", 403),
        ("allied_professional", 404),
        ("receptionist", OK),
    ],
)
def test_the_inbox_route_follows_the_registry_permission_not_the_role_list(
    rbac_graph: RbacGraph, role: str, status: int
) -> None:
    # The owner role is in MESSAGES_ROUTE, but the route is also gated by the
    # registry permission, which the owner bundle lacks: the shell hides the link
    # and the route refuses, so an owner never reaches patient names here.
    client, _user = _client_for(rbac_graph, role)
    response = _get(client, f"/clinics/{rbac_graph.clinic_a}/messages/")
    assert response.status_code == status


def test_registry_never_renders_an_undelivered_destination() -> None:
    undelivered = {d.key for d in DESTINATIONS if d.url_name is None}
    assert undelivered == {
        "operations",
    }
    assert [d.key for d in DESTINATIONS] == [
        "today",
        "agenda",
        "patients",
        "inbox",
        "messages",
        "finance",
        "operations",
        "automations",
        "reports",
        "settings",
    ]


def test_palette_search_filters_destinations_by_permission(
    rbac_graph: RbacGraph,
) -> None:
    physician, _ = _client_for(rbac_graph, "physician")
    receptionist, _ = _client_for(rbac_graph, "receptionist")
    body = {"q": "", "clinic_id": str(rbac_graph.clinic_a)}

    physician_rows = _api(physician, body).json()
    receptionist_rows = _api(receptionist, body).json()

    assert [row["action_url_name"] for row in physician_rows] == [
        "workflows:today",
        "scheduling:agenda",
        "scheduling:availability-list",
        "consent:staff",
        "workflows:inbox",
        "orders:results",
        # Task 55: the urgent-concern queue and the release notice wording.
        "workflows:urgent-concerns",
        "workflows:release-notices",
        "comms:inbox",
        # Task 54: Messages > Assistant handoffs.
        "ai:front-desk-handoffs",
        "retention:status",
        "ai:care-coordination",
        "identity:preferences",
    ]
    assert {row["kind"] for row in receptionist_rows} == {"destination", "action"}
    assert "intake:patient-create" in {
        row["action_url_name"] for row in receptionist_rows
    }
    assert all(
        set(row)
        == {"kind", "label", "meta", "action_url_name", "token", "href", "html"}
        for row in receptionist_rows
    )
    agenda = _api(physician, {"q": "AGEN", "clinic_id": str(rbac_graph.clinic_a)})
    # "AGEN" is also a fragment of the Portuguese label Mensagens.
    assert [row["label"] for row in agenda.json()] == [
        gettext("Agenda"),
        gettext("Messages"),
    ]


def test_patient_matches_are_exact_full_names_only(rbac_graph: RbacGraph) -> None:
    client, user = _client_for(rbac_graph, "receptionist")
    seed_patients(rbac_graph, user.pk, rbac_graph.clinic_a, (PATIENT, OTHER))
    clinic = str(rbac_graph.clinic_a)

    exact = _api(client, {"q": PATIENT.lower(), "clinic_id": clinic}).json()
    partial = _api(client, {"q": "Sintetico Navegacao", "clinic_id": clinic}).json()
    word = _api(client, {"q": "Sintetico", "clinic_id": clinic}).json()

    patients = [row for row in exact if row["kind"] == "patient"]
    assert [row["label"] for row in patients] == [PATIENT]
    years = re.fullmatch(r"(\d+) \w+", patients[0]["meta"])
    assert years is not None
    count = int(years.group(1))
    assert patients[0]["meta"] == ngettext("%(age)s year", "%(age)s years", count) % {
        "age": count
    }
    assert patients[0]["href"] is None
    assert patients[0]["action_url_name"] == "workspace-command-run"
    assert len(str(patients[0]["token"])) >= TOKEN_MIN_LENGTH
    assert partial == []
    assert word == []


@pytest.mark.parametrize(
    "role", ["finance", "org_admin", "physician", "nurse", "owner"]
)
def test_roles_without_registry_authority_never_see_patients(
    rbac_graph: RbacGraph, role: str
) -> None:
    _, reception_user = _client_for(rbac_graph, "receptionist")
    seed_patients(rbac_graph, reception_user.pk, rbac_graph.clinic_a, (PATIENT,))
    client, _ = _client_for(rbac_graph, role)

    rows = _api(client, {"q": PATIENT, "clinic_id": str(rbac_graph.clinic_a)}).json()

    assert all(row["kind"] != "patient" for row in rows)
    assert PATIENT not in json.dumps(rows)
    # Nothing outside the actor's own permitted destinations is offered.
    allowed = {
        row["action_url_name"]
        for row in _api(client, {"q": "", "clinic_id": str(rbac_graph.clinic_a)}).json()
    }
    assert {row["action_url_name"] for row in rows} <= allowed


def test_foreign_and_unknown_clinics_answer_like_no_match(
    rbac_graph: RbacGraph,
) -> None:
    client, user = _client_for(rbac_graph, "receptionist")
    seed_patients(rbac_graph, user.pk, rbac_graph.clinic_a, (PATIENT,))

    foreign = _api(client, {"q": PATIENT, "clinic_id": str(rbac_graph.clinic_c)})
    unknown = _api(client, {"q": PATIENT, "clinic_id": str(uuid4())})
    unassigned = _api(client, {"q": PATIENT, "clinic_id": str(rbac_graph.clinic_b)})
    nothing = _api(
        client,
        {"q": "Sintetico Inexistente Nome", "clinic_id": str(rbac_graph.clinic_a)},
    )

    for response in (foreign, unknown, unassigned, nothing):
        assert response.status_code == OK
        assert response.content == b"[]"


def test_search_requires_csrf_and_bounded_text(rbac_graph: RbacGraph) -> None:
    client, _ = _client_for(rbac_graph, "receptionist")

    missing = _api(client, {"q": "agenda"}, csrf=False)
    too_long = _api(client, {"q": "a" * 101, "clinic_id": str(rbac_graph.clinic_a)})

    assert missing.status_code == FORBIDDEN
    assert missing.json() == {
        "code": "csrf_failed",
        "message_key": "api.error.csrf_failed",
    }
    assert too_long.status_code == BAD_REQUEST
    assert too_long.json()["code"] == "invalid_input"


def _patient_token(client: Client, graph: RbacGraph) -> str:
    rows = _api(client, {"q": PATIENT, "clinic_id": str(graph.clinic_a)}).json()
    return str(next(row["token"] for row in rows if row["kind"] == "patient"))


def test_choosing_a_patient_pins_the_banner_without_leaking_identity(
    rbac_graph: RbacGraph,
) -> None:
    client, user = _client_for(rbac_graph, "receptionist")
    seed_patients(rbac_graph, user.pk, rbac_graph.clinic_a, (PATIENT,))
    agenda = f"/scheduling/clinics/{rbac_graph.clinic_a}/agenda/"
    _get(client, agenda)  # remember clinic A as the shell's clinic

    moved = _post(
        client, RUN, {"token": _patient_token(client, rbac_graph), "next": agenda}
    )
    page = _get(client, agenda)

    assert moved.status_code == FOUND
    assert moved["Location"] == agenda
    html = page.content.decode()
    banner = html.split("data-patient-banner", 1)[1].split("</section>", 1)[0]
    assert PATIENT in banner
    assert re.search(r"<span class=\"patient-banner-meta\">\d+ ", banner)
    title = re.search(r"<title>(.*?)</title>", html, re.DOTALL)
    assert title is not None
    assert PATIENT not in title.group(1)
    enrollment = client.session[
        f"{patient_context.SESSION_PREFIX}{rbac_graph.clinic_a}"
    ]
    assert all(enrollment not in href for href in re.findall(r'href="([^"]+)"', html))
    assert PATIENT not in moved["Location"]
    # The banner offers exactly the reception actions as POST forms.
    assert re.findall(
        r'<button class="button--quiet" type="submit">([^<]+)</button>', banner
    ) == [gettext("Book appointment"), gettext("Contacts"), gettext("Access")]

    closed = _post(client, CLOSE, {"next": agenda})
    assert closed.status_code == FOUND
    assert "data-patient-banner" not in _get(client, agenda).content.decode()


def test_unusable_tokens_share_one_denial(rbac_graph: RbacGraph) -> None:
    client, user = _client_for(rbac_graph, "receptionist")
    seed_patients(rbac_graph, user.pk, rbac_graph.clinic_a, (PATIENT,))
    other, _ = _client_for(rbac_graph, "receptionist")
    _get(client, f"/scheduling/clinics/{rbac_graph.clinic_a}/agenda/")
    _get(other, f"/scheduling/clinics/{rbac_graph.clinic_a}/agenda/")
    token = _patient_token(client, rbac_graph)

    forged = _post(client, RUN, {"token": secrets.token_urlsafe(18)})
    stolen = _post(other, RUN, {"token": token})
    empty = _post(client, RUN, {})

    for response in (forged, stolen, empty):
        assert response.status_code == NOT_FOUND
    # Same body apart from the signed-in username in the shell.
    assert _main(forged) == _main(stolen) == _main(empty)
    assert PATIENT not in stolen.content.decode()


def test_revoked_demographics_drop_the_pinned_patient(rbac_graph: RbacGraph) -> None:
    client, user = _client_for(rbac_graph, "receptionist")
    seed_patients(rbac_graph, user.pk, rbac_graph.clinic_a, (PATIENT,))
    agenda = f"/scheduling/clinics/{rbac_graph.clinic_a}/agenda/"
    _get(client, agenda)
    _post(client, RUN, {"token": _patient_token(client, rbac_graph), "next": agenda})
    assert PATIENT in _get(client, agenda).content.decode()

    with owner_context(rbac_graph.organization_a):
        RoleGrant.objects.create(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_a,
            role="receptionist",
            permission="demographics.read",
            valid_from=timezone.now() - timedelta(minutes=1),
        )

    assert PATIENT not in _get(client, agenda).content.decode()
    assert (
        f"{patient_context.SESSION_PREFIX}{rbac_graph.clinic_a}" not in client.session
    )


class _FakeGuard:
    """A bound-work guard as todo 27/40 will register; records its calls."""

    def __init__(self) -> None:
        self.checked: list[ContextSwitch] = []
        self.discarded: list[ContextSwitch] = []
        self.bound = True

    def check(self, switch: ContextSwitch) -> SwitchConsequence | None:
        self.checked.append(switch)
        if not self.bound:
            return None
        return SwitchConsequence(key="draft", message="Rascunho sintetico vinculado")

    def discard(self, switch: ContextSwitch) -> None:
        self.discarded.append(switch)
        self.bound = False


@pytest.fixture
def fake_guard() -> Iterator[_FakeGuard]:
    guard = _FakeGuard()
    register_context_switch_guard("test-bound-work", guard)
    try:
        yield guard
    finally:
        unregister_context_switch_guard("test-bound-work")


def test_bound_work_blocks_the_switch_until_discarded(
    rbac_graph: RbacGraph, fake_guard: _FakeGuard
) -> None:
    client, user = _client_for(rbac_graph, "receptionist")
    seed_patients(rbac_graph, user.pk, rbac_graph.clinic_a, (PATIENT,))
    agenda = f"/scheduling/clinics/{rbac_graph.clinic_a}/agenda/"
    _get(client, agenda)
    token = _patient_token(client, rbac_graph)
    key = f"{patient_context.SESSION_PREFIX}{rbac_graph.clinic_a}"

    held = _post(client, RUN, {"token": token, "next": agenda})
    assert held.status_code == CONFLICT
    assert "Rascunho sintetico vinculado" in held.content.decode()
    assert key not in client.session  # never retargeted
    assert fake_guard.discarded == []
    # A client-side "unsaved=discard" does not authorize server-bound work.
    still = _post(client, RUN, {"token": token, "next": agenda, "unsaved": "discard"})
    assert still.status_code == CONFLICT
    assert key not in client.session

    done = _post(
        client, RUN, {"token": token, "next": agenda, "consequence": "discard"}
    )
    assert done.status_code == FOUND
    assert len(fake_guard.discarded) == 1
    switch = fake_guard.discarded[0]
    assert switch.clinic_id == rbac_graph.clinic_a
    assert switch.from_enrollment_id is None
    assert isinstance(switch.to_enrollment_id, UUID)
    assert client.session[key] == str(switch.to_enrollment_id)


def test_next_never_returns_to_a_patient_bound_record(rbac_graph: RbacGraph) -> None:
    client, user = _client_for(rbac_graph, "receptionist")
    seed_patients(rbac_graph, user.pk, rbac_graph.clinic_a, (PATIENT,))
    _get(client, f"/scheduling/clinics/{rbac_graph.clinic_a}/agenda/")
    contacts = f"/intake/clinics/{rbac_graph.clinic_a}/contacts/"

    bound = _post(
        client, RUN, {"token": _patient_token(client, rbac_graph), "next": contacts}
    )
    external = _post(client, CLOSE, {"next": "https://example.invalid/steal"})

    assert bound["Location"] == "/workspace/"
    assert external["Location"] == "/workspace/"


def test_combobox_rows_render_server_side_with_tokens_only(
    rbac_graph: RbacGraph,
) -> None:
    client, user = _client_for(rbac_graph, "receptionist")
    seed_patients(rbac_graph, user.pk, rbac_graph.clinic_a, (PATIENT,))
    _get(client, f"/scheduling/clinics/{rbac_graph.clinic_a}/agenda/")

    with runtime_role():
        rows = client.post(OPTIONS, {"q": PATIENT}, headers=_csrf(client))
        denied = Client().post(OPTIONS, {"q": PATIENT})

    html = rows.content.decode()
    assert rows.status_code == OK
    assert 'role="option"' in html
    assert 'data-kind="patient"' in html
    assert PATIENT in html
    enrollment_ids = re.findall(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", html
    )
    assert enrollment_ids == []
    assert denied.status_code == FORBIDDEN
    assert PATIENT not in denied.content.decode()


def test_palette_page_is_the_no_javascript_baseline(rbac_graph: RbacGraph) -> None:
    client, user = _client_for(rbac_graph, "receptionist")
    seed_patients(rbac_graph, user.pk, rbac_graph.clinic_a, (PATIENT,))
    _get(client, f"/scheduling/clinics/{rbac_graph.clinic_a}/agenda/")

    blank = _get(client, "/workspace/command/")
    searched = _post(client, "/workspace/command/", {"q": PATIENT})

    assert blank.status_code == OK
    html = searched.content.decode()
    assert PATIENT in html
    assert 'name="token"' in html
    assert f'action="{RUN}"' in html
    title = re.search(r"<title>(.*?)</title>", html, re.DOTALL)
    assert title is not None
    assert PATIENT not in title.group(1)


def _enrollment_of(client: Client, graph: RbacGraph, name: str) -> str:
    """Pin ``name`` through the palette and read the session's enrollment."""
    token = next(
        row["token"]
        for row in _api(client, {"q": name, "clinic_id": str(graph.clinic_a)}).json()
        if row["kind"] == "patient"
    )
    _post(client, RUN, {"token": str(token)})
    return str(client.session[f"{patient_context.SESSION_PREFIX}{graph.clinic_a}"])


def test_record_pages_show_only_their_own_patient(rbac_graph: RbacGraph) -> None:
    client, user = _client_for(rbac_graph, "receptionist")
    seed_patients(rbac_graph, user.pk, rbac_graph.clinic_a, (PATIENT, OTHER))
    agenda = f"/scheduling/clinics/{rbac_graph.clinic_a}/agenda/"
    contacts = f"/intake/clinics/{rbac_graph.clinic_a}/contacts/"
    _get(client, agenda)
    other = _enrollment_of(client, rbac_graph, OTHER)
    _enrollment_of(client, rbac_graph, PATIENT)  # PATIENT is now in context

    # A record page renders its own patient in the banner, never the context.
    record = _post(client, contacts, {"action": "manage", "enrollment_id": other})
    banner = _main_banner(record)
    assert OTHER in banner
    assert PATIENT not in banner
    # A record route that bound no patient (the blank entry) shows no banner.
    assert "data-patient-banner" not in _get(client, contacts).content.decode()
    # The session context followed the record the user opened.
    assert OTHER in _main_banner(_get(client, agenda))


def _main_banner(response: _MonkeyPatchedWSGIResponse) -> str:
    html = response.content.decode()
    assert "data-patient-banner" in html
    return html.split("data-patient-banner", 1)[1].split("</section>", 1)[0]


def test_tokens_are_bound_to_the_clinic_they_were_issued_in(
    rbac_graph: RbacGraph,
) -> None:
    client, user = _client_for(rbac_graph, "receptionist")
    with owner_context(rbac_graph.organization_a):
        UserClinicRole.objects.create(
            user_id=user.pk,
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_b,
            role="receptionist",
        )
    _reauthenticate(client, user.pk, "receptionist")
    seed_patients(rbac_graph, user.pk, rbac_graph.clinic_a, (PATIENT,))
    _get(client, f"/scheduling/clinics/{rbac_graph.clinic_a}/agenda/")
    token = _patient_token(client, rbac_graph)
    # The shell moves to clinic B; the clinic-A token must not act there.
    _get(client, f"/scheduling/clinics/{rbac_graph.clinic_b}/agenda/")

    moved = _post(client, RUN, {"token": token})

    assert moved.status_code == NOT_FOUND
    assert (
        f"{patient_context.SESSION_PREFIX}{rbac_graph.clinic_b}" not in client.session
    )


def test_the_shell_adds_no_live_region_role_to_workspace_pages(
    rbac_graph: RbacGraph,
) -> None:
    # Page-level [role=status]/[role=alert] stay unique: the palette's status
    # line is aria-live, and forms/buttons appear only with a patient banner.
    client, _ = _client_for(rbac_graph, "receptionist")
    html = _get(client, f"/scheduling/clinics/{rbac_graph.clinic_a}/agenda/")
    shell = re.sub(r"<main.*?</main>", "", html.content.decode(), flags=re.DOTALL)

    assert 'id="command-palette"' in shell
    assert 'role="status"' not in shell
    assert 'role="alert"' not in shell
    assert "<form" not in shell
    assert 'aria-live="polite"' in shell


def test_denial_pages_never_show_the_pinned_patient(rbac_graph: RbacGraph) -> None:
    client, user = _client_for(rbac_graph, "receptionist")
    seed_patients(rbac_graph, user.pk, rbac_graph.clinic_a, (PATIENT,))
    agenda = f"/scheduling/clinics/{rbac_graph.clinic_a}/agenda/"
    _get(client, agenda)
    _enrollment_of(client, rbac_graph, PATIENT)
    assert PATIENT in _get(client, agenda).content.decode()

    foreign = _get(client, f"/scheduling/clinics/{rbac_graph.clinic_b}/agenda/")
    missing = _post(client, RUN, {"token": "unknown"})

    for response in (foreign, missing):
        assert response.status_code == NOT_FOUND
        assert PATIENT not in response.content.decode()
        assert "data-patient-banner" not in response.content.decode()
    # The context itself survives: the next own-clinic page pins it again.
    assert PATIENT in _get(client, agenda).content.decode()

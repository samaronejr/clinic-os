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
from apps.core.navigation import DESTINATIONS
from apps.core.patient_context import (
    ContextSwitch,
    SwitchConsequence,
    register_context_switch_guard,
    unregister_context_switch_guard,
)
from apps.identity.models import RoleGrant, User, UserClinicRole
from django.conf import settings
from django.test import Client
from django.utils import timezone
from django.utils.translation import gettext, ngettext

from auth.stepup_test_support import create_role_actor
from identity.permission_support import owner_context
from otp_test_support import (
    create_totp_device,
    fixed_otp_time,
    get_totp_device,
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
PRIVILEGED: Final = frozenset({"physician", "clinic_admin", "owner"})
# The exact top-level set per role (plan annex IA, RP bundles v1, and the
# route guards the destinations lead to today). Roles whose routes are not
# migrated to permissions yet have no workspace clinic and no destinations.
EXPECTED: Final = {
    "owner": ["agenda", "patients", "finance", "operations", "settings"],
    "clinic_admin": ["agenda", "patients", "finance", "operations", "settings"],
    "receptionist": ["agenda", "patients", "finance", "operations"],
    "physician": ["agenda", "patients", "operations"],
    "nurse": [],
    "allied_professional": [],
    "scheduler": [],
    "clinic_manager": [],
    "finance": [],
    "org_admin": [],
}
TOKEN_MIN_LENGTH: Final = 20
UNDELIVERED: Final = frozenset({"today", "inbox", "messages", "automations", "reports"})


def _client_for(graph: RbacGraph, role: str) -> tuple[Client, User]:
    user = create_role_actor(graph, UserClinicRole.Role(role))
    client = Client(enforce_csrf_checks=True, raise_request_exception=False)
    client.cookies[settings.CSRF_COOKIE_NAME] = secrets.token_hex(16)
    with runtime_role():
        login_client = Client()
        login(login_client, user.username, password=RBAC_RAW_CREDENTIAL)
        if role in PRIVILEGED:
            create_totp_device(user.pk, confirmed=True)
            with fixed_otp_time():
                device = get_totp_device(user.pk, confirmed=True)
                login_client.post(
                    "/auth/verify/",
                    {
                        "otp_device": device.persistent_id,
                        "otp_token": token_for(device),
                        "next": "/auth/protected/",
                    },
                )
    client.cookies[settings.SESSION_COOKIE_NAME] = login_client.cookies[
        settings.SESSION_COOKIE_NAME
    ].value
    return client, user


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
    # The palette exists exactly when a workspace clinic does.
    assert ('data-command-open="command-palette"' in html) is bool(EXPECTED[role])
    assert ('id="command-palette"' in html) is bool(EXPECTED[role])


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
    assert receptionist_links["finance"] == f"/billing/clinics/{clinic}/charges/"


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
    client, _ = _client_for(rbac_graph, "receptionist")
    assert "finance" in _modules(_get(client, "/auth/protected/"))

    with owner_context(rbac_graph.organization_a):
        RoleGrant.objects.create(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_a,
            role="receptionist",
            permission="charge.read",
            valid_from=timezone.now() - timedelta(minutes=1),
        )

    assert _modules(_get(client, "/auth/protected/")) == [
        "agenda",
        "patients",
        "operations",
    ]


def test_registry_never_renders_an_undelivered_destination() -> None:
    undelivered = {d.key for d in DESTINATIONS if d.url_name is None}
    assert undelivered == {
        "today",
        "inbox",
        "messages",
        "operations",
        "automations",
        "reports",
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
        "scheduling:agenda",
        "scheduling:availability-list",
        "consent:staff",
        "retention:status",
        "identity:preferences",
    ]
    assert {row["kind"] for row in receptionist_rows} == {"destination", "action"}
    assert "intake:patient-create" in {
        row["action_url_name"] for row in receptionist_rows
    }
    assert all(
        set(row) == {"kind", "label", "meta", "action_url_name", "token", "href"}
        for row in receptionist_rows
    )
    agenda = _api(physician, {"q": "AGEN", "clinic_id": str(rbac_graph.clinic_a)})
    assert [row["label"] for row in agenda.json()] == [gettext("Agenda")]


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


@pytest.mark.parametrize("role", ["finance", "physician", "nurse", "owner"])
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

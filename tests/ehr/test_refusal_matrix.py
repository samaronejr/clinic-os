"""Every refused EHR write leaves no committed row (plan item 27 round 3, B1).

The tenant middleware commits any response below 500, so a refusal decided
after a write keeps that write. This matrix derives the EHR routes from the
URL resolver and the POST actions from each workspace's own action table,
then drives every action in every mode (native form POST, htmx form POST,
JSON UI API) as actors who must be refused. After each refusal a superuser
snapshot of every table in ``clinic_app`` and ``public`` must be unchanged,
with one accepted exception: the fixed, metadata-only ``ehr.access.denied``
refusal record that the record contract requires once a denial reaches a
clinical object (gate-review-r2, answer 4), and on the teleconsult route
only, that app's equivalent ``teleconsult.access.denied``. No other audit
event, domain row, session row or session cookie may survive a refusal.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import psycopg
import pytest
from apps.core.integration import register_send_adapter
from apps.ehr import attachment_views, history_views, views
from apps.ehr.addenda import open_addendum
from apps.ehr.finalization import finalize_version
from apps.ehr.history import HistoryChange, save_history
from apps.ehr.models import (
    ClinicalDocumentVersion,
    Encounter,
    Problem,
    SpecialtyTemplate,
)
from apps.ehr.services import create_draft
from apps.teleconsult.adapters import SyntheticRoomAdapter
from apps.teleconsult.workspace import NOTE_ACTIONS
from django.conf import settings
from django.urls import URLPattern, URLResolver, get_resolver
from psycopg import sql

from auth.stepup_test_support import verified_request
from ehr.test_addenda import clinician, enrollment_of, signed_in
from ehr.test_autosave import (
    SECTIONS,
    as_actor,
    open_walk_in,
    registration,
    save,
)
from identity.permission_support import owner_context
from renewal.test_teleconsult_sessions import _create
from renewal.test_teleconsult_sessions import seed as teleconsult_seed

if TYPE_CHECKING:
    from collections.abc import Iterator

    from django.test import Client
    from django.test.client import _MonkeyPatchedWSGIResponse
    from pytest_django.fixtures import SettingsWrapper

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)

DENIAL = "ehr.access.denied"
# The teleconsult route refuses a non-assigned session with its own fixed
# metadata-only record (apps/teleconsult/services._denied) before any EHR
# code runs; it is accepted there only.
TELECONSULT_DENIAL = "teleconsult.access.denied"
ENCOUNTER = "ehr:encounter"
HISTORY = "ehr:history"
ATTACHMENTS = "ehr:attachments"
# The teleconsult notes panel saves EHR notes in htmx mode.
TELECONSULT = "teleconsult:staff"
AUTOSAVE = "ui_api:ehr-autosave"
ADDENDUM_OPEN = "ui_api:ehr-addendum-open"
ADDENDUM_AUTOSAVE = "ui_api:ehr-addendum-autosave"
FORM_ACTIONS: dict[str, frozenset[str]] = {
    ENCOUNTER: views.POST_ACTIONS,
    HISTORY: history_views.POST_ACTIONS,
    ATTACHMENTS: attachment_views.POST_ACTIONS,
    TELECONSULT: NOTE_ACTIONS,
}
API_ROUTES = (AUTOSAVE, ADDENDUM_OPEN, ADDENDUM_AUTOSAVE)
FORM_MODES = ("native", "htmx")
# Requests these actors are entitled to make; each must succeed, so this list
# cannot hide a refusal. Everything else in the matrix must be refused.
PERMITTED = frozenset(
    {
        ("reader", ENCOUNTER, "review"),
        ("reader", ENCOUNTER, "current"),
        ("reader", ENCOUNTER, "open_unscheduled"),
        ("reader", HISTORY, "open"),
        ("reader", ATTACHMENTS, "open"),
        ("reader", ADDENDUM_OPEN, ""),
        ("staff", ENCOUNTER, "current"),
    }
)


@dataclass(frozen=True)
class World:
    """One finalized note on a teleconsult encounter, and every selector."""

    users: dict[str, UUID]
    form: dict[str, str]
    api: dict[str, dict[str, object]]
    # The assignee's own records bound to another encounter of the patient.
    foreign: dict[str, str] = field(default_factory=dict)


@pytest.fixture
def synthetic_room(settings: SettingsWrapper) -> None:
    settings.TELECONSULT_SYNTHETIC_PROVIDER = True
    register_send_adapter(SyntheticRoomAdapter())


def route_names() -> dict[str, str]:
    """Every resolver route as ``namespace:name`` -> the view's module."""
    found: dict[str, str] = {}

    def walk(patterns: list[URLPattern | URLResolver], prefix: str) -> None:
        for pattern in patterns:
            if isinstance(pattern, URLResolver):
                space = f"{prefix}{pattern.namespace}:" if pattern.namespace else prefix
                walk(pattern.url_patterns, space)
            elif pattern.name:
                view = getattr(pattern.callback, "view_class", pattern.callback)
                found[f"{prefix}{pattern.name}"] = view.__module__

    walk(get_resolver().url_patterns, "")
    return found


def world(graph: RbacGraph) -> World:
    appointment, encounter, _consent, _patient, _manager = teleconsult_seed(graph)
    with owner_context(graph.organization_a):
        template = SpecialtyTemplate.objects.get(clinic_id=graph.clinic_a)
    enrolled = enrollment_of(graph, encounter.patient_id, graph.clinic_a)
    registration(graph, graph.physician, graph.clinic_a)
    request = verified_request(graph.physician)
    with as_actor(graph, graph.physician):
        version = create_draft(
            clinic_id=graph.clinic_a,
            encounter_id=encounter.pk,
            template_id=template.pk,
        )
        save(graph, version.pk, 1)
        finalize_version(
            clinic_id=graph.clinic_a,
            version_id=version.pk,
            expected_revision=2,
            request=request,
        )
        foreign_visit = open_walk_in(graph, enrolled)
        foreign = create_draft(
            clinic_id=graph.clinic_a,
            encounter_id=foreign_visit.pk,
            template_id=template.pk,
        )
    session = _create(graph, encounter)
    reader = clinician(graph, "physician", enrolled)
    colleague = clinician(graph, "physician", enrolled)
    staff = clinician(graph, "receptionist", enrolled)
    with as_actor(graph, reader):
        # Authoring a note for the patient makes the reader a care reader.
        visit = open_walk_in(graph, enrolled)
        create_draft(
            clinic_id=graph.clinic_a, encounter_id=visit.pk, template_id=template.pk
        )
        entry = save_history(
            clinic_id=graph.clinic_a,
            encounter_id=visit.pk,
            change=HistoryChange(
                kind="problem",
                expected_revision=0,
                state="documented",
                description="Sintetico problema",
                status="active",
                reason="Sintetico motivo",
            ),
        )
    with owner_context(graph.organization_a):
        entry_id = Problem.objects.get(assessment=entry).entry_id
    with as_actor(graph, colleague):
        addendum = open_addendum(clinic_id=graph.clinic_a, encounter_id=encounter.pk)
    clinic = str(graph.clinic_a)
    return World(
        users={
            "reader": reader,
            "staff": staff,
            "assignee": graph.physician,
        },
        form={
            "version_id": str(version.pk),
            "encounter_id": str(encounter.pk),
            "appointment_id": str(appointment.pk),
            "enrollment_id": str(enrolled),
            "template_id": str(template.pk),
            "episode_id": str(uuid4()),
            "session_id": str(session.pk),
            "attachment_id": str(uuid4()),
            "entry_id": str(entry_id),
            "revision": "2",
            "merge_revision": "2",
            "title": "Sintetico episodio",
            "kind": "problem",
            "state": "documented",
            "status": "active",
            "description": "Sintetico descricao",
            **SECTIONS,
        },
        api={
            AUTOSAVE: {
                "clinic_id": clinic,
                "version_id": str(version.pk),
                "expected_revision": 2,
                "editor_session": str(uuid4()),
                "sections": SECTIONS,
            },
            ADDENDUM_OPEN: {"clinic_id": clinic, "encounter_id": str(encounter.pk)},
            ADDENDUM_AUTOSAVE: {
                "clinic_id": clinic,
                "addendum_id": str(addendum.pk),
                "expected_revision": 1,
                "text": "Sintetico complemento",
            },
        },
        foreign={"version_id": str(foreign.pk), "entry_id": str(entry_id)},
    )


@dataclass(frozen=True)
class Case:
    actor: str
    route: str
    action: str
    mode: str
    overrides: dict[str, str] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return f"{self.actor} {self.route} {self.action or '-'} {self.mode}"


def cases(built: World) -> Iterator[Case]:
    for actor in ("reader", "staff"):
        for route, actions in FORM_ACTIONS.items():
            for action in sorted(actions):
                for mode in FORM_MODES:
                    yield Case(actor, route, action, mode)
        for route in API_ROUTES:
            yield Case(actor, route, "", "json")
    # The assignee may read both versions but a posted binding to another
    # encounter (or another encounter's history entry) is refused.
    foreign_version = {"version_id": built.foreign["version_id"]}
    for mode in FORM_MODES:
        yield Case("assignee", ENCOUNTER, "review", mode, foreign_version)
        for action in sorted(NOTE_ACTIONS - {"note-template"}):
            yield Case("assignee", TELECONSULT, action, mode, foreign_version)
        yield Case("assignee", HISTORY, "edit", mode)
    yield Case("assignee", ADDENDUM_AUTOSAVE, "", "json")


def snapshot(url: str) -> tuple[dict[str, str], int]:
    """Digest every committed row outside the audit ledger, plus its tail."""
    with psycopg.connect(url) as connection:
        tables = connection.execute(
            "SELECT n.nspname, c.relname FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.relkind IN ('r', 'p') "
            "AND n.nspname IN ('clinic_app', 'public') "
            "AND c.oid <> 'clinic_app.audit_event'::regclass ORDER BY 1, 2"
        ).fetchall()
        digests = connection.execute(
            sql.SQL(" UNION ALL ").join(
                sql.SQL(
                    "SELECT {label}, md5(coalesce(string_agg(md5(r::text), ',' "
                    "ORDER BY md5(r::text)), '')) FROM {table} r"
                ).format(
                    label=sql.Literal(f"{schema}.{name}"),
                    table=sql.Identifier(schema, name),
                )
                for schema, name in tables
            )
        ).fetchall()
        tail = connection.execute(
            "SELECT coalesce(max(seq), 0) FROM clinic_app.audit_event"
        ).fetchone()
    assert tail is not None
    return {str(name): str(digest) for name, digest in digests}, int(tail[0])


def audit_after(url: str, tail: int) -> list[str]:
    with psycopg.connect(url) as connection:
        return [
            str(row[0])
            for row in connection.execute(
                "SELECT event_type FROM clinic_app.audit_event WHERE seq > %s "
                "ORDER BY seq",
                [tail],
            ).fetchall()
        ]


def send(
    client: Client, built: World, case: Case, clinic: UUID
) -> _MonkeyPatchedWSGIResponse:
    if case.mode == "json":
        body: dict[str, object] = {**built.api[case.route], **case.overrides}
        body.setdefault("editor_command_id", str(uuid4()))
        path = {
            AUTOSAVE: "/api/ui/v1/ehr/autosave/",
            ADDENDUM_OPEN: "/api/ui/v1/ehr/addendum/open/",
            ADDENDUM_AUTOSAVE: "/api/ui/v1/ehr/addendum/autosave/",
        }[case.route]
        return client.post(path, json.dumps(body), "application/json")
    path = {
        ENCOUNTER: f"/ehr/clinics/{clinic}/encounter/",
        HISTORY: f"/ehr/clinics/{clinic}/history/",
        ATTACHMENTS: f"/ehr/clinics/{clinic}/attachments/",
        TELECONSULT: f"/teleconsult/clinics/{clinic}/",
    }[case.route]
    reason = "walk_in" if case.action == "open_unscheduled" else "Sintetico motivo"
    body = {**built.form, "reason": reason, "action": case.action, **case.overrides}
    headers = {"HX-Request": "true"} if case.mode == "htmx" else {}
    return client.post(path, body, headers=headers)


def test_matrix_covers_every_ehr_route_and_action() -> None:
    names = route_names()
    ehr = {
        name
        for name, module in names.items()
        if module.startswith("apps.ehr.") or name.startswith("ui_api:ehr-")
    }
    assert ehr == set(FORM_ACTIONS) - {TELECONSULT} | set(API_ROUTES)
    assert names[TELECONSULT] == "apps.teleconsult.views"
    assert {route for _actor, route, _action in PERMITTED} <= ehr


def test_every_refused_ehr_write_leaves_no_committed_row(
    rbac_graph: RbacGraph,
    superuser_database_url: str,
    synthetic_room: None,
) -> None:
    built = world(rbac_graph)
    planned = list(cases(built))
    failures: list[str] = []
    refused = 0
    for actor in ("reader", "staff", "assignee"):
        with signed_in(built.users[actor]) as client:
            # The first request after sign-in stores the default active clinic,
            # even for an unknown-clinic refusal; it belongs to the sign-in.
            client.get(f"/ehr/clinics/{rbac_graph.clinic_a}/encounter/")
            for case in (c for c in planned if c.actor == actor):
                before, tail = snapshot(superuser_database_url)
                response = send(client, built, case, rbac_graph.clinic_a)
                if (case.actor, case.route, case.action) in PERMITTED:
                    if response.status_code >= 400:
                        failures.append(f"{case.label}: {response.status_code}")
                    continue
                refused += 1
                allowed = {DENIAL, TELECONSULT_DENIAL}
                if case.route != TELECONSULT:
                    allowed = {DENIAL}
                after, _tail = snapshot(superuser_database_url)
                changed = sorted(t for t in after if after[t] != before.get(t))
                events = audit_after(superuser_database_url, tail)
                if (
                    not 400 <= response.status_code < 500
                    or changed
                    or set(events) - allowed
                    or settings.SESSION_COOKIE_NAME in response.cookies
                ):
                    failures.append(
                        f"{case.label}: {response.status_code} "
                        f"tables={changed} audit={events}"
                    )
    assert not failures, "\n".join(failures)
    # Every derived action ran as a refusal for at least one actor.
    assert refused == len(planned) - sum(
        (c.actor, c.route, c.action) in PERMITTED for c in planned
    )
    with owner_context(rbac_graph.organization_a):
        assert (
            ClinicalDocumentVersion.objects.get(pk=built.form["version_id"]).state
            == "finalized"
        )
        assert (
            Encounter.objects.get(pk=built.form["encounter_id"]).state
            == Encounter.State.OPEN
        )

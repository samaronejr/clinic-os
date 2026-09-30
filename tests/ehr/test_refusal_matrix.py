"""Every refused EHR write leaves no committed row (plan item 27 round 3, B1).

The tenant middleware commits any response below 500, so a refusal decided
after a write keeps that write. This matrix derives the EHR routes from every
pattern of the URL resolver, by view and mount rather than by name, and the
POST actions from each workspace's action table plus every ``action`` control
its templates and scripts post (gate-review-r3, R3-B2); an unclassified route
or action fails closed. It drives every action in every mode (native form
POST, htmx form POST, JSON UI API) as actors who must be refused. After each
refusal a superuser snapshot of every table in ``clinic_app`` and ``public``
must be unchanged,
with one accepted exception: the fixed, metadata-only ``ehr.access.denied``
refusal record that the record contract requires once a denial reaches a
clinical object (gate-review-r2, answer 4), and on the teleconsult route
only, that app's equivalent ``teleconsult.access.denied``. No other audit
event, domain row, session row or session cookie may survive a refusal.

The bound author with a stale step-up is refused too: finalizing answers with
the step-up challenge and must leave the session exactly as it was
(gate-review-r3, R3-B1).
"""

from __future__ import annotations

import inspect
import json
import re
import types
from collections import Counter
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import psycopg
import pytest
from apps.core.api.views import (
    AddendumAutosaveView,
    AddendumOpenView,
    DraftAutosaveView,
)
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
from apps.identity.clinic_settings_views import clinic_settings
from apps.identity.stepup import STEP_UP_SESSION_KEY
from apps.intake.views import patient_access_url, patient_contacts_url
from apps.prescription.views import (
    draft_workspace,
    patient_documents_view,
    review_view,
    signing_status_view,
)
from apps.retention.views import retention_workspace
from apps.teleconsult.adapters import SyntheticRoomAdapter
from apps.teleconsult.views import staff_teleconsult
from apps.teleconsult.workspace import NOTE_ACTIONS, encounter_url, staff_url
from django.conf import settings
from django.template import Engine, engines
from django.template.backends.django import DjangoTemplates
from django.template.base import FilterExpression
from django.template.loader_tags import ExtendsNode, IncludeNode
from django.urls import URLResolver, get_resolver, resolve, reverse
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
    from collections.abc import Callable, Iterator

    from django.test import Client
    from django.test.client import _MonkeyPatchedWSGIResponse
    from django.urls import URLPattern
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
# Routes are identified by their view and mount, never by URL name.
ROUTE_VIEWS: dict[str, object] = {
    ENCOUNTER: views.encounter_workspace,
    HISTORY: history_views.history_workspace,
    ATTACHMENTS: attachment_views.attachment_workspace,
    TELECONSULT: staff_teleconsult,
    AUTOSAVE: DraftAutosaveView,
    ADDENDUM_OPEN: AddendumOpenView,
    ADDENDUM_AUTOSAVE: AddendumAutosaveView,
}
# Other views that reference EHR code; each writes only its own domain, which
# its own suite covers. A new view reaching EHR code must be classified here.
OUTSIDE_EHR: dict[object, str] = {
    clinic_settings: "clinic-admin template configuration, no clinical record",
    draft_workspace: "prescription drafts, catches the EHR refusal types",
    review_view: "prescription review, catches the EHR refusal types",
    signing_status_view: "prescription signing, catches the EHR refusal types",
    patient_documents_view: "patient prescription downloads",
    retention_workspace: "retention holds and exports",
}
# Each form route's action table. The EHR workspaces dispatch from theirs, so
# every action their UI posts must be in it; teleconsult's full table is local
# to its view, so its UI-posted actions are driven as found.
FORM_ACTIONS: dict[str, frozenset[str]] = {
    ENCOUNTER: views.POST_ACTIONS,
    HISTORY: history_views.POST_ACTIONS,
    ATTACHMENTS: attachment_views.POST_ACTIONS,
    TELECONSULT: NOTE_ACTIONS,
}
TABLED = (ENCOUNTER, HISTORY, ATTACHMENTS)
# The templates each form route renders; every response below is checked
# against their include/extends closure, so an unlisted page fails.
ROUTE_TEMPLATES: dict[str, tuple[str, ...]] = {
    ENCOUNTER: ("ehr/encounter.html",),
    HISTORY: ("ehr/history.html",),
    ATTACHMENTS: ("ehr/attachments.html",),
    TELECONSULT: (
        "teleconsult/staff.html",
        "teleconsult/clinician.html",
        "teleconsult/partials/clinician_notes.html",
        "teleconsult/partials/clinician_session.html",
    ),
}
SHARED_TEMPLATES = ("403.html",)
# Context variables forms post to, resolved through the functions that build them.
URL_VARIABLES: dict[str, Callable[[UUID], str]] = {
    "staff_url": staff_url,
    "record_url": encounter_url,
    "contacts_url": patient_contacts_url,
    "access_url": patient_access_url,
}
# Project scripts that post an action, and the form target they post to (the
# clinician poller reads ``data-status-url``, rendered from ``staff_url``).
SCRIPT_TARGETS = {"js/teleconsult-clinician.js": "{{ staff_url }}"}
THIRD_PARTY_SCRIPTS = "vendor/"
URL_TAG = re.compile(r"\{%\s*url\s+'(?P<name>[^']+)'(?P<args>[^%]*)%\}")
URL_VARIABLE = re.compile(r"\{\{\s*(?P<name>\w+)\s*\}\}")
ACTION_VALUE = re.compile(r"[a-z][a-z_-]*")
SCRIPT_ACTION = re.compile(
    r"""\.(?:set|append)\(\s*["']action["']\s*,\s*["'](?P<value>[a-z][a-z_-]*)["']\s*\)"""
)
SCRIPT_ACTION_KEY = re.compile(r"""["']action["']""")
API_ROUTES = (AUTOSAVE, ADDENDUM_OPEN, ADDENDUM_AUTOSAVE)
FORM_MODES = ("native", "htmx")
# The assignee again, signed in with a step-up verified long ago.
STALE = "stale-assignee"
STEP_UP_PATH = "/auth/step-up/"
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


@dataclass(frozen=True)
class Mount:
    path: str
    view: object


def mounts() -> list[Mount]:
    """Every resolver pattern, named or not, as its full path and its view."""
    found: list[Mount] = []

    def walk(patterns: list[URLPattern | URLResolver], prefix: str) -> None:
        for pattern in patterns:
            path = f"{prefix}{pattern.pattern}"
            if isinstance(pattern, URLResolver):
                walk(pattern.url_patterns, path)
            else:
                view = getattr(pattern.callback, "view_class", pattern.callback)
                found.append(Mount(path, view))

    walk(get_resolver().url_patterns, "")
    return found


def route_path(route: str, clinic: UUID) -> str:
    """The one live mount of a matrix route, filled in for ``clinic``."""
    (mount,) = [m for m in mounts() if m.view is ROUTE_VIEWS[route]]
    path = mount.path.replace("<uuid:clinic_id>", str(clinic))
    assert "<" not in path, mount.path
    return f"/{path}"


def reaches_ehr(view: object) -> bool:
    """Whether a view lives in, or directly references, the EHR app."""
    if str(getattr(view, "__module__", "")).startswith("apps.ehr."):
        return True
    functions = (
        [
            member
            for klass in view.__mro__
            if klass.__module__.startswith("apps.")
            for member in vars(klass).values()
            if isinstance(member, types.FunctionType)
        ]
        if isinstance(view, type)
        else [inspect.unwrap(view)]
        if callable(view)
        else []
    )
    assert functions, f"uninspectable view {view!r}"
    for function in functions:
        codes = [function.__code__]
        while codes:
            code = codes.pop()
            codes.extend(c for c in code.co_consts if isinstance(c, types.CodeType))
            for name in code.co_names:
                target = function.__globals__.get(name)
                module = (
                    target.__name__
                    if isinstance(target, types.ModuleType)
                    else getattr(target, "__module__", None)
                )
                if isinstance(module, str) and module.startswith("apps.ehr"):
                    return True
    return False


def django_engine() -> Engine:
    backend = engines["django"]
    assert isinstance(backend, DjangoTemplates)
    return backend.engine


def closure(names: tuple[str, ...]) -> set[str]:
    """The templates ``names`` include or extend; a dynamic name fails closed."""
    engine = django_engine()
    seen: set[str] = set()
    pending = list(names)
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        nodelist = engine.get_template(name).nodelist
        expressions = [
            node.template
            for node in nodelist.get_nodes_by_type(IncludeNode)
            if isinstance(node, IncludeNode)
        ] + [
            node.parent_name
            for node in nodelist.get_nodes_by_type(ExtendsNode)
            if isinstance(node, ExtendsNode)
        ]
        for expression in expressions:
            assert isinstance(expression, FilterExpression), name
            assert isinstance(expression.var, str), f"{name}: {expression.token}"
            pending.append(expression.var)
    return seen


class ActionControls(HTMLParser):
    """Every control named ``action`` in a template, with where it posts."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.form: set[str] = set()
        # (value, targets); no target means the page's own route.
        self.found: list[tuple[str, set[str]]] = []
        self.opaque: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: value or "" for key, value in attrs}
        if tag == "form":
            self.form = {
                attributes[key] for key in ("action", "hx-post") if attributes.get(key)
            }
        if "action" in attributes.get("hx-vals", ""):
            self.opaque.append(f"hx-vals on <{tag}> line {self.getpos()[0]}")
        if attributes.get("name") == "action":
            own = {
                attributes[key]
                for key in ("formaction", "hx-post")
                if attributes.get(key)
            }
            self.found.append((attributes.get("value", ""), own or self.form))

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self.form = set()


def target_view(target: str) -> object:
    """The view a form target reaches; any other spelling fails closed."""
    if match := URL_TAG.fullmatch(target):
        args = match["args"].split()
        path = reverse(
            match["name"],
            args=[uuid4() for arg in args if "=" not in arg] or None,
            kwargs={arg.split("=")[0]: uuid4() for arg in args if "=" in arg} or None,
        )
    elif (match := URL_VARIABLE.fullmatch(target)) and match["name"] in URL_VARIABLES:
        path = URL_VARIABLES[match["name"]](uuid4())
    else:
        msg = f"unresolvable form target {target!r}"
        raise AssertionError(msg)
    view = resolve(path).func
    return getattr(view, "view_class", view)


def posted_routes(targets: set[str], rendering: set[str]) -> set[str]:
    """The matrix routes a control posts to; no target is the page's route."""
    if not targets:
        return rendering
    reached = {target_view(target) for target in targets}
    return {route for route, view in ROUTE_VIEWS.items() if view in reached}


def ui_actions() -> dict[str, set[str]]:
    """Every action a template or project script posts to each matrix route."""
    closures = {route: closure(roots) for route, roots in ROUTE_TEMPLATES.items()}
    found: dict[str, set[str]] = {}
    problems: list[str] = []
    posts: list[tuple[str, str, set[str]]] = []
    (root,) = map(Path, django_engine().dirs)
    for path in sorted(root.rglob("*.html")):
        name = path.relative_to(root).as_posix()
        parser = ActionControls()
        parser.feed(path.read_text(encoding="utf-8"))
        parser.close()
        problems += [f"{name}: {opaque}" for opaque in parser.opaque]
        rendering = {route for route, names in closures.items() if name in names}
        posts += [
            (f"{name}: {value!r}", value, posted_routes(targets, rendering))
            for value, targets in parser.found
        ]
    for static in map(Path, settings.STATICFILES_DIRS):
        for path in sorted(static.rglob("*.js")):
            name = path.relative_to(static).as_posix()
            text = path.read_text(encoding="utf-8")
            values = [m["value"] for m in SCRIPT_ACTION.finditer(text)]
            if name.startswith(THIRD_PARTY_SCRIPTS) or not SCRIPT_ACTION_KEY.search(
                text
            ):
                continue
            if (
                len(SCRIPT_ACTION_KEY.findall(text)) != len(values)
                or name not in SCRIPT_TARGETS
            ):
                problems.append(f"{name}: unclassified action key")
                continue
            routes = posted_routes({SCRIPT_TARGETS[name]}, set())
            posts += [(f"{name}: {value!r}", value, routes) for value in values]
    for where, value, routes in posts:
        for route in routes:
            if ACTION_VALUE.fullmatch(value):
                found.setdefault(route, set()).add(value)
            else:
                problems.append(f"{where} posted to {route}")
    assert not problems, "\n".join(problems)
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
            STALE: graph.physician,
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


def cases(built: World, ui: dict[str, set[str]]) -> Iterator[Case]:
    for actor in ("reader", "staff"):
        for route, table in FORM_ACTIONS.items():
            for action in sorted(table | ui.get(route, set())):
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
    # Finalizing the author's own draft, and the teleconsult-bound note, with
    # a stale step-up: the challenge answers and nothing may be written.
    for mode in FORM_MODES:
        yield Case(STALE, ENCOUNTER, "finalize", mode, foreign_version)
        yield Case(STALE, TELECONSULT, "note-finalize", mode)


def is_refusal(case: Case, response: _MonkeyPatchedWSGIResponse) -> bool:
    """A plain refusal is a 4xx; a stale step-up answers with the challenge."""
    if case.actor != STALE:
        return 400 <= response.status_code < 500
    if case.mode == "htmx":
        return response.status_code == 204 and response.headers.get(
            "HX-Redirect", ""
        ).startswith(STEP_UP_PATH)
    return response.status_code == 302 and response.headers.get(
        "Location", ""
    ).startswith(STEP_UP_PATH)


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
        return client.post(
            route_path(case.route, clinic), json.dumps(body), "application/json"
        )
    path = route_path(case.route, clinic)
    reason = "walk_in" if case.action == "open_unscheduled" else "Sintetico motivo"
    body = {**built.form, "reason": reason, "action": case.action, **case.overrides}
    headers = {"HX-Request": "true"} if case.mode == "htmx" else {}
    return client.post(path, body, headers=headers)


def test_matrix_covers_every_ehr_route_and_action() -> None:
    live = mounts()
    mounted = Counter(mount.view for mount in live)
    # Each matrix view has exactly one mount: another one, named or not,
    # under any namespace, is a route the matrix does not drive.
    assert {
        route: mounted[view] for route, view in ROUTE_VIEWS.items()
    } == dict.fromkeys(ROUTE_VIEWS, 1)
    # Every view in or referencing the EHR app is classified, or this fails.
    assert {mount.view for mount in live if reaches_ehr(mount.view)} == {
        *ROUTE_VIEWS.values(),
        *OUTSIDE_EHR,
    }
    # Every action the UI posts to an EHR workspace is in its dispatch table.
    ui = ui_actions()
    assert set(ui) <= set(FORM_ACTIONS)
    for route in TABLED:
        assert ui.get(route, set()) <= FORM_ACTIONS[route], route
    assert {route for _actor, route, _action in PERMITTED} <= set(ROUTE_VIEWS)


def test_every_refused_ehr_write_leaves_no_committed_row(
    rbac_graph: RbacGraph,
    superuser_database_url: str,
    synthetic_room: None,
) -> None:
    built = world(rbac_graph)
    planned = list(cases(built, ui_actions()))
    shared = closure(SHARED_TEMPLATES)
    pages = {
        route: closure(ROUTE_TEMPLATES.get(route, ())) | shared for route in ROUTE_VIEWS
    }
    failures: list[str] = []
    refused = 0
    for actor in ("reader", "staff", "assignee", STALE):
        with signed_in(built.users[actor]) as client:
            # The first request after sign-in stores the default active clinic,
            # even for an unknown-clinic refusal; it belongs to the sign-in.
            client.get(f"/ehr/clinics/{rbac_graph.clinic_a}/encounter/")
            for case in (c for c in planned if c.actor == actor):
                if actor == STALE:
                    # Each case starts from the stale state, before the snapshot.
                    session = client.session
                    session[STEP_UP_SESSION_KEY] = 0
                    session.save()
                before, tail = snapshot(superuser_database_url)
                response = send(client, built, case, rbac_graph.clinic_a)
                # A page outside the route's listed templates carries actions
                # the inventory never read.
                unlisted = {str(t.name) for t in response.templates} - pages[case.route]
                if unlisted:
                    failures.append(f"{case.label}: templates {sorted(unlisted)}")
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
                    not is_refusal(case, response)
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

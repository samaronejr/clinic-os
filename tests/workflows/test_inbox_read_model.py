"""Plan item 32: the Clinical Inbox read model, its feeds and clinician Today.

Everything runs as ``clinic_app`` against the real task, result and clinical
policies. Feeds are exercised through their registered queries, never stubs;
the only replaced seam is the Redis send behind the commit-bound hint.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, cast
from uuid import UUID, uuid4

import pytest
from apps.ehr.models import ClinicalDocumentVersion, Encounter
from apps.identity.current_context import CurrentActorError
from apps.identity.models import CareTeamMembership, UserClinicRole
from apps.orders.inbox import apply_bulk_acknowledgment, preview_bulk_acknowledgment
from apps.orders.models import (
    ResultAcknowledgment,
    ResultMatch,
    ResultRelease,
    ResultWorkItem,
)
from apps.orders.services import (
    IdentityEvidence,
    ObservationInput,
    OrdersConflictError,
    acknowledge_result,
    ingest_result,
    place_order,
)
from apps.realtime import transport
from apps.workflows import inbox as inbox_module
from apps.workflows import today as today_module
from apps.workflows.inbox import (
    MAX_KNOWN_KEYS,
    InboxInputError,
    query_inbox,
    register_inbox_feed,
    registered_kinds,
    validated_filters,
    validated_keys,
)
from apps.workflows.inbox_views import MAX_SELECTED
from apps.workflows.services import (
    TaskOwner,
    TaskSpec,
    assign_task,
    cancel_task,
    create_task,
)
from apps.workflows.task_feed import task_items
from apps.workflows.today import view_today
from django.db import transaction
from django.test import Client
from django.utils import timezone

from ehr.test_autosave import as_actor
from identity.permission_support import owner_context, permission_actor
from orders.support import World, ordered, submission
from otp_test_support import OTP_RAW_CREDENTIAL, create_receptionist, runtime_role
from renewal.test_encounters import (
    physician_client,
    seed_appointment_setup_for_existing,
)
from scheduling.appointment_service_support import create_synthetic_appointment

if TYPE_CHECKING:
    from apps.orders.values import IngestReceipt
    from apps.workflows.inbox import InboxFilters, InboxPage
    from django.test.client import _MonkeyPatchedWSGIResponse
    from pytest_django.fixtures import SettingsWrapper

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)

API = "/api/ui/v1/inbox/query/"
EMPTY_KEYS = {
    "items",
    "counts",
    "total",
    "overdue",
    "critical",
    "removed",
    "truncated",
    "counts_complete",
}


def promote(graph: RbacGraph) -> None:
    with owner_context(graph.organization_a):
        UserClinicRole.objects.filter(organization_id=graph.organization_a).update(
            workflow_bundle_version=2
        )


def page(
    graph: RbacGraph,
    actor: UUID,
    *,
    filters: InboxFilters | None = None,
    known: tuple[str, ...] = (),
) -> InboxPage:
    with as_actor(graph, actor):
        return query_inbox(clinic_id=graph.clinic_a, filters=filters, known=known)


def result(
    graph: RbacGraph,
    world: World,
    *,
    flag: str = "normal",
    observed: bool = True,
    uploader: UUID | None = None,
) -> IngestReceipt:
    observations = (
        (
            ObservationInput(
                code="cbc",
                value="7,10",
                unit="g/dL",
                reference_interval="6,0-8,0",
                abnormal_flag=flag,
            ),
        )
        if observed
        else ()
    )
    with as_actor(graph, uploader or graph.physician):
        return ingest_result(
            clinic_id=graph.clinic_a,
            submission=replace(
                submission(world), event_id=uuid4(), observations=observations
            ),
        )


def own_task(
    graph: RbacGraph,
    actor: UUID,
    *,
    owner: TaskOwner,
    priority: str = "high",
    due_in: timedelta = timedelta(days=1),
) -> UUID:
    with as_actor(graph, actor):
        task = create_task(
            clinic_id=graph.clinic_a,
            spec=TaskSpec(
                kind="follow_up",
                subject_ref={"kind": "clinic", "id": str(graph.clinic_a)},
                due_at=timezone.now() + due_in,
                priority=priority,
            ),
            idempotency_key=uuid4(),
        )
    manager = actor if owner.user_id == actor else graph.clinic_admin
    with as_actor(graph, manager):
        assign_task(
            clinic_id=graph.clinic_a,
            task_id=task.pk,
            owner=owner,
            expected_revision=task.revision,
        )
    return task.pk


def keys(view: InboxPage, kind: str) -> set[str]:
    return {item.key for item in view.items if item.kind == kind}


def test_physician_queue_is_typed_triaged_and_never_duplicates_review_tasks(
    rbac_graph: RbacGraph,
) -> None:
    world = ordered(rbac_graph)
    promote(rbac_graph)
    normal = result(rbac_graph, world)
    critical = result(rbac_graph, world, flag="critical")
    high = result(rbac_graph, world, flag="high")
    task = own_task(
        rbac_graph, rbac_graph.physician, owner=TaskOwner(user_id=rbac_graph.physician)
    )

    view = page(rbac_graph, rbac_graph.physician)

    # Then: the critical source leads and each record appears exactly once.
    assert view.items[0].key == f"result:{critical.version_id}"
    assert view.items[0].critical
    assert view.items[0].priority == "urgent"
    assert keys(view, "result") == {
        f"result:{receipt.version_id}" for receipt in (normal, critical, high)
    }
    assert keys(view, "task") == {f"task:{task}"}
    with owner_context(rbac_graph.organization_a):
        review_tasks = set(
            ResultWorkItem.objects.filter(kind="review").values_list(
                "task_id", flat=True
            )
        )
        draft = ClinicalDocumentVersion.objects.select_related("document").get(
            state="draft"
        )
    assert len(review_tasks) == 3
    assert not {f"task:{value}" for value in review_tasks} & keys(view, "task")
    assert keys(view, "draft") == {f"draft:{draft.pk}"}
    [draft_item] = (item for item in view.items if item.kind == "draft")
    assert draft_item.target_id == draft.document.encounter_id
    assert dict(view.counts) == {"result": 3, "draft": 1, "task": 1}
    assert view.critical == 1
    eligible = {item.key for item in view.items if item.bulk_eligible}
    assert eligible == {f"result:{normal.version_id}"}
    [normal_item] = (item for item in view.items if item.bulk_eligible)
    assert normal_item.owner == "me"
    assert [(row.value, row.unit, row.flag) for row in normal_item.preview] == [
        ("7,10", "g/dL", "normal")
    ]
    assert registered_kinds() == ("result", "draft", "task")


def test_nurse_sees_only_scope_limited_items(rbac_graph: RbacGraph) -> None:
    world = ordered(rbac_graph)
    result(rbac_graph, world)
    nurse, _enrollment = permission_actor(rbac_graph, "nurse")
    promote(rbac_graph)
    nurse_task = own_task(
        rbac_graph, rbac_graph.clinic_admin, owner=TaskOwner(role="nurse")
    )
    physician_task = own_task(
        rbac_graph, rbac_graph.physician, owner=TaskOwner(user_id=rbac_graph.physician)
    )

    nurse_view = page(rbac_graph, nurse)
    physician_view = page(rbac_graph, rbac_graph.physician)

    # Then: the nurse's queue is her role's work only; no results or notes.
    assert [item.key for item in nurse_view.items] == [f"task:{nurse_task}"]
    assert nurse_view.items[0].owner == "role"
    assert nurse_view.items[0].patient_label == ""
    assert dict(nurse_view.counts) == {"result": 0, "draft": 0, "task": 1}
    assert f"task:{nurse_task}" not in keys(physician_view, "task")
    assert f"task:{physician_task}" in keys(physician_view, "task")
    assert physician_view.counts["result"] == 1


def test_manager_visibility_is_not_an_inbox_and_receptionist_page_is_empty(
    rbac_graph: RbacGraph,
) -> None:
    world = ordered(rbac_graph)
    receipt = result(rbac_graph, world)
    promote(rbac_graph)
    receptionist = create_receptionist(rbac_graph)
    promote(rbac_graph)

    # Given: routing staff can read result rows through the identity queue.
    with as_actor(rbac_graph, receptionist.pk):
        assert ResultMatch.objects.filter(version_id=receipt.version_id).exists()

    # Then: a clinic-wide reader without a clinical role gets the gate's refusal.
    for actor in (receptionist.pk, rbac_graph.clinic_admin):
        with as_actor(rbac_graph, actor), pytest.raises(CurrentActorError):
            query_inbox(clinic_id=rbac_graph.clinic_a)


def test_revoked_item_is_reported_removed_on_refresh(rbac_graph: RbacGraph) -> None:
    ordered(rbac_graph)
    promote(rbac_graph)
    task = own_task(
        rbac_graph, rbac_graph.physician, owner=TaskOwner(user_id=rbac_graph.physician)
    )
    known = (f"task:{task}",)
    assert page(rbac_graph, rbac_graph.physician, known=known).removed == ()

    # When: a manager cancels the open item while the clinician still shows it.
    with as_actor(rbac_graph, rbac_graph.clinic_admin):
        cancel_task(clinic_id=rbac_graph.clinic_a, task_id=task, expected_revision=2)

    refreshed = page(rbac_graph, rbac_graph.physician, known=known)
    assert refreshed.removed == known
    assert f"task:{task}" not in keys(refreshed, "task")


@pytest.mark.parametrize(
    "value",
    [
        ["task:not-a-uuid"],
        [f"letter:{uuid4()}"],
        [f"TASK:{uuid4()}"],
        [f"task:{uuid4()}"] * 1 + [f"result:{uuid4()} "],
        [f"task:{uuid4()}" for _ in range(MAX_KNOWN_KEYS + 1)],
    ],
)
def test_malformed_known_keys_are_refused(value: list[str]) -> None:
    with pytest.raises(InboxInputError):
        validated_keys(value)


@pytest.mark.parametrize(
    ("kinds", "owner", "due"),
    [(["unknown"], "all", "all"), ([], "everyone", "all"), ([], "all", "yesterday")],
)
def test_filters_outside_the_closed_vocabulary_are_refused(
    kinds: list[str], owner: str, due: str
) -> None:
    with pytest.raises(InboxInputError):
        validated_filters(kinds=kinds, owner=owner, due=due)


def test_feed_registry_refuses_conflicting_and_malformed_kinds() -> None:
    def other(scope: object) -> tuple[()]:
        return ()

    with pytest.raises(ValueError, match="already registered"):
        register_inbox_feed("task", other, label="Other", position=1)
    with pytest.raises(ValueError, match="identifier"):
        register_inbox_feed("Bad Kind", other, label="Other", position=1)
    register_inbox_feed("task", task_items, label="Tasks", position=30)
    assert registered_kinds() == ("result", "draft", "task")


def test_filters_narrow_items_but_counts_cover_the_whole_queue(
    rbac_graph: RbacGraph,
) -> None:
    world = ordered(rbac_graph)
    promote(rbac_graph)
    result(rbac_graph, world)
    task = own_task(
        rbac_graph, rbac_graph.physician, owner=TaskOwner(user_id=rbac_graph.physician)
    )

    tasks_only = page(
        rbac_graph, rbac_graph.physician, filters=validated_filters(kinds=["task"])
    )
    overdue = page(
        rbac_graph, rbac_graph.physician, filters=validated_filters(due="overdue")
    )

    assert [item.key for item in tasks_only.items] == [f"task:{task}"]
    assert dict(tasks_only.counts) == {"result": 1, "draft": 1, "task": 1}
    # Then: the task is due tomorrow; results are due on receipt, like review tasks.
    assert f"task:{task}" not in {item.key for item in overdue.items}
    assert {item.kind for item in overdue.items} == {"result"}


def test_overflow_keeps_urgent_work_and_marks_counts_incomplete(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = ordered(rbac_graph)
    promote(rbac_graph)
    older = result(rbac_graph, world)
    result(rbac_graph, world)
    critical = result(rbac_graph, world, flag="critical")
    mine = TaskOwner(user_id=rbac_graph.physician)
    soon, _later = (
        own_task(
            rbac_graph,
            rbac_graph.physician,
            owner=mine,
            priority="low",
            due_in=timedelta(hours=hours),
        )
        for hours in (1, 2)
    )
    urgent = own_task(
        rbac_graph,
        rbac_graph.physician,
        owner=mine,
        priority="urgent",
        due_in=timedelta(days=3),
    )

    # When: each feed holds more items than one page.
    monkeypatch.setattr(inbox_module, "MAX_ITEMS", 2)
    results = page(
        rbac_graph, rbac_graph.physician, filters=validated_filters(kinds=["result"])
    )
    tasks = page(
        rbac_graph, rbac_graph.physician, filters=validated_filters(kinds=["task"])
    )

    # Then: the newest critical result and the urgent task are never cut, and
    # the page says its counts are lower bounds instead of claiming completeness.
    assert [item.key for item in results.items] == [
        f"result:{critical.version_id}",
        f"result:{older.version_id}",
    ]
    assert [item.key for item in tasks.items] == [f"task:{urgent}", f"task:{soon}"]
    for view in (results, tasks):
        assert view.truncated
        assert view.overflowed == {"result", "task"}
        assert dict(view.counts) == {"result": 2, "draft": 1, "task": 2}


def test_bound_applies_after_acknowledgment_authority(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = ordered(rbac_graph)
    colleague, enrollment = permission_actor(rbac_graph, "physician")
    with as_actor(rbac_graph, colleague):
        order = place_order(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=enrollment,
            exam_codes=("cbc",),
            command_id=uuid4(),
        )
    theirs = World(
        order,
        IdentityEvidence(birth_date=date(1990, 1, 1), name="Sintetico Permission"),
        enrollment,
    )
    older = [result(rbac_graph, theirs, uploader=colleague) for _ in range(3)]
    own = result(rbac_graph, world)
    # Given: the physician also routes results, so the colleague's patient's
    # older rows pass result RLS without granting acknowledgment authority.
    with owner_context(rbac_graph.organization_a):
        UserClinicRole.objects.create(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_a,
            user_id=rbac_graph.physician,
            role=UserClinicRole.Role.RECEPTIONIST,
        )
    with as_actor(rbac_graph, rbac_graph.physician):
        visible = set(ResultMatch.objects.values_list("version_id", flat=True))
    assert {receipt.version_id for receipt in (*older, own)} <= visible

    # When: those rows alone would fill the feed's bound.
    monkeypatch.setattr(inbox_module, "MAX_ITEMS", 2)
    view = page(
        rbac_graph, rbac_graph.physician, filters=validated_filters(kinds=["result"])
    )

    # Then: only the acknowledgeable result is shown, and nothing overflowed.
    assert [item.key for item in view.items] == [f"result:{own.version_id}"]
    assert view.counts["result"] == 1
    assert not view.truncated
    assert view.overflowed == frozenset()


def test_owner_and_due_filters_reach_work_beyond_an_overflowing_feed(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    ordered(rbac_graph)
    promote(rbac_graph)
    for days in (2, 3, 4):
        own_task(
            rbac_graph,
            rbac_graph.clinic_admin,
            owner=TaskOwner(role="physician"),
            priority="urgent",
            due_in=timedelta(days=days),
        )
    overdue = own_task(
        rbac_graph,
        rbac_graph.physician,
        owner=TaskOwner(user_id=rbac_graph.physician),
        priority="normal",
        due_in=timedelta(hours=-2),
    )

    # Given: urgent role-owned tasks fill the bound before the actor's own
    # overdue task, so the unfiltered page cannot show it.
    monkeypatch.setattr(inbox_module, "MAX_ITEMS", 2)
    tasks = page(
        rbac_graph, rbac_graph.physician, filters=validated_filters(kinds=["task"])
    )
    assert f"task:{overdue}" not in keys(tasks, "task")
    assert tasks.truncated
    assert not tasks.counts_complete

    # When/Then: narrowing by owner or due reaches it and the narrowed list is
    # complete. Every summary counts the recovered task, so none contradicts a
    # listed item, and all are flagged as lower bounds of the overflowing queue.
    for filters in (validated_filters(owner="mine"), validated_filters(due="overdue")):
        view = page(rbac_graph, rbac_graph.physician, filters=filters)
        assert keys(view, "task") == {f"task:{overdue}"}
        assert not view.truncated
        assert view.overflowed == {"task"}
        assert not view.counts_complete
        assert view.counts["task"] == 3
        assert view.overdue == 1

    with physician_client(rbac_graph) as clinician:
        body = _query(clinician, clinic_id=str(rbac_graph.clinic_a), due="overdue")
        board = clinician.post(
            _inbox_url(rbac_graph), {"action": "refresh", "due": "overdue"}
        )
        today = clinician.get(f"/clinics/{rbac_graph.clinic_a}/today/")
    assert body["overdue"] == 1
    assert body["counts_complete"] is False
    assert body["truncated"] is False
    assert [item["key"] for item in cast("list[dict[str, str]]", body["items"])] == [
        f"task:{overdue}"
    ]
    for response in (board, today):
        assert response.status_code == 200
        assert _summary(response, "overdue").endswith("+")
        assert _summary(response, "critical").endswith("+")
    assert _summary(board, "overdue") == "1+"


def test_bulk_acknowledgment_needs_previewed_normal_noncritical_results(
    rbac_graph: RbacGraph,
) -> None:
    world = ordered(rbac_graph)
    first = result(rbac_graph, world)
    second = result(rbac_graph, world)
    high = result(rbac_graph, world, flag="high")
    critical = result(rbac_graph, world, flag="critical")
    unflagged = result(rbac_graph, world, flag="")
    missing = result(rbac_graph, world, observed=False)
    view = page(rbac_graph, rbac_graph.physician)

    with as_actor(rbac_graph, rbac_graph.physician):
        for refused in (high, critical, unflagged, missing):
            with pytest.raises(OrdersConflictError, match="bulk_ineligible"):
                preview_bulk_acknowledgment(
                    clinic_id=rbac_graph.clinic_a,
                    items=view.items,
                    version_ids=(first.version_id, refused.version_id),
                )
        with pytest.raises(InboxInputError):
            preview_bulk_acknowledgment(
                clinic_id=rbac_graph.clinic_a, items=view.items, version_ids=()
            )
        preview = preview_bulk_acknowledgment(
            clinic_id=rbac_graph.clinic_a,
            items=view.items,
            version_ids=(first.version_id, second.version_id),
        )
        assert [item.record_id for item in preview.items] == [
            first.version_id,
            second.version_id,
        ]
        receipts = apply_bulk_acknowledgment(
            clinic_id=rbac_graph.clinic_a, token=preview.token
        )
        assert {row.disposition for row in receipts} == {"no_action"}
        assert {row.version_id for row in receipts} == {
            first.version_id,
            second.version_id,
        }
        # Then: a replayed confirmation finds nothing eligible and writes nothing.
        with pytest.raises(OrdersConflictError, match="bulk_preview_stale"):
            apply_bulk_acknowledgment(
                clinic_id=rbac_graph.clinic_a, token=preview.token
            )
    with owner_context(rbac_graph.organization_a):
        assert ResultAcknowledgment.objects.count() == 2
        assert not ResultRelease.objects.exists()
    remaining = page(rbac_graph, rbac_graph.physician)
    assert keys(remaining, "result") == {
        f"result:{receipt.version_id}"
        for receipt in (high, critical, unflagged, missing)
    }


def test_stale_or_foreign_bulk_confirmation_acknowledges_nothing(
    rbac_graph: RbacGraph,
) -> None:
    world = ordered(rbac_graph)
    first = result(rbac_graph, world)
    second = result(rbac_graph, world)
    other, _enrollment = permission_actor(rbac_graph, "physician")
    view = page(rbac_graph, rbac_graph.physician)
    with as_actor(rbac_graph, rbac_graph.physician):
        preview = preview_bulk_acknowledgment(
            clinic_id=rbac_graph.clinic_a,
            items=view.items,
            version_ids=(first.version_id, second.version_id),
        )

    for token in (preview.token + "x", preview.token[::-1], ""):
        with (
            as_actor(rbac_graph, rbac_graph.physician),
            pytest.raises(OrdersConflictError, match="bulk_preview_stale"),
        ):
            apply_bulk_acknowledgment(clinic_id=rbac_graph.clinic_a, token=token)
    with (
        as_actor(rbac_graph, other),
        pytest.raises(OrdersConflictError, match="bulk_preview_stale"),
    ):
        apply_bulk_acknowledgment(clinic_id=rbac_graph.clinic_a, token=preview.token)
    with (
        as_actor(rbac_graph, rbac_graph.physician),
        pytest.raises(OrdersConflictError, match="bulk_preview_stale"),
    ):
        apply_bulk_acknowledgment(clinic_id=rbac_graph.clinic_b, token=preview.token)

    # When: the previewed result confirmed last is decided individually first,
    # so the confirmation acknowledges the other one before finding it stale.
    last = max(first, second, key=lambda receipt: str(receipt.version_id))
    with as_actor(rbac_graph, rbac_graph.physician):
        acknowledge_result(
            clinic_id=rbac_graph.clinic_a,
            version_id=last.version_id,
            disposition="follow_up",
        )
        with pytest.raises(OrdersConflictError, match="bulk_preview_stale"):
            apply_bulk_acknowledgment(
                clinic_id=rbac_graph.clinic_a, token=preview.token
            )
    # Then: that earlier acknowledgment rolled back with the stale batch.
    with owner_context(rbac_graph.organization_a):
        assert list(
            ResultAcknowledgment.objects.values_list("version_id", "disposition")
        ) == [(last.version_id, "follow_up")]


def _json(response: _MonkeyPatchedWSGIResponse) -> dict[str, object]:
    assert response.status_code == 200
    assert response["Content-Type"] == "application/json"
    assert "no-store" in response.headers["Cache-Control"]
    body = json.loads(response.content)
    assert isinstance(body, dict)
    return body


def _query(client: Client, **body: object) -> dict[str, object]:
    return _json(
        client.post(API, data=json.dumps(body), content_type="application/json")
    )


def test_receptionist_api_query_is_the_same_empty_shape(rbac_graph: RbacGraph) -> None:
    world = ordered(rbac_graph)
    receipt = result(rbac_graph, world)
    receptionist = create_receptionist(rbac_graph)
    key = f"result:{receipt.version_id}"
    with runtime_role():
        reception = Client()
        assert reception.login(
            username=receptionist.username, password=OTP_RAW_CREDENTIAL
        )
        answers = [
            _query(reception, clinic_id=str(rbac_graph.clinic_a), known=[key]),
            _query(reception, clinic_id=str(uuid4()), known=[key]),
            _query(reception, clinic_id=str(rbac_graph.clinic_c), known=[key]),
        ]
    with physician_client(rbac_graph) as clinician:
        nothing_to_do = _query(
            clinician, clinic_id=str(rbac_graph.clinic_b), known=[key]
        )
        working = _query(clinician, clinic_id=str(rbac_graph.clinic_a), known=[key])

    empty = {
        "items": [],
        "counts": {"result": 0, "draft": 0, "task": 0},
        "total": 0,
        "overdue": 0,
        "critical": 0,
        "removed": [key],
        "truncated": False,
        "counts_complete": True,
    }
    assert answers == [empty, empty, empty]
    assert nothing_to_do == empty
    assert set(working) == EMPTY_KEYS
    assert working["removed"] == []
    items = working["items"]
    assert isinstance(items, list)
    assert [item["key"] for item in items if item["kind"] == "result"] == [key]


def test_api_query_bounds_its_body(rbac_graph: RbacGraph) -> None:
    ordered(rbac_graph)
    with physician_client(rbac_graph) as clinician:
        for body in (
            {"kinds": ["unknown"]},
            {"owner": "all-staff"},
            {"known": ["result:not-a-uuid"]},
            {"known": [f"task:{uuid4()}"] * (MAX_KNOWN_KEYS + 1)},
            {"clinic_id": "not-a-uuid"},
        ):
            response = clinician.post(
                API, data=json.dumps(body), content_type="application/json"
            )
            assert response.status_code == 400
            assert json.loads(response.content) == {
                "code": "invalid_input",
                "message_key": "api.error.invalid_input",
            }


def _inbox_url(graph: RbacGraph) -> str:
    return f"/clinics/{graph.clinic_a}/inbox/"


def _summary(response: _MonkeyPatchedWSGIResponse, kind: str) -> str:
    match = re.search(
        rf'data-inbox-count="{kind}"><dt>[^<]*</dt><dd>([^<]*)</dd>',
        response.content.decode(),
    )
    assert match is not None, kind
    return match[1]


def test_inbox_page_acknowledges_one_result_and_never_bulk_releases(
    rbac_graph: RbacGraph,
) -> None:
    world = ordered(rbac_graph)
    first = result(rbac_graph, world)
    second = result(rbac_graph, world)
    url = _inbox_url(rbac_graph)
    with physician_client(rbac_graph) as client:
        shown = client.get(url)
        assert shown.status_code == 200
        assert "no-store" in shown.headers["Cache-Control"]
        assert f'data-inbox-key="result:{first.version_id}"'.encode() in shown.content
        assert str(first.version_id).encode() not in url.encode()

        acknowledged = client.post(
            url,
            {
                "action": "acknowledge",
                "version_id": str(first.version_id),
                "disposition": "no_action",
            },
        )
        assert acknowledged.status_code == 200
        assert b"data-inbox-outcome" in acknowledged.content
        assert f"result:{first.version_id}".encode() not in acknowledged.content

        for action in ("bulk_release", "release"):
            refused = client.post(
                url,
                {
                    "action": action,
                    "version_id": str(second.version_id),
                    "audience": "patient",
                },
            )
            assert refused.status_code == 403
            assert not refused.cookies
        missing_disposition = client.post(
            url, {"action": "acknowledge", "version_id": str(second.version_id)}
        )
        assert missing_disposition.status_code == 400
        malformed = client.post(
            url,
            {
                "action": "acknowledge",
                "version_id": "not-a-uuid",
                "disposition": "no_action",
            },
        )
        assert malformed.status_code == 403
        for body in ({}, {"action": "refresh", "known": ["task:not-a-uuid"]}):
            assert client.post(url, body).status_code == 403
        foreign = client.post(
            url,
            {
                "action": "acknowledge",
                "version_id": str(uuid4()),
                "disposition": "no_action",
            },
        )
        assert foreign.status_code == 403
    with owner_context(rbac_graph.organization_a):
        assert list(
            ResultAcknowledgment.objects.values_list("version_id", "disposition")
        ) == [(first.version_id, "no_action")]
        assert not ResultRelease.objects.exists()


def test_inbox_page_bulk_flow_previews_each_item_then_acknowledges(
    rbac_graph: RbacGraph,
) -> None:
    world = ordered(rbac_graph)
    first = result(rbac_graph, world)
    second = result(rbac_graph, world)
    url = _inbox_url(rbac_graph)
    with physician_client(rbac_graph) as client:
        oversized = client.post(
            url,
            {
                "action": "bulk_preview",
                "version_id": [str(uuid4()) for _ in range(MAX_SELECTED + 1)],
            },
        )
        assert oversized.status_code == 403
        preview = client.post(
            url,
            {
                "action": "bulk_preview",
                "version_id": [str(first.version_id), str(second.version_id)],
            },
        )
        assert preview.status_code == 200
        assert b'data-bulk-preview="2"' in preview.content
        for receipt in (first, second):
            assert (
                f'data-preview-key="result:{receipt.version_id}"'.encode()
                in preview.content
            )
        with owner_context(rbac_graph.organization_a):
            assert not ResultAcknowledgment.objects.exists()
        # When: the clinician cancels the preview, then resumes with a new one.
        cancelled = client.post(
            url, {"action": "refresh"}, headers={"HX-Request": "true"}
        )
        assert cancelled.status_code == 200
        assert b"data-bulk-preview" not in cancelled.content
        assert b"data-inbox-outcome" not in cancelled.content
        with owner_context(rbac_graph.organization_a):
            assert not ResultAcknowledgment.objects.exists()
        preview = client.post(
            url,
            {
                "action": "bulk_preview",
                "version_id": [str(first.version_id), str(second.version_id)],
            },
        )
        assert b'data-bulk-preview="2"' in preview.content
        token = preview.content.split(b'name="preview_token" value="')[1].split(b'"')[0]
        confirmed = client.post(
            url, {"action": "bulk_confirm", "preview_token": token.decode()}
        )
        assert confirmed.status_code == 200
        replay = client.post(
            url, {"action": "bulk_confirm", "preview_token": token.decode()}
        )
        assert replay.status_code == 409
    with owner_context(rbac_graph.organization_a):
        assert ResultAcknowledgment.objects.count() == 2


def test_bulk_preview_uses_the_filtered_selection_beyond_an_overflowing_feed(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = ordered(rbac_graph)
    colleague, _enrollment = permission_actor(rbac_graph, "physician")
    with owner_context(rbac_graph.organization_a):
        CareTeamMembership.objects.create(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_a,
            patient_enrollment_id=world.enrollment,
            user_id=colleague,
            role="physician",
            valid_from=timezone.now() - timedelta(days=1),
            valid_to=timezone.now() + timedelta(days=1),
        )
    with as_actor(rbac_graph, colleague):
        order = place_order(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=world.enrollment,
            exam_codes=("cbc",),
            command_id=uuid4(),
        )
    theirs = World(order, world.identity, world.enrollment)
    for _ in range(2):
        result(rbac_graph, theirs, uploader=colleague)
    mine = result(rbac_graph, world)
    key = f"result:{mine.version_id}"
    url = _inbox_url(rbac_graph)

    # Given: the colleague's older results, which the physician may acknowledge
    # through care-team scope, fill the bound ahead of the physician's own.
    monkeypatch.setattr(inbox_module, "MAX_ITEMS", 2)
    with physician_client(rbac_graph) as client:
        everything = client.post(url, {"action": "refresh"})
        assert f'data-inbox-key="{key}"'.encode() not in everything.content

        # When: the owner filter recovers it with its bulk selector, and the
        # clinician previews and confirms from that same filtered selection.
        board = client.post(url, {"action": "refresh", "owner": "mine"})
        assert f'data-inbox-key="{key}"'.encode() in board.content
        assert f'value="{mine.version_id}" form="inbox-bulk"'.encode() in board.content
        preview = client.post(
            url,
            {
                "action": "bulk_preview",
                "owner": "mine",
                "version_id": [str(mine.version_id)],
            },
        )
        assert preview.status_code == 200
        assert b'data-bulk-preview="1"' in preview.content
        assert f'data-preview-key="{key}"'.encode() in preview.content
        token = preview.content.split(b'name="preview_token" value="')[1].split(b'"')[0]
        confirm = {
            "action": "bulk_confirm",
            "owner": "mine",
            "preview_token": token.decode(),
        }
        assert client.post(url, confirm).status_code == 200
        # Then: a replay of the same confirmation is stale and writes nothing.
        assert client.post(url, confirm).status_code == 409
    with owner_context(rbac_graph.organization_a):
        assert list(
            ResultAcknowledgment.objects.values_list("version_id", "disposition")
        ) == [(mine.version_id, "no_action")]
        assert not ResultRelease.objects.exists()


def test_refresh_reports_removed_items_in_the_page(rbac_graph: RbacGraph) -> None:
    ordered(rbac_graph)
    promote(rbac_graph)
    task = own_task(
        rbac_graph, rbac_graph.physician, owner=TaskOwner(user_id=rbac_graph.physician)
    )
    with as_actor(rbac_graph, rbac_graph.clinic_admin):
        cancel_task(clinic_id=rbac_graph.clinic_a, task_id=task, expected_revision=2)
    with physician_client(rbac_graph) as client:
        response = client.post(
            _inbox_url(rbac_graph),
            {"action": "refresh", "known": [f"task:{task}"]},
            headers={"HX-Request": "true"},
        )
    assert response.status_code == 200
    assert b'id="inbox-board"' in response.content
    assert b'data-inbox-removed="1"' in response.content


def test_receptionist_is_refused_the_inbox_and_today_pages(
    rbac_graph: RbacGraph,
) -> None:
    receptionist = create_receptionist(rbac_graph)
    with runtime_role():
        client = Client()
        assert client.login(username=receptionist.username, password=OTP_RAW_CREDENTIAL)
        for path in (
            _inbox_url(rbac_graph),
            f"/clinics/{rbac_graph.clinic_a}/today/",
        ):
            response = client.get(path)
            assert response.status_code == 403
            assert not response.cookies


def test_today_shows_own_day_next_patient_brief_and_counts(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = ordered(rbac_graph)
    result(rbac_graph, world)
    with owner_context(rbac_graph.organization_a):
        encounter = Encounter.objects.get()
        arrived = encounter.appointment
    assert arrived is not None
    setup = seed_appointment_setup_for_existing(rbac_graph, arrived)
    with runtime_role(), as_actor(rbac_graph, rbac_graph.shared_user):
        later = create_synthetic_appointment(
            setup, start_local="2035-06-02T10:00", end_local="2035-06-02T11:00"
        )
    monkeypatch.setattr(
        today_module, "utc_now", lambda: datetime(2035, 6, 2, 11, 30, tzinfo=UTC)
    )

    with as_actor(rbac_graph, rbac_graph.physician):
        screen = view_today(clinic_id=rbac_graph.clinic_a)
    assert [row.appointment_id for row in screen.appointments] == [
        arrived.pk,
        later.pk,
    ]
    assert screen.next_patient is not None
    assert screen.next_patient.appointment_id == arrived.pk
    assert screen.next_patient.can_start
    assert screen.next_patient.encounter_open
    assert [row.appointment_id for row in screen.arrivals] == [arrived.pk]
    assert screen.upcoming == 1
    assert screen.brief == "unavailable"
    assert screen.inbox is not None
    assert dict(screen.inbox.counts) == {"result": 1, "draft": 1, "task": 0}
    with (
        as_actor(rbac_graph, rbac_graph.shared_user),
        pytest.raises(CurrentActorError),
    ):
        view_today(clinic_id=rbac_graph.clinic_a)

    with physician_client(rbac_graph) as client:
        response = client.get(f"/clinics/{rbac_graph.clinic_a}/today/")
    assert response.status_code == 200
    assert "no-store" in response.headers["Cache-Control"]
    assert b'data-today-next="arrived"' in response.content
    assert b'data-today-brief="unavailable"' in response.content
    assert f'name="appointment_id" value="{arrived.pk}"'.encode() in response.content
    assert f"/{arrived.pk}".encode() not in response.content


def test_inbox_hint_is_published_only_after_commit(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    settings: SettingsWrapper,
) -> None:
    ordered(rbac_graph)
    promote(rbac_graph)
    settings.REALTIME_ENABLED = True
    sent: list[tuple[str, str, int]] = []

    def record(*, topic: str, kind: str, version: int) -> None:
        sent.append((topic, kind, version))

    monkeypatch.setattr(transport, "publish", record)
    topic = f"clinic:{rbac_graph.clinic_a}:inbox"

    def rolled_back_task() -> None:
        with as_actor(rbac_graph, rbac_graph.physician), transaction.atomic():
            create_task(
                clinic_id=rbac_graph.clinic_a,
                spec=TaskSpec(
                    kind="checklist",
                    subject_ref={"kind": "clinic", "id": str(rbac_graph.clinic_a)},
                    due_at=timezone.now() + timedelta(days=1),
                ),
                idempotency_key=uuid4(),
            )
            message = "rolled back"
            raise RuntimeError(message)

    # When: a task change rolls back, no hint leaves the transaction.
    with pytest.raises(RuntimeError, match="rolled back"):
        rolled_back_task()
    assert (topic, "inbox", 1) not in sent

    own_task(
        rbac_graph, rbac_graph.physician, owner=TaskOwner(user_id=rbac_graph.physician)
    )
    assert (topic, "inbox", 1) in sent

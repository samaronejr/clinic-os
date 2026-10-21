"""Native request lifecycle, exact-byte download and the separate lab boundary."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.orders import callback_views
from apps.orders.models import ResultVersion
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory

from auth.stepup_test_support import logged_in_client, seed_freshness
from ehr.test_autosave import as_actor
from orders.support import SOURCE, laboratory, ordered
from otp_test_support import create_totp_device
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from pytest_django.fixtures import SettingsWrapper

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_native_result_lifecycle_downloads_only_the_exact_authorized_source(
    rbac_graph: RbacGraph,
    settings: SettingsWrapper,
) -> None:
    graph = rbac_graph
    settings.ALLOWED_HOSTS = ["testserver"]
    world = ordered(graph)
    client = logged_in_client(graph.physician)
    seed_freshness(client, create_totp_device(graph.physician, confirmed=True))
    session = client.session
    session["active_org_id"] = str(graph.organization_a)
    session.save()
    url = f"/clinics/{graph.clinic_a}/results/"
    with runtime_role():
        assert client.get(url).status_code == 200
        assert (
            client.post(
                url,
                {
                    "action": "place",
                    "enrollment_id": str(world.enrollment),
                    "exam_codes": ["cbc"],
                    "command_id": str(uuid4()),
                },
            ).status_code
            == 200
        )
    event = uuid4()
    with runtime_role():
        response = client.post(
            url,
            {
                "action": "upload",
                "order_id": str(world.order.pk),
                "event_id": str(event),
                "name": world.identity.name,
                "birth_date": str(world.identity.birth_date),
                "source": SimpleUploadedFile(
                    "sintetico.pdf", SOURCE, content_type="application/pdf"
                ),
                "value": "7,10",
                "observation_code": "cbc",
                "unit": "source-unit",
                "reference_interval": "source-interval",
                "abnormal_flag": "high",
            },
        )
        assert response.status_code == 200
    with as_actor(graph, graph.physician):
        version = ResultVersion.objects.get(event_id=event)
    with runtime_role():
        conflict = client.post(
            url,
            {
                "action": "upload",
                "order_id": str(world.order.pk),
                "event_id": str(event),
                "name": world.identity.name,
                "birth_date": str(world.identity.birth_date),
                "source": SimpleUploadedFile(
                    "sintetico.pdf",
                    SOURCE + b"\nchanged",
                    content_type="application/pdf",
                ),
            },
        )
        assert conflict.status_code == 409
        response = client.post(url + "source/", {"version_id": str(version.pk)})
        assert response.status_code == 200
        assert response.content == SOURCE
        assert "no-store" in response.headers["Cache-Control"]
        for action, fields in (
            ("acknowledge", {"disposition": "follow_up"}),
            ("release", {"audience": "patient"}),
        ):
            response = client.post(
                url,
                {
                    "action": action,
                    "version_id": str(version.pk),
                    **fields,
                },
            )
            assert response.status_code == 200
        assert client.get(f"/clinics/{graph.clinic_c}/results/").status_code == 403
        assert (
            client.post(url + "source/", {"version_id": str(uuid4())}).status_code
            == 403
        )


def test_lab_http_boundary_replays_without_borrowing_a_staff_session(
    rbac_graph: RbacGraph,
    settings: SettingsWrapper,
) -> None:
    lab = laboratory(rbac_graph, settings)
    body, factory = lab.body(), RequestFactory()
    responses = []
    for _attempt in range(2):
        request = factory.post(
            "/orders/lab/callback/",
            data=body,
            content_type="application/json",
            headers=lab.headers(body),
        )
        with runtime_role():
            response = callback_views.lab_callback(request)
        assert response.status_code == 200
        responses.append(json.loads(response.content))
    assert responses[0] == responses[1]
    assert (
        callback_views.lab_callback(factory.get("/orders/lab/callback/")).status_code
        == 405
    )
    forged = factory.post(
        "/orders/lab/callback/",
        data=body,
        content_type="application/json",
        headers={"X-Lab-Account": str(lab.account), "X-Lab-Signature": "0" * 64},
    )
    with runtime_role():
        assert callback_views.lab_callback(forged).status_code == 403
    changed = json.loads(body)
    changed["critical"] = True
    different = json.dumps(changed).encode()
    replay = factory.post(
        "/orders/lab/callback/",
        data=different,
        content_type="application/json",
        headers=lab.headers(different),
    )
    with runtime_role():
        assert callback_views.lab_callback(replay).status_code == 409


@pytest.mark.parametrize(
    ("case", "status"),
    [
        ("file", 400),
        ("companion", 400),
        ("exam", 400),
        ("place", 400),
        ("uuid", 400),
        ("action", 403),
        ("method", 405),
    ],
)
def test_native_invalid_commands_never_create_a_result(
    rbac_graph: RbacGraph,
    settings: SettingsWrapper,
    case: str,
    status: int,
) -> None:
    graph = rbac_graph
    settings.ALLOWED_HOSTS = ["testserver"]
    world = ordered(graph)
    client = logged_in_client(graph.physician)
    seed_freshness(client, create_totp_device(graph.physician, confirmed=True))
    session = client.session
    session["active_org_id"] = str(graph.organization_a)
    session.save()
    url = f"/clinics/{graph.clinic_a}/results/"
    payload: dict[str, object] = {
        "action": "upload",
        "order_id": str(world.order.pk),
        "event_id": str(uuid4()),
    }
    if case in {"companion", "exam"}:
        payload.update(
            source=SimpleUploadedFile(
                "sintetico.pdf", SOURCE, content_type="application/pdf"
            ),
            unit="source-unit",
            value="7,10" if case == "exam" else "",
        )
    elif case == "place":
        payload["action"] = "place"
    elif case == "uuid":
        payload.update(action="acknowledge", version_id="invalid")
    elif case == "action":
        payload.update(action="unknown", version_id=str(uuid4()))
    with runtime_role():
        response = client.delete(url) if case == "method" else client.post(url, payload)
    assert response.status_code == status
    with as_actor(graph, graph.physician):
        assert not ResultVersion.objects.exists()

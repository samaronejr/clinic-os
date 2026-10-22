"""Authorized invalid resource forms preserve the issued workspace session."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from django.conf import settings
from django.urls import reverse

from core.test_navigation import _client_for, _get, _post

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_invalid_resource_form_is_an_inert_authorized_refusal(
    rbac_graph: RbacGraph,
) -> None:
    client, _user = _client_for(rbac_graph, "receptionist")
    url = reverse("scheduling:resource-settings", args=(rbac_graph.clinic_a,))
    assert _get(client, url).status_code == 200
    snapshot = dict(client.session)

    response = _post(client, url, {"action": "resource"})

    assert response.status_code == 400
    form = response.context["forms"]["resource"]
    assert form.is_bound
    assert set(form.errors) == {"name", "kind", "capacity"}
    assert settings.SESSION_COOKIE_NAME not in response.cookies
    assert response.wsgi_request.session.modified is False
    assert dict(client.session) == snapshot

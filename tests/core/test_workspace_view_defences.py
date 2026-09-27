"""Defence-in-depth exits must execute, even when an outer gate normally wins."""

from __future__ import annotations

import json
from uuid import uuid4

from django.test import Client, override_settings


@override_settings(
    ROOT_URLCONF="workspace_defence_urls",
    MIDDLEWARE=[
        "django.contrib.sessions.middleware.SessionMiddleware",
        "django.contrib.auth.middleware.AuthenticationMiddleware",
    ],
    SESSION_ENGINE="django.contrib.sessions.backends.signed_cookies",
    DEBUG=False,
)
def test_view_local_identity_and_debug_defences() -> None:
    client = Client()
    root = f"/defence/{uuid4()}/"
    responses = (
        client.post(root + "options/"),
        client.post(
            root + "search/", json.dumps({"q": ""}), content_type="application/json"
        ),
        client.get(root + "showcase/"),
    )
    assert [response.status_code for response in responses] == [403, 403, 404]
    for response in responses:
        assert response.wsgi_request.session.modified is False
        assert not response.cookies

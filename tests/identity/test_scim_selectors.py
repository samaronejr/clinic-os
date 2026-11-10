"""The SCIM exception admits directory UUID4s only, never arbitrary selectors."""

from __future__ import annotations

from uuid import uuid4

import pytest
from apps.identity.scim_paths import scim_resource_path
from django.test import Client
from django.urls import Resolver404, resolve


@pytest.mark.parametrize("kind", ["Users", "Groups"])
def test_scim_resource_paths_contain_only_opaque_random_ids(kind: str) -> None:
    # Given server-generated directory identifiers.
    identifier = uuid4()
    # When their sole allowed path form is constructed.
    value = scim_resource_path(kind=kind, resource_id=identifier)
    # Then no account, name, email or clinical selector is included.
    assert value == f"/scim/v2/{kind}/{identifier}"
    assert identifier.version == 4


@pytest.mark.parametrize(
    "path",
    [
        "/scim/v2/Users/synthetic@example.invalid",
        "/scim/v2/Groups/123",
        "/scim/v2/Users/11111111-1111-1111-1111-111111111111",
        "/enterprise/Users/11111111-1111-4111-8111-111111111111",
        "/scim/v2/Patients/11111111-1111-4111-8111-111111111111",
    ],
)
def test_selector_exception_does_not_expand_other_routes(path: str) -> None:
    # Given a path outside the closed directory-resource allowlist.
    # When Django attempts to resolve it.
    with pytest.raises(Resolver404):
        resolve(path)
    # Then no tenant bypass or selector-bearing resource endpoint exists.


def test_get_search_filters_are_refused_without_echoing_the_value() -> None:
    # Given a sensitive directory filter in a URL rather than a search body.
    # When it reaches the SCIM root.
    response = Client().get(
        "/scim/v2/Users",
        {"filter": 'userName eq "synthetic@example.invalid"'},
    )
    # Then the refusal contains none of the supplied personal content.
    assert response.status_code in {400, 401}
    assert b"synthetic@example.invalid" not in response.content

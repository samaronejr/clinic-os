"""New scheduling callbacks must join the closed refusal population."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from apps.scheduling import booking_views
from django.http import HttpResponse
from django.urls import get_resolver, path

from scheduling.write_scope_routes import write_routes

if TYPE_CHECKING:
    from django.http import HttpRequest


@pytest.mark.parametrize("name", ["unclassified", "agenda_view"])
def test_new_scheduling_route_requires_classification(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    def unclassified(request: HttpRequest) -> HttpResponse:
        return HttpResponse()

    unclassified.__module__ = booking_views.__name__
    unclassified.__name__ = name
    resolver = get_resolver()
    monkeypatch.setattr(
        resolver,
        "url_patterns",
        [*resolver.url_patterns, path("new-write/", unclassified, name="new-write")],
    )
    with pytest.raises(AssertionError, match="unclassified scheduling route"):
        write_routes()

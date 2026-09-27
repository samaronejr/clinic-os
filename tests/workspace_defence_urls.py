"""Expose view-local defence checks without an earlier authentication short circuit."""

from __future__ import annotations

from typing import TYPE_CHECKING

from apps.core.api.views import CommandSearchView
from apps.core.command_views import command_options
from apps.identity.views import auth_showcase
from django.urls import path

if TYPE_CHECKING:
    from uuid import UUID

    from django.http import HttpRequest, HttpResponseBase


def options(request: HttpRequest, clinic_id: UUID) -> HttpResponseBase:
    return command_options(request)


def search(request: HttpRequest, clinic_id: UUID) -> HttpResponseBase:
    return CommandSearchView.as_view(authentication_classes=(), permission_classes=())(
        request
    )


def showcase(request: HttpRequest, clinic_id: UUID) -> HttpResponseBase:
    return auth_showcase(request)


urlpatterns = [
    path("defence/<uuid:clinic_id>/options/", options),
    path("defence/<uuid:clinic_id>/search/", search),
    path("defence/<uuid:clinic_id>/showcase/", showcase),
]

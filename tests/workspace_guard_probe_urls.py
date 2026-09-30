"""Real handler endpoints for adversarial tests of the pytest observer."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.http import HttpResponse
from django.urls import path

if TYPE_CHECKING:
    from uuid import UUID

    from django.http import HttpRequest


def response_probe(request: HttpRequest, clinic_id: UUID, status: int) -> HttpResponse:
    if request.POST.get("write"):
        request.session["synthetic-refusal-write"] = True
    return HttpResponse(status=status)


urlpatterns = [
    path("observer/<uuid:clinic_id>/<int:status>/", response_probe),
]

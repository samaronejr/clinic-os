"""Landing page and health endpoints for the project shell."""

from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render

from apps.core.readiness import probe_database_ready


def index(request: HttpRequest) -> HttpResponse:
    """Render the HTMX base shell."""
    return render(request, "base.html")


def healthz(_request: HttpRequest) -> JsonResponse:
    """Report process liveness; never touches the database."""
    return JsonResponse({"status": "ok"})


def readyz(_request: HttpRequest) -> JsonResponse:
    """Report generic readiness without reflecting dependency details."""
    if not probe_database_ready():
        return JsonResponse({"status": "unavailable"}, status=503)
    return JsonResponse({"status": "ok"})

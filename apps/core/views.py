"""Landing page and health endpoints for the project shell."""

from django.db import DatabaseError, connections
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render


def index(request: HttpRequest) -> HttpResponse:
    """Render the HTMX base shell."""
    return render(request, "base.html")


def healthz(_request: HttpRequest) -> JsonResponse:
    """Report process liveness; never touches the database."""
    return JsonResponse({"status": "ok"})


def readyz(_request: HttpRequest) -> JsonResponse:
    """Report readiness by probing the default database connection."""
    try:
        with connections["default"].cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except DatabaseError:
        return JsonResponse({"status": "unavailable"}, status=503)
    return JsonResponse({"status": "ok"})

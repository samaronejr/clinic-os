"""Landing page, workspace entry, installable shell and health endpoints."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Final

from django.contrib.staticfiles import finders
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.templatetags.static import static
from django.views.decorators.http import require_GET

from apps.core.readiness import probe_database_ready
from apps.core.workspace import has_session_identity, resolve_workspace
from apps.identity.otp import privileged_totp_required

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponseBase

SHELL_STATIC_ASSETS: Final = (
    "css/clinic-os.css",
    "css/clinic-os-auth.css",
    "css/clinic-os-intake.css",
    "css/clinic-os-scheduling.css",
    "js/auth-ui.js",
    "js/clinic-os-shell.js",
    "vendor/htmx/htmx.min.js",
    "brand/clinic-ops-logo-compact-dark.svg",
    "icons/clinic-os.svg",
    "manifest.webmanifest",
)
SERVICE_WORKER_TEMPLATE: Final = "clinic-os-sw.js"
VERSION_DIGEST_LENGTH: Final = 12


def index(request: HttpRequest) -> HttpResponseBase:
    """Send signed-in sessions to their workspace; greet everyone else."""
    if has_session_identity(request):
        return redirect("workspace-home")
    return render(request, "base.html")


def workspace_home_continuation() -> str:
    """Resume an unsafe workspace challenge at the workspace home."""
    return "/workspace/"


@privileged_totp_required(workspace_home_continuation)
@require_GET
def workspace_home(request: HttpRequest) -> HttpResponseBase:
    """Open today's agenda for the current clinic, or explain the missing clinic."""
    workspace = resolve_workspace(request)
    if workspace is not None and workspace.clinic is not None:
        return redirect(workspace.clinic.agenda_url)
    return render(request, "core/no_clinic.html")


def shell_static_version(assets: tuple[str, ...] = SHELL_STATIC_ASSETS) -> str:
    """Digest the shell's static bytes so the worker cache is content-versioned."""
    digest = hashlib.sha256()
    for asset in assets:
        path = finders.find(asset)
        if not isinstance(path, str):
            msg = f"shell static asset is missing: {asset}"
            raise FileNotFoundError(msg)
        with open(path, "rb") as handle:  # noqa: PTH123 - finders return plain paths
            digest.update(asset.encode())
            digest.update(b"\0")
            digest.update(handle.read())
            digest.update(b"\0")
    return digest.hexdigest()[:VERSION_DIGEST_LENGTH]


@require_GET
def service_worker(request: HttpRequest) -> HttpResponseBase:
    """Serve the static-only worker at the root scope for signed-in sessions."""
    response = render(
        request,
        SERVICE_WORKER_TEMPLATE,
        {
            "version": shell_static_version(),
            "precache_json": json.dumps(
                [static(asset) for asset in SHELL_STATIC_ASSETS]
            ),
        },
        content_type="text/javascript; charset=utf-8",
    )
    response.headers["Service-Worker-Allowed"] = "/"
    return response


def healthz(_request: HttpRequest) -> JsonResponse:
    """Report process liveness; never touches the database."""
    return JsonResponse({"status": "ok"})


def readyz(_request: HttpRequest) -> JsonResponse:
    """Report generic readiness without reflecting dependency details."""
    if not probe_database_ready():
        return JsonResponse({"status": "unavailable"}, status=503)
    return JsonResponse({"status": "ok"})

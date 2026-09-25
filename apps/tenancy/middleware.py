"""Synchronous request tenant transaction boundary."""

from collections.abc import Callable
from typing import Final
from uuid import UUID

from django.contrib.auth import SESSION_KEY
from django.db import transaction
from django.http import HttpRequest
from django.http.response import HttpResponseBase
from django.shortcuts import render

from apps.core.api.errors import UI_API_PREFIX, ui_api_denial_response
from apps.intake.patient_access import (
    PATIENT_SESSION_KEY,
    patient_session_context,
)
from apps.tenancy.db import (
    TenantAccessDeniedError,
    clear_connection_tenant_gucs,
    tenant_context,
)

BYPASS_PATHS: Final = frozenset(
    {
        "/",
        "/healthz",
        "/healthz/",
        "/readyz",
        "/readyz/",
        "/auth/login/",
    }
)
BYPASS_PREFIXES: Final = (
    "/static/",
    "/prescription/signing/callback/",
    "/prescription/verify/",
)
PATIENT_ACCESS_PREFIX: Final = "/patient/access/"
PATIENT_PREFIX: Final = "/patient/"
SERVER_ERROR_STATUS: Final = 500


class TenantStreamingResponseError(RuntimeError):
    """Reject deferred tenant work after the request transaction closes."""

    def __init__(self) -> None:
        """Expose the unsupported streaming contract."""
        super().__init__("streaming tenant responses are not supported")


def _staff_denial(request: HttpRequest) -> HttpResponseBase:
    """Refuse one staff request; the UI API keeps its JSON error contract."""
    if request.path_info.startswith(UI_API_PREFIX):
        return ui_api_denial_response()
    return render(request, "403.html", status=403)


def _parse_uuid(raw_value: str | None) -> UUID | None:
    if raw_value is None:
        return None
    try:
        return UUID(raw_value)
    except ValueError:
        return None


class TenantMiddleware:
    """Keep lazy authentication and downstream ORM work inside tenant context."""

    def __init__(
        self,
        get_response: Callable[[HttpRequest], HttpResponseBase],
    ) -> None:
        """Store the synchronous downstream request handler."""
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponseBase:
        """Validate signed session identifiers and execute the full response chain."""
        path = request.path_info
        if path in BYPASS_PATHS or path.startswith(
            (*BYPASS_PREFIXES, PATIENT_ACCESS_PREFIX)
        ):
            clear_connection_tenant_gucs()
            try:
                return self.get_response(request)
            finally:
                clear_connection_tenant_gucs()

        if path.startswith(PATIENT_PREFIX):
            return self._patient(request)

        raw_user_id = request.session.get(SESSION_KEY)
        raw_org_id = request.session.get("active_org_id")
        if not isinstance(raw_user_id, str) or not isinstance(raw_org_id, str):
            clear_connection_tenant_gucs()
            return _staff_denial(request)

        user_id = _parse_uuid(raw_user_id)
        org_id = _parse_uuid(raw_org_id)
        if user_id is None or org_id is None:
            clear_connection_tenant_gucs()
            return _staff_denial(request)

        try:
            with tenant_context(user_id, org_id):
                response = self.get_response(request)
                if response.streaming:
                    raise TenantStreamingResponseError
                if response.status_code >= SERVER_ERROR_STATUS:
                    transaction.set_rollback(True)
                return response
        except TenantAccessDeniedError:
            return _staff_denial(request)

    def _patient(self, request: HttpRequest) -> HttpResponseBase:
        """Run one patient request inside its own session-bound transaction.

        The signed session cookie carries only the patient session id; the
        database touch re-validates revocation and both expiries and renews
        the idle deadline. No staff tenant GUC is ever set, so staff tables
        stay fail-closed for the whole request.
        """
        session_id = _parse_uuid(request.session.get(PATIENT_SESSION_KEY))
        if session_id is None:
            clear_connection_tenant_gucs()
            return render(
                request,
                "intake/patient_gate.html",
                {"state": "required"},
                status=403,
            )
        try:
            with patient_session_context(session_id) as binding:
                if binding is None:
                    return render(
                        request,
                        "intake/patient_gate.html",
                        {"state": "required"},
                        status=403,
                    )
                response = self.get_response(request)
                if response.streaming:
                    raise TenantStreamingResponseError
                if response.status_code >= SERVER_ERROR_STATUS:
                    transaction.set_rollback(True)
                return response
        finally:
            clear_connection_tenant_gucs()

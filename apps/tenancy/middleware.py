"""Synchronous request tenant transaction boundary."""

from collections.abc import Callable
from http import HTTPStatus
from typing import Final
from uuid import UUID

from django.contrib.auth import SESSION_KEY, logout
from django.db import connection, transaction
from django.http import HttpRequest
from django.http.response import HttpResponseBase
from django.shortcuts import render

from apps.core.api.errors import UI_API_PREFIX, ui_api_denial_response
from apps.core.patient_context import persist_bound_patient_context
from apps.core.workspace import finish_clinic_selection
from apps.core.workspace_access import workspace_denial
from apps.identity.otp import auth_redirect
from apps.identity.session_inventory import (
    session_cookie_is_revoked,
    session_is_active,
    touch_session,
)
from apps.intake.delegate_views import delegate_session_status, renewal_offer
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
        # WebAuthn assertions authenticate their own exact stored credential,
        # then open tenant_context; no body-supplied tenant is accepted.
        "/auth/passkey/assert/begin/",
        "/auth/passkey/assert/finish/",
        "/auth/signup/",
        "/auth/invitation/",
        "/auth/identity-notification/callback/",
        # Sessionless ops scrape endpoint; authenticates with its own bearer
        # token plus a network allowlist (D-19, apps.core.telemetry).
        "/internal/metrics",
        "/internal/metrics/",
    }
)
BYPASS_PREFIXES: Final = (
    "/waiting-display/",  # Device capability only; staff cookies grant nothing.
    "/static/",
    "/rt/",  # Ticket endpoint owns a short, independently authenticated txn.
    "/workflows/synthetic/callback/",  # HMAC before stored-scope resolution.
    "/workflows/synthetic/callback/",  # HMAC before stored-scope resolution.
    "/prescription/signing/callback/",
    "/prescription/verify/",
)
PATIENT_ACCESS_PREFIX: Final = "/patient/access/"
PATIENT_PREFIX: Final = "/patient/"
SERVER_ERROR_STATUS: Final = 500
HANDLED_REJECTION_ATTR: Final = "clinic_handled_rejection"


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
        """Commit clinic selection only after the response and transaction succeed."""
        accepted = False
        try:
            # These entrypoints authenticate independently and must retain
            # their side-effect-free denial rather than mutate a staff session.
            if request.path_info.startswith((*BYPASS_PREFIXES, PATIENT_PREFIX)):
                return self._response(request)
            incoming_key = request.session.session_key
            raw_session_user = request.session.get(SESSION_KEY)
            key = request.session.session_key
            if isinstance(raw_session_user, str) and key is not None:
                signed_user_id = _parse_uuid(raw_session_user)
                if signed_user_id is not None:
                    clear_connection_tenant_gucs()
                    if not session_is_active(user_id=signed_user_id, session_key=key):
                        if request.path_info.startswith(UI_API_PREFIX):
                            return ui_api_denial_response()
                        logout(request)
                        if request.path_info not in BYPASS_PATHS:
                            return auth_redirect(request, "/auth/login/")
            elif incoming_key is not None and incoming_key.isascii():
                clear_connection_tenant_gucs()
                if (
                    session_cookie_is_revoked(session_key=incoming_key)
                    and request.path_info not in BYPASS_PATHS
                ):
                    if request.path_info.startswith(UI_API_PREFIX):
                        return ui_api_denial_response()
                    # No payload remains to log out. Do not delete a newer
                    # cookie that the browser may already hold after rotation.
                    return auth_redirect(request, "/auth/login/")
            response = self._response(request)
            accepted = response.status_code < HTTPStatus.BAD_REQUEST
            return response
        finally:
            finish_clinic_selection(request, accepted=accepted)

    def _response(self, request: HttpRequest) -> HttpResponseBase:
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
                elif response.status_code not in {403, 404}:
                    persist_bound_patient_context(request)
                    # None uses the existing no-write path for marked rejections.
                    touch_session(
                        session_key=(
                            None
                            if getattr(response, HANDLED_REJECTION_ATTR, False)
                            else request.session.session_key
                        ),
                        http_status=response.status_code,
                    )
                return response
        except TenantAccessDeniedError:
            # Resolve account status without opening a tenant. An inactive
            # account must still reach the existing anonymous-session cleanup
            # and login/step-up redirects, never a staff tenant transaction.
            with transaction.atomic(), connection.cursor() as cursor:
                cursor.execute(
                    "SELECT set_config('app.current_user_id', %s, true)",
                    [str(user_id)],
                )
                cursor.execute("SELECT is_active FROM clinic_app.load_current_user()")
                inactive = cursor.fetchone() == (False,)
            if inactive:
                clear_connection_tenant_gucs()
                response = self.get_response(request)
            else:
                response = _staff_denial(request)
            return response

    def process_view(
        self,
        request: HttpRequest,
        view_func: Callable[..., HttpResponseBase],
        view_args: tuple[object, ...],
        view_kwargs: dict[str, object],
    ) -> HttpResponseBase | None:
        """Apply workspace permissions inside the already-open tenant transaction."""
        del view_func, view_args, view_kwargs
        return workspace_denial(request)

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
                    return self._delegate_recovery(request, session_id)
                response = self.get_response(request)
                if response.streaming:
                    raise TenantStreamingResponseError
                if response.status_code >= SERVER_ERROR_STATUS:
                    transaction.set_rollback(True)
                return response
        finally:
            clear_connection_tenant_gucs()

    def _delegate_recovery(
        self, request: HttpRequest, session_id: UUID
    ) -> HttpResponseBase:
        """Open only renewal/reset for an expired grant, never a clinical surface."""
        state = delegate_session_status(session_id)
        if (
            state in {"expired", "revoked"}
            and request.method == "POST"
            and (
                request.path_info == "/patient/delegation/switch/"
                or (
                    state == "expired"
                    and request.path_info == "/patient/delegation/renew/"
                )
            )
        ):
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT set_config('app.current_patient_session',%s,true)",
                    [str(session_id)],
                )
            return self.get_response(request)
        context: dict[str, object] = {"state": "required"}
        if state == "expired":
            context = {
                "state": "delegate_expired",
                "renewal_offer": renewal_offer(session_id),
            }
        return render(request, "intake/patient_gate.html", context, status=403)

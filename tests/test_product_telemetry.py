from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from typing import TYPE_CHECKING, Protocol, runtime_checkable
from urllib.parse import urlencode

import sentry_sdk
from django.test import Client, override_settings
from django.urls import path
from sentry_sdk.integrations.django import DjangoIntegration
from sentry_sdk.transport import Transport

from test_production_settings import VALID_HOSTS, VALID_RUNTIME_TOKEN

if TYPE_CHECKING:
    from pathlib import Path

    from django.http import HttpRequest, HttpResponse
    from sentry_sdk.envelope import Envelope
    from sentry_sdk.types import Event, Hint

_run_process = subprocess.run

CANARIES = (
    "synthetic-patient-name-canary",
    "synthetic-birth-date-canary",
    "synthetic-search-canary",
    "synthetic-page-canary",
    "synthetic-enrollment-canary",
    "synthetic-csrf-totp-canary",
    "synthetic-cookie-canary",
    "synthetic-header-canary",
)


@runtime_checkable
class _EventScrubber(Protocol):
    def __call__(self, event: Event, hint: Hint) -> Event | None: ...


class _CaptureTransport(Transport):
    def __init__(self) -> None:
        super().__init__()
        self.payloads: list[str] = []

    def capture_envelope(self, envelope: Envelope) -> None:
        event: object = envelope.get_event()
        if event is not None:
            self.payloads.append(json.dumps(event, default=str, sort_keys=True))


class _SyntheticTelemetryError(RuntimeError):
    pass


def _failing_view(_request: HttpRequest) -> HttpResponse:
    raise _SyntheticTelemetryError


urlpatterns = [path("sentry-canary/", _failing_view)]


@override_settings(
    ROOT_URLCONF=__name__,
    DEBUG=False,
    ALLOWED_HOSTS=["testserver"],
    MIDDLEWARE=[],
)
def test_real_sentry_django_event_contains_no_request_canaries() -> None:
    module = importlib.import_module("config.settings.telemetry")
    scrubber = getattr(module, "scrub_sentry_event", None)
    assert isinstance(scrubber, _EventScrubber)
    transport = _CaptureTransport()
    sentry_sdk.init(
        dsn="https://public@example.invalid/1",
        integrations=[DjangoIntegration()],
        send_default_pii=False,
        include_local_variables=False,
        max_request_body_size="never",
        before_send=scrubber,
        transport=transport,
    )
    try:
        client = Client(raise_request_exception=False)
        client.cookies["synthetic"] = CANARIES[6]
        response = client.post(
            f"/sentry-canary/?search={CANARIES[2]}&page={CANARIES[3]}",
            {
                "birth_date": CANARIES[1],
                "csrfmiddlewaretoken": CANARIES[5],
                "enrollment": CANARIES[4],
                "patient_name": CANARIES[0],
            },
            headers={"X-Synthetic-Canary": CANARIES[7]},
        )
        sentry_sdk.flush(timeout=2)
    finally:
        sentry_sdk.get_client().close(timeout=2)
        sentry_sdk.get_global_scope().set_client(None)
    assert response.status_code == 500
    assert transport.payloads
    serialized = "".join(transport.payloads)
    assert all(canary not in serialized for canary in CANARIES)


def test_spoofed_forwarded_proto_is_ignored_and_redirected(tmp_path: Path) -> None:
    ca = tmp_path / "db-ca.pem"
    ca.write_text("synthetic CA fixture\n", encoding="ascii")
    ca.chmod(0o444)
    query = urlencode(
        {
            "connect_timeout": "2",
            "sslmode": "verify-full",
            "sslrootcert": str(ca),
        }
    )
    environment = os.environ.copy()
    environment.update(
        {
            "ALLOWED_HOSTS": VALID_HOSTS,
            "APP_DATABASE_URL": (
                "postgresql://clinic_app:synthetic@db.qa.clinic-os.dev:5432/"
                f"clinic?{query}"
            ),
            "CLINIC_DATA_MODE": "synthetic",
            "DJANGO_SETTINGS_MODULE": "config.settings.prod",
            "SECRET_KEY": VALID_RUNTIME_TOKEN,
            "SECURE_SSL_HOST": "app.qa.clinic-os.dev",
        }
    )
    code = (
        "import django; django.setup(); "
        "from django.http import HttpResponse; "
        "from django.middleware.security import SecurityMiddleware; "
        "from django.test import RequestFactory; "
        "r=RequestFactory().get('/',HTTP_HOST='app.qa.clinic-os.dev',"
        "HTTP_X_FORWARDED_PROTO='https'); "
        "assert not r.is_secure(); "
        "x=SecurityMiddleware(lambda request: HttpResponse('ok'))(r); "
        "assert x.status_code==301; assert x['Location'].startswith('https://')"
    )
    result = _run_process(
        (sys.executable, "-c", code),
        check=False,
        capture_output=True,
        env=environment,
        text=True,
    )
    assert result.returncode == 0, result.stderr

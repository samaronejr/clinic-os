from __future__ import annotations

import base64
import importlib
import json
from types import TracebackType
from typing import TYPE_CHECKING, Final, Protocol, runtime_checkable
from unittest.mock import patch

import pytest
import sentry_sdk
from apps.identity.models import User
from config.settings import base as base_settings
from config.settings.telemetry import drop_sentry_transaction, scrub_sentry_event
from django.test import Client, override_settings
from django.views.debug import ExceptionReporter
from django_otp.plugins.otp_totp.models import TOTPDevice
from sentry_sdk.client import Client as SentryClient
from sentry_sdk.utils import event_from_exception

from otp_test_support import runtime_role
from rbac_fixtures import RBAC_RAW_CREDENTIAL

if TYPE_CHECKING:
    from _pytest.logging import LogCaptureFixture
    from _pytest.monkeypatch import MonkeyPatch

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)

SYNTHETIC_KEY_BYTES: Final = b"todo9-qr-seed-canary"
SECRET_SENTINEL: Final = base64.b32encode(SYNTHETIC_KEY_BYTES).decode("ascii")
QR_DATA_URI_SENTINEL: Final = "data:image/png;base64,TODO9_QR_DATA_URI_CANARY"


type ExceptionInfo = tuple[type[BaseException], BaseException, TracebackType]


@runtime_checkable
class FailureResponse(Protocol):
    status_code: int
    exc_info: ExceptionInfo | None
    content: bytes


class SyntheticTelemetryError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("synthetic telemetry failure")


class FailingQrCode:
    def __init__(self, *, box_size: int, border: int) -> None:
        del box_size, border

    def add_data(self, data: str) -> None:
        del data
        raise SyntheticTelemetryError


def _synthetic_device(graph: RbacGraph) -> TOTPDevice:
    return TOTPDevice(
        user_id=graph.physician,
        name="Synthetic telemetry authenticator",
        confirmed=False,
        key=SYNTHETIC_KEY_BYTES.hex(),
    )


def _logged_in_client(graph: RbacGraph) -> Client:
    username = User.objects.get(pk=graph.physician).username
    client = Client(raise_request_exception=False)
    with runtime_role():
        assert client.login(username=username, password=RBAC_RAW_CREDENTIAL)
    return client


def _assert_failure_surfaces_exclude(
    response: FailureResponse,
    caplog: LogCaptureFixture,
    *sentinels: str,
) -> None:
    assert response.status_code == 500
    assert response.exc_info is not None
    event, _ = event_from_exception(
        response.exc_info,
        client_options=SentryClient(
            dsn=None,
            include_local_variables=False,
        ).options,
    )
    reporter = ExceptionReporter(None, *response.exc_info)
    surfaces = (
        json.dumps(event, default=str),
        json.dumps(reporter.get_traceback_data(), default=str),
        response.content.decode("utf-8", errors="replace"),
        caplog.text,
    )
    for sentinel in sentinels:
        assert all(sentinel not in surface for surface in surfaces)


def test_sentry_initialization_disables_local_variable_capture(
    monkeypatch: MonkeyPatch,
) -> None:
    with monkeypatch.context() as scoped, patch("sentry_sdk.init") as sentry_init:
        scoped.setenv("SENTRY_DSN", "https://public@example.invalid/1")
        scoped.delenv("SENTRY_ENVIRONMENT", raising=False)
        scoped.delenv("SENTRY_RELEASE", raising=False)
        importlib.reload(base_settings)

        sentry_init.assert_called_once_with(
            dsn="https://public@example.invalid/1",
            send_default_pii=False,
            include_local_variables=False,
            max_request_body_size="never",
            before_send=scrub_sentry_event,
            before_send_transaction=drop_sentry_transaction,
            environment="production",
            release="unversioned",
            server_name="clinic-os",
            auto_session_tracking=False,
            spotlight=False,
        )

    importlib.reload(base_settings)
    assert not sentry_sdk.is_initialized()


@override_settings(DEBUG=False)
def test_qr_generation_failure_exposes_no_seed_or_provisioning_material(
    rbac_graph: RbacGraph,
    caplog: LogCaptureFixture,
) -> None:
    device = _synthetic_device(rbac_graph)
    client = _logged_in_client(rbac_graph)

    with (
        patch(
            "apps.identity.otp_views.get_or_create_pending_device",
            return_value=device,
        ),
        patch("apps.identity.otp.QRCODE.QRCode", FailingQrCode),
        runtime_role(),
    ):
        response = client.post("/auth/enroll/", {"action": "start"})

    assert isinstance(response, FailureResponse)
    _assert_failure_surfaces_exclude(response, caplog, SECRET_SENTINEL)


@override_settings(DEBUG=False)
def test_enrollment_render_failure_exposes_no_qr_data_uri(
    rbac_graph: RbacGraph,
    caplog: LogCaptureFixture,
) -> None:
    device = _synthetic_device(rbac_graph)
    client = _logged_in_client(rbac_graph)

    with (
        patch(
            "apps.identity.otp_views.get_or_create_pending_device",
            return_value=device,
        ),
        patch(
            "apps.identity.otp_views.provisioning_qr_data_uri",
            return_value=QR_DATA_URI_SENTINEL,
        ),
        patch(
            "apps.identity.otp_views.render",
            side_effect=SyntheticTelemetryError(),
        ),
        runtime_role(),
    ):
        response = client.post("/auth/enroll/", {"action": "start"})

    assert isinstance(response, FailureResponse)
    _assert_failure_surfaces_exclude(response, caplog, QR_DATA_URI_SENTINEL)

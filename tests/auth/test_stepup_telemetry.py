from __future__ import annotations

import json
from types import TracebackType
from typing import TYPE_CHECKING, Final, Protocol, runtime_checkable
from unittest.mock import patch

import pytest
from apps.identity.models import User
from django.test import Client, override_settings
from django.views.debug import ExceptionReporter
from sentry_sdk.client import Client as SentryClient
from sentry_sdk.utils import event_from_exception

from otp_test_support import create_totp_device, runtime_role
from rbac_fixtures import RBAC_RAW_CREDENTIAL

if TYPE_CHECKING:
    from _pytest.logging import LogCaptureFixture

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)

type ExceptionInfo = tuple[type[BaseException], BaseException, TracebackType]
SENSITIVE_VALUE_CANARY: Final = "123456789012-step-up-telemetry-canary"


@runtime_checkable
class _FailureResponse(Protocol):
    status_code: int
    exc_info: ExceptionInfo | None
    content: bytes


class _SyntheticStepUpTelemetryError(RuntimeError):
    pass


@override_settings(DEBUG=False)
def test_invalid_token_is_absent_from_exception_telemetry(
    rbac_graph: RbacGraph,
    caplog: LogCaptureFixture,
) -> None:
    device = create_totp_device(rbac_graph.physician, confirmed=True)
    user = User.objects.get(pk=rbac_graph.physician)
    client = Client(raise_request_exception=False)
    with runtime_role():
        assert client.login(username=user.username, password=RBAC_RAW_CREDENTIAL)
        with patch(
            "apps.identity.stepup_views.render",
            side_effect=_SyntheticStepUpTelemetryError(),
        ):
            response = client.post(
                "/auth/step-up/",
                {
                    "otp_device": device.persistent_id,
                    "otp_token": SENSITIVE_VALUE_CANARY,
                },
            )

    assert isinstance(response, _FailureResponse)
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
    assert all(SENSITIVE_VALUE_CANARY not in surface for surface in surfaces)

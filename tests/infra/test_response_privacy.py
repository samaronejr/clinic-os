from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from apps.core.middleware import is_private_product_path
from django.conf import settings
from django.test import Client

from otp_test_support import OTP_RAW_CREDENTIAL, create_receptionist, runtime_role

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

TELEMETRY_MIDDLEWARE = "apps.core.telemetry.TelemetryMiddleware"
PRIVACY_MIDDLEWARE = "apps.core.middleware.ResponsePrivacyMiddleware"
LIVE_HALT_MIDDLEWARE = "apps.core.middleware.LiveModeHaltMiddleware"
SECURITY_MIDDLEWARE = "django.middleware.security.SecurityMiddleware"
WHITENOISE_MIDDLEWARE = "whitenoise.middleware.WhiteNoiseMiddleware"
PRIVATE_DIRECTIVES = ("private", "no-store", "no-cache", "must-revalidate")


def test_privacy_middleware_precedes_the_preserved_security_whitenoise_pair() -> None:
    middleware = list(settings.MIDDLEWARE)

    # Telemetry is outermost so every response — including early refusals —
    # carries a request id and a latency observation.
    assert middleware[0] == TELEMETRY_MIDDLEWARE
    assert middleware[1] == PRIVACY_MIDDLEWARE
    # The live-mode halt sits between privacy and security so halted
    # responses still carry the private cache contract.
    assert middleware[2] == LIVE_HALT_MIDDLEWARE
    security = middleware.index(SECURITY_MIDDLEWARE)
    assert middleware[security - 1] == LIVE_HALT_MIDDLEWARE
    assert middleware[security + 1] == WHITENOISE_MIDDLEWARE


def test_product_landing_response_is_private_and_uncacheable() -> None:
    response = Client().get("/")

    cache_control = response.headers["Cache-Control"]
    for directive in PRIVATE_DIRECTIVES:
        assert directive in cache_control
    vary = response.headers["Vary"]
    assert "Cookie" in vary
    assert "HX-Request" in vary


def test_static_paths_stay_outside_the_private_product_scope() -> None:
    assert not is_private_product_path("/static/css/clinic-os.css")
    assert not is_private_product_path("/static/vendor/htmx/htmx.min.js")
    assert is_private_product_path("/")
    assert is_private_product_path("/auth/protected/")
    assert is_private_product_path("/intake/patients/")


def test_static_responses_are_not_marked_no_store() -> None:
    response = Client().get("/static/css/clinic-os.css")

    assert "no-store" not in response.headers.get("Cache-Control", "")


@pytest.mark.django_db(transaction=True)
def test_inner_middleware_refusal_is_still_private_and_uncacheable() -> None:
    response = Client().get("/auth/protected/")

    assert response.status_code == 403
    cache_control = response.headers["Cache-Control"]
    for directive in PRIVATE_DIRECTIVES:
        assert directive in cache_control
    assert "HX-Request" in response.headers["Vary"]


@pytest.mark.django_db(transaction=True)
def test_authenticated_product_response_is_private_and_uncacheable(
    rbac_graph: RbacGraph,
) -> None:
    client = Client()
    receptionist = create_receptionist(rbac_graph)
    with runtime_role():
        assert client.login(
            username=receptionist.username,
            password=OTP_RAW_CREDENTIAL,
        )
        response = client.get("/auth/protected/")

    assert response.status_code == 200
    cache_control = response.headers["Cache-Control"]
    for directive in PRIVATE_DIRECTIVES:
        assert directive in cache_control

import pytest
import sentry_sdk
from apps.core import views
from config.celery import app as celery_app
from django.conf import settings
from django.test import Client


def test_healthz_reports_liveness_without_database_access(client: Client) -> None:
    # Given: no django_db marker, so pytest-django blocks every database query
    # When: the liveness endpoint is requested
    # Then: it answers 200 without touching the database
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz_reports_ready_when_the_database_probe_succeeds(
    client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: the bounded production readiness contract succeeds
    monkeypatch.setattr(views, "probe_database_ready", lambda: True)

    # When: the readiness endpoint is requested
    response = client.get("/readyz")

    # Then: it reports ready
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz_reports_unavailable_when_the_database_probe_fails(
    client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: the bounded production readiness contract rejects its dependency
    monkeypatch.setattr(views, "probe_database_ready", lambda: False)

    # When: the readiness endpoint is requested
    response = client.get("/readyz")

    # Then: it reports unavailable with a 503 while liveness semantics stay separate
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}


def test_index_renders_the_htmx_base_shell(client: Client) -> None:
    # Given: the project shell landing route
    # When: the index page is requested anonymously
    response = client.get("/")
    content = response.content.decode()

    # Then: the accessible HTMX base template is rendered
    assert response.status_code == 200
    assert "htmx.min.js" in content
    assert '<main id="main-content"' in content
    assert 'class="skip-link"' in content


def test_rest_framework_defaults_to_session_auth_and_authenticated() -> None:
    # Given: the Phase 0 DRF baseline settings
    rest_framework = settings.REST_FRAMEWORK

    # Then: session authentication, authenticated-by-default, and throttling apply
    assert rest_framework["DEFAULT_AUTHENTICATION_CLASSES"] == [
        "rest_framework.authentication.SessionAuthentication",
    ]
    assert rest_framework["DEFAULT_PERMISSION_CLASSES"] == [
        "rest_framework.permissions.IsAuthenticated",
    ]
    assert rest_framework["DEFAULT_THROTTLE_CLASSES"] == [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ]
    assert set(rest_framework["DEFAULT_THROTTLE_RATES"]) == {"anon", "user"}


def test_sentry_stays_dormant_without_a_dsn() -> None:
    # Given: no SENTRY_DSN is configured in the environment
    # Then: settings expose an empty DSN and the SDK is never initialized
    assert settings.SENTRY_DSN == ""
    assert not sentry_sdk.is_initialized()


def test_celery_app_registers_no_project_tasks() -> None:
    # Given: the thin Phase 0 celery application
    # When: registered tasks are inspected
    project_tasks = [
        name for name in celery_app.tasks if not name.startswith("celery.")
    ]

    # Then: only celery built-ins exist; the project defines no tasks
    assert project_tasks == []

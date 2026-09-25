"""Exact role defaults and the deliberate transaction/session split."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest
from django.conf import settings
from django.db import connection, connections

from infra.test_database_options import _ca, _url
from infra.test_production_settings import VALID_HOSTS, VALID_RUNTIME_TOKEN

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.django_db(transaction=True)
def test_runtime_role_defaults_are_pinned_for_transaction_pooling() -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT rolconfig FROM pg_roles WHERE rolname = 'clinic_app'")
        row = cursor.fetchone()
    assert row is not None
    assert set(row[0]) == {
        "search_path=clinic_app, public",
        "idle_in_transaction_session_timeout=15s",
    }


def test_runtime_aliases_disable_prepared_statements_and_implicit_transactions() -> (
    None
):
    assert settings.DATABASES["locks"]["TEST"] == {
        "MIRROR": "default",
        "CHARSET": None,
        "COLLATION": None,
        "MIGRATE": True,
        "NAME": None,
    }
    for name in ("default", "locks"):
        config = settings.DATABASES[name]
        assert config["OPTIONS"]["prepare_threshold"] is None
        assert config["OPTIONS"]["options"] == "-c search_path=clinic_app,public"
        assert config["ATOMIC_REQUESTS"] is False
        assert connections[name].get_connection_params()["prepare_threshold"] is None


@pytest.mark.parametrize("explicit", [False, True])
def test_production_pooling_requires_an_explicit_session_connection(
    tmp_path: Path,
    explicit: bool,
) -> None:
    database_url = _url(_ca(tmp_path))
    environment = {
        **os.environ,
        "APP_DATABASE_URL": database_url,
        "SECRET_KEY": VALID_RUNTIME_TOKEN,
        "ALLOWED_HOSTS": VALID_HOSTS,
        "SECURE_SSL_HOST": "app.qa.clinic-os.dev",
        "CLINIC_DATA_MODE": "synthetic",
        "CLINIC_SECRET_BACKEND": "synthetic-file",
        "CLINIC_SECRET_DIR": str(tmp_path / "secrets"),
        "DATABASE_TRANSACTION_POOLING": "true",
        "CELERY_BROKER_URL": "memory://",
        "CELERY_RESULT_BACKEND": "",
    }
    environment.pop("LOCKS_DATABASE_URL", None)
    environment.pop("REALTIME_TOPIC_SECRET", None)
    if explicit:
        environment["LOCKS_DATABASE_URL"] = database_url
    result = subprocess.run(
        [sys.executable, "-c", "import config.settings.prod"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    assert (result.returncode == 0) is explicit
    assert database_url not in result.stderr
    if explicit:
        environment["REALTIME_TOPIC_SECRET"] = ""
        rejected = subprocess.run(
            [sys.executable, "-c", "import config.settings.prod"],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        assert rejected.returncode != 0
        assert database_url not in rejected.stderr


def test_realtime_settings_mount_only_stream_without_tenant_middleware(
    tmp_path: Path,
) -> None:
    env = {
        **os.environ,
        "CLINIC_REALTIME_MODE": "local",
        "DJANGO_SETTINGS_MODULE": "config.settings.realtime",
        "CELERY_BROKER_URL": "memory://",
        "CELERY_RESULT_BACKEND": "",
    }
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import django,json; django.setup(); "
            "from django.conf import settings as s; "
            "from django.urls import get_resolver; "
            "print(json.dumps({'routes':[str(x.pattern) "
            "for x in get_resolver().url_patterns], 'middleware':s.MIDDLEWARE, "
            "'ages':[d['CONN_MAX_AGE'] for d in s.DATABASES.values()]}))",
        ],
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    payload = json.loads(result.stdout)
    assert payload["routes"] == ["rt/stream"]
    assert payload["ages"] == [0, 0]
    assert payload["middleware"] == [
        "apps.core.middleware.ResponsePrivacyMiddleware",
        "apps.core.middleware.LiveModeHaltMiddleware",
        "apps.core.middleware.ContentSecurityPolicyMiddleware",
        "django.contrib.sessions.middleware.SessionMiddleware",
        "django.middleware.csrf.CsrfViewMiddleware",
        "django.contrib.auth.middleware.AuthenticationMiddleware",
        "django_otp.middleware.OTPMiddleware",
    ]

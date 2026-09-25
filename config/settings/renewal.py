"""Serving settings for the renewal runner's supervised local runtime.

Real middleware, CSRF, sessions, and RLS apply exactly as in production; the
only relaxations are loopback HTTP (no TLS requirement) and direct static
storage so no collectstatic step is needed. The serving DSN must still
resolve to the ``clinic_app`` role on a loopback host.
"""

from __future__ import annotations

import os
from urllib.parse import unquote, urlsplit

import environ

from . import base
from .database import agent_database_config

env = environ.Env()

base.export_settings(globals())

SECRET_KEY = env("SECRET_KEY")
SECRET_KEY_CONFIGURED = True
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=["127.0.0.1", "localhost"])
CLINIC_PROCESS_ROLE = "clinic_app"

_app_dsn = os.environ.get("APP_DATABASE_URL", "")
_parsed = urlsplit(_app_dsn)
if (
    _parsed.scheme != "postgresql"
    or unquote(_parsed.username or "") != CLINIC_PROCESS_ROLE
    or (_parsed.hostname or "") not in {"127.0.0.1", "localhost", "::1"}
):
    from django.core.exceptions import ImproperlyConfigured

    message = "renewal serving DSN must use the clinic_app role on loopback"
    raise ImproperlyConfigured(message)

DATABASES = {"default": env.db("APP_DATABASE_URL")}
DATABASES["default"]["ATOMIC_REQUESTS"] = False
DATABASES["default"].setdefault("OPTIONS", {})["options"] = (
    "-c search_path=clinic_app,public"
)

DATABASES.update(agent_database_config(os.environ, primary=DATABASES["default"]))

DEBUG = False
SECURE_SSL_REDIRECT = False
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
SECURE_PROXY_SSL_HEADER = None
STATICFILES_BACKEND = "django.contrib.staticfiles.storage.StaticFilesStorage"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": STATICFILES_BACKEND},
}
WHITENOISE_AUTOREFRESH = True
WHITENOISE_USE_FINDERS = True

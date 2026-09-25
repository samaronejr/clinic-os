from __future__ import annotations

import os

from django.core.exceptions import ImproperlyConfigured

from . import base
from .contracts import (
    parse_production_hosts,
    validate_runtime_secret,
    validate_secure_ssl_host,
)
from .database import agent_database_config, parse_database_url

base.export_settings(globals())

SECRET_KEY = validate_runtime_secret(os.environ.get("SECRET_KEY", ""))
SECRET_KEY_CONFIGURED = True
ALLOWED_HOSTS = parse_production_hosts(os.environ.get("ALLOWED_HOSTS", ""))
SECURE_SSL_HOST = validate_secure_ssl_host(
    os.environ.get("SECURE_SSL_HOST", ""), ALLOWED_HOSTS
)
CLINIC_PROCESS_ROLE = "clinic_app"
DATABASES = {
    "default": parse_database_url(
        os.environ.get("APP_DATABASE_URL", ""), required_role=CLINIC_PROCESS_ROLE
    )
}
DATABASES.update(
    agent_database_config(os.environ, primary=DATABASES["default"], strict_tls=True)
)
# Protected fields decrypt only through the managed-secret boundary; a
# production deployment without a configured backend must fail at startup,
# not at the first clinical read.
BACKEND_REQUIRED_MESSAGE = "CLINIC_SECRET_BACKEND is required for production"
if base.CLINIC_SECRET_BACKEND is None:
    raise ImproperlyConfigured(BACKEND_REQUIRED_MESSAGE)
DEBUG = False
SECURE_SSL_REDIRECT = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = 31_536_000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_PROXY_SSL_HEADER = None

from __future__ import annotations

import os

from . import base
from .contracts import (
    parse_production_hosts,
    validate_runtime_secret,
    validate_secure_ssl_host,
)
from .database import parse_database_url

base.export_settings(globals())
globals().pop("WSGI_APPLICATION", None)

SECRET_KEY = validate_runtime_secret(os.environ.get("SECRET_KEY", ""))
SECRET_KEY_CONFIGURED = True
ALLOWED_HOSTS = parse_production_hosts(os.environ.get("ALLOWED_HOSTS", ""))
SECURE_SSL_HOST = validate_secure_ssl_host(
    os.environ.get("SECURE_SSL_HOST", ""), ALLOWED_HOSTS
)
CLINIC_PROCESS_ROLE = "clinic_owner"
DATABASES = {
    "default": parse_database_url(
        os.environ.get("MIGRATION_DATABASE_URL", ""),
        required_role=CLINIC_PROCESS_ROLE,
    )
}
DEBUG = False
SECURE_PROXY_SSL_HEADER = None

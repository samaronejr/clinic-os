from __future__ import annotations

import os
from pathlib import Path

from . import base
from .browser_contract import (
    BROWSER_DATABASE_HOST,
    BROWSER_HOST,
    validate_browser_environment,
)
from .browser_rpc import receive_reserved_attestation
from .database import parse_database_url

_environment = dict(os.environ)
_environment_contract = validate_browser_environment(_environment)
_attestation = receive_reserved_attestation(
    Path(_environment["CLINIC_LEDGER_RPC_SOCKET"]),
    attempt_id=_environment["CLINIC_ATTEMPT_ID"],
    claim_id=_environment["CLINIC_PROCESS_CLAIM_ID"],
)

base.export_settings(globals())

SECRET_KEY = _environment["SECRET_KEY"]
SECRET_KEY_CONFIGURED = True
ALLOWED_HOSTS = [BROWSER_HOST]
CLINIC_DATA_MODE = _environment["CLINIC_DATA_MODE"]
CLINIC_PROCESS_ROLE = "clinic_app"
CLINIC_ATTEMPT_ID = _environment["CLINIC_ATTEMPT_ID"]
CLINIC_LEDGER_RPC_SOCKET = _environment["CLINIC_LEDGER_RPC_SOCKET"]
CLINIC_PROCESS_CLAIM_ID = _environment["CLINIC_PROCESS_CLAIM_ID"]
COMPOSE_PROJECT_NAME = _environment["COMPOSE_PROJECT_NAME"]
DATABASES = {
    "default": parse_database_url(
        _environment["APP_DATABASE_URL"],
        required_role=CLINIC_PROCESS_ROLE,
        required_host=BROWSER_DATABASE_HOST,
        required_hostaddr="127.0.0.1",
        database_prefix=f"{_environment['COMPOSE_PROJECT_NAME']}_",
    )
}
DEBUG = False
SECURE_SSL_HOST = ""
SECURE_SSL_REDIRECT = False
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
SECURE_PROXY_SSL_HEADER = None

from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from typing import TYPE_CHECKING, Final, Never

from django.core.exceptions import ImproperlyConfigured

from .contracts import SECRET_PREFIXES, require_synthetic_mode
from .database import parse_database_url

if TYPE_CHECKING:
    from ops.testing.isolation_common import JsonObject, JsonValue

BROWSER_HOST: Final = "phase1a-browser.qa.clinic-os.test"
BROWSER_DATABASE_HOST: Final = "phase1a-db.qa.clinic-os.dev"
BROWSER_SETTINGS_MODULE: Final = "config.settings.browser"
PROJECT_PATTERN: Final = re.compile(r"clinic_phase1a_[a-z0-9_]+")
UUID_PATTERN: Final = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)
ENVIRONMENT_KEYS: Final = frozenset(
    {
        "ALLOWED_HOSTS",
        "APP_DATABASE_URL",
        "CLINIC_ATTEMPT_ID",
        "CLINIC_DATA_MODE",
        "CLINIC_LEDGER_RPC_SOCKET",
        "CLINIC_PROCESS_CLAIM_ID",
        "COMPOSE_PROJECT_NAME",
        "DJANGO_SETTINGS_MODULE",
        "HOME",
        "LANG",
        "PYTHONTZPATH",
        "SECRET_KEY",
        "TMPDIR",
    }
)
ABSENT_KEYS: Final = ("FORWARDED_ALLOW_IPS", "GUNICORN_CMD_ARGS")
SECRET_KEYS: Final = ("APP_DATABASE_URL", "SECRET_KEY")
FORBIDDEN_BROWSER_SECRETS: Final = frozenset(
    {
        "browser-secret",
        "development-only-secret-key",
        "placeholder",
        "synthetic",
    }
)
MIN_BROWSER_SECRET_LENGTH: Final = 32
MAX_BROWSER_SECRET_LENGTH: Final = 128
PRINTABLE_ASCII_MIN: Final = 32
PRINTABLE_ASCII_MAX: Final = 126
PRIVATE_SOCKET_MODE: Final = 0o600


def validate_browser_environment(environment: dict[str, str]) -> JsonObject:
    if set(environment) != ENVIRONMENT_KEYS or not all(
        isinstance(value, str) for value in environment.values()
    ):
        _fail()
    project = environment["COMPOSE_PROJECT_NAME"]
    if (
        environment["ALLOWED_HOSTS"] != BROWSER_HOST
        or environment["DJANGO_SETTINGS_MODULE"] != BROWSER_SETTINGS_MODULE
        or environment["LANG"] != "C.UTF-8"
        or environment["PYTHONTZPATH"] != ""
        or PROJECT_PATTERN.fullmatch(project) is None
        or project == "clinic_project"
    ):
        _fail()
    try:
        require_synthetic_mode(environment["CLINIC_DATA_MODE"])
        _canonical_uuid(environment["CLINIC_ATTEMPT_ID"])
        _canonical_uuid(environment["CLINIC_PROCESS_CLAIM_ID"])
        _validate_browser_secret(environment["SECRET_KEY"])
        _validate_directory(environment["HOME"])
        _validate_directory(environment["TMPDIR"])
        _validate_rpc_socket(environment["CLINIC_LEDGER_RPC_SOCKET"])
        parse_database_url(
            environment["APP_DATABASE_URL"],
            required_role="clinic_app",
            required_host=BROWSER_DATABASE_HOST,
            required_hostaddr="127.0.0.1",
            database_prefix=f"{project}_",
        )
    except (ImproperlyConfigured, OSError, ValueError):
        _fail()
    literal: list[JsonValue] = [
        {"name": name, "value": environment[name]}
        for name in sorted(ENVIRONMENT_KEYS - set(SECRET_KEYS))
    ]
    return {
        "absent_keys": list(ABSENT_KEYS),
        "literal": literal,
        "secret_keys": list(SECRET_KEYS),
    }


def _validate_browser_secret(value: str) -> None:
    if not MIN_BROWSER_SECRET_LENGTH <= len(
        value
    ) <= MAX_BROWSER_SECRET_LENGTH or not all(
        PRINTABLE_ASCII_MIN <= ord(char) <= PRINTABLE_ASCII_MAX for char in value
    ):
        _fail()
    folded = value.casefold()
    if folded in FORBIDDEN_BROWSER_SECRETS or any(
        folded.startswith(prefix) for prefix in SECRET_PREFIXES
    ):
        _fail()


def _validate_directory(value: str) -> None:
    path = Path(value)
    if not path.is_absolute() or path.is_symlink() or path.resolve(strict=True) != path:
        _fail()
    identity = path.stat(follow_symlinks=False)
    if not stat.S_ISDIR(identity.st_mode):
        _fail()


def _validate_rpc_socket(value: str) -> None:
    path = Path(value)
    if not path.is_absolute() or path.is_symlink() or path.resolve(strict=True) != path:
        _fail()
    identity = path.stat(follow_symlinks=False)
    if (
        not stat.S_ISSOCK(identity.st_mode)
        or stat.S_IMODE(identity.st_mode) != PRIVATE_SOCKET_MODE
        or identity.st_uid != os.geteuid()
        or identity.st_gid != os.getegid()
    ):
        _fail()


def _canonical_uuid(value: str) -> str:
    if UUID_PATTERN.fullmatch(value) is None:
        _fail()
    return value


def _fail() -> Never:
    message = "browser settings violate the closed environment contract"
    raise ImproperlyConfigured(message)

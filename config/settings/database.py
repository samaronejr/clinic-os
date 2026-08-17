from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Final, Never
from urllib.parse import parse_qsl, unquote, urlsplit

from django.core.exceptions import ImproperlyConfigured

BASE_QUERY_KEYS: Final = frozenset({"connect_timeout", "sslmode", "sslrootcert"})
MAX_CONNECT_TIMEOUT: Final = 3


def parse_database_url(
    value: str,
    *,
    required_role: str,
    required_host: str | None = None,
    required_hostaddr: str | None = None,
    database_prefix: str | None = None,
) -> dict[str, object]:
    try:
        parsed = urlsplit(value)
        port = parsed.port
        query_items = parse_qsl(
            parsed.query, keep_blank_values=True, strict_parsing=True
        )
    except ValueError:
        _fail()
    query = _query(query_items, required_hostaddr)
    role = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    host = parsed.hostname or ""
    database = parsed.path.removeprefix("/")
    if (
        parsed.scheme != "postgresql"
        or role != required_role
        or not password
        or not host
        or parsed.fragment
        or not database
        or "/" in database
        or port is None
    ):
        _fail()
    if required_host is not None and host != required_host:
        _fail()
    if database_prefix is not None and not database.startswith(database_prefix):
        _fail()
    ca = _canonical_ca(query["sslrootcert"])
    timeout_text = query["connect_timeout"]
    if (
        query["sslmode"] != "verify-full"
        or not timeout_text.isdecimal()
        or str(int(timeout_text)) != timeout_text
        or not 1 <= int(timeout_text) <= MAX_CONNECT_TIMEOUT
    ):
        _fail()
    options: dict[str, object] = {
        "connect_timeout": int(timeout_text),
        "options": "-c search_path=clinic_app,public",
        "sslmode": "verify-full",
        "sslrootcert": str(ca),
    }
    if required_hostaddr is not None:
        options["hostaddr"] = required_hostaddr
    return {
        "ATOMIC_REQUESTS": False,
        "ENGINE": "django.db.backends.postgresql",
        "HOST": host,
        "NAME": database,
        "OPTIONS": options,
        "PASSWORD": password,
        "PORT": str(port),
        "USER": role,
    }


def _query(
    items: list[tuple[str, str]], required_hostaddr: str | None
) -> dict[str, str]:
    expected = set(BASE_QUERY_KEYS)
    if required_hostaddr is not None:
        expected.add("hostaddr")
    if len(items) != len(expected) or {name for name, _ in items} != expected:
        _fail()
    query = dict(items)
    if required_hostaddr is not None and query.get("hostaddr") != required_hostaddr:
        _fail()
    return query


def _canonical_ca(value: str) -> Path:
    path = Path(value)
    try:
        identity = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError:
        _fail()
    if (
        not path.is_absolute()
        or path.is_symlink()
        or resolved != path
        or not stat.S_ISREG(identity.st_mode)
        or identity.st_uid != os.geteuid()
        or identity.st_gid != os.getegid()
        or not os.access(path, os.R_OK, effective_ids=True)
    ):
        _fail()
    return path


def _fail() -> Never:
    message = "database settings violate the fail-closed database contract"
    raise ImproperlyConfigured(message)

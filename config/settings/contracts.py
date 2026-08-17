from __future__ import annotations

import ipaddress
import math
import re
from collections import Counter
from typing import Final

from django.core.exceptions import ImproperlyConfigured

SYNTHETIC_DATA_MODE: Final = "synthetic"
SECRET_PREFIXES: Final = (
    "development",
    "test",
    "changeme",
    "secret",
    "insecure",
    "development-only-secret-key",
)
RESERVED_DNS_SUFFIXES: Final = (
    "localhost",
    "local",
    "internal",
    "test",
    "invalid",
    "example",
    "example.com",
    "example.net",
    "example.org",
)
LDH_LABEL: Final = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
MAX_SECRET_LENGTH: Final = 128
MIN_SECRET_DISTINCT_CHARACTERS: Final = 20
MIN_SECRET_ENTROPY: Final = 4.0
MAX_PORT: Final = 65_535
MAX_DNS_NAME_BYTES: Final = 253
MIN_DNS_LABELS: Final = 2
PRINTABLE_ASCII_MIN: Final = 32
PRINTABLE_ASCII_MAX: Final = 126


def require_synthetic_mode(value: str) -> str:
    if value != SYNTHETIC_DATA_MODE:
        message = "synthetic data mode is required"
        raise ImproperlyConfigured(message)
    return value


def validate_runtime_secret(value: str, *, minimum_length: int = 64) -> str:
    if not minimum_length <= len(value) <= MAX_SECRET_LENGTH or not _printable_ascii(
        value
    ):
        _secret_failure()
    folded = value.casefold()
    if any(folded.startswith(prefix) for prefix in SECRET_PREFIXES):
        _secret_failure()
    classes = (
        any(character.islower() for character in value),
        any(character.isupper() for character in value),
        any(character.isdigit() for character in value),
        any(not character.isalnum() for character in value),
    )
    if (
        not all(classes)
        or len(set(value)) < MIN_SECRET_DISTINCT_CHARACTERS
        or _entropy(value) < MIN_SECRET_ENTROPY
    ):
        _secret_failure()
    return value


def parse_production_hosts(value: str) -> list[str]:
    if not value or any(character.isspace() for character in value):
        _host_failure()
    try:
        value.encode("ascii")
    except UnicodeEncodeError:
        _host_failure()
    hosts = value.split(",")
    if hosts != list(dict.fromkeys(hosts)):
        _host_failure()
    for host in hosts:
        _validate_production_host(host)
    return hosts


def validate_secure_ssl_host(value: str, allowed_hosts: list[str]) -> str:
    if not value:
        return value
    if value.count(":") > 1:
        _ssl_host_failure()
    host, separator, port = value.rpartition(":")
    if not separator:
        host = value
    elif (
        not port.isdecimal() or str(int(port)) != port or not 1 <= int(port) <= MAX_PORT
    ):
        _ssl_host_failure()
    try:
        parsed = parse_production_hosts(host)
    except ImproperlyConfigured:
        _ssl_host_failure()
    if parsed != [host] or host not in allowed_hosts:
        _ssl_host_failure()
    return value


def _validate_production_host(host: str) -> None:
    if host != host.lower() or not 1 <= len(host.encode("ascii")) <= MAX_DNS_NAME_BYTES:
        _host_failure()
    labels = host.split(".")
    if len(labels) < MIN_DNS_LABELS or any(
        LDH_LABEL.fullmatch(label) is None for label in labels
    ):
        _host_failure()
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        _host_failure()
    if any(
        host == suffix or host.endswith(f".{suffix}")
        for suffix in RESERVED_DNS_SUFFIXES
    ):
        _host_failure()


def _entropy(value: str) -> float:
    length = len(value)
    return -sum(
        count / length * math.log2(count / length) for count in Counter(value).values()
    )


def _printable_ascii(value: str) -> bool:
    return all(
        PRINTABLE_ASCII_MIN <= ord(character) <= PRINTABLE_ASCII_MAX
        for character in value
    )


def _secret_failure() -> None:
    message = "SECRET_KEY violates the runtime secret contract"
    raise ImproperlyConfigured(message)


def _host_failure() -> None:
    message = "ALLOWED_HOSTS violates the production host contract"
    raise ImproperlyConfigured(message)


def _ssl_host_failure() -> None:
    message = "SECURE_SSL_HOST violates the SSL host contract"
    raise ImproperlyConfigured(message)

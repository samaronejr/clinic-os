from __future__ import annotations

import ipaddress
import math
import os
import re
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Final

import environ
from django.core.exceptions import ImproperlyConfigured

if TYPE_CHECKING:
    from collections.abc import Mapping

SYNTHETIC_DATA_MODE: Final = "synthetic"
LIVE_DATA_MODE: Final = "live"
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

# The attachment root ``env("EHR_ATTACHMENT_ROOT")`` falls back to when the
# variable is absent; the synthetic isolation check must derive the same
# effective root, so the default lives here next to the contract.
DEFAULT_EHR_ATTACHMENT_ROOT: Final = (
    Path(__file__).resolve().parents[2] / "var" / "ehr-attachments"
)


def resolve_attachment_root(environment: Mapping[str, str]) -> str:
    """Return the root ``env("EHR_ATTACHMENT_ROOT")`` derives.

    Settings modules resolve ``EHR_ATTACHMENT_ROOT`` through
    ``environ.Env.get_value``, which resolves ``$VARIABLE`` proxy
    indirection recursively — a missing proxy target falls back to the
    default — before the value is used. The startup isolation check must
    evaluate that same effective root, so it resolves through the
    identical ``env`` call against the supplied environment rather than
    re-reading the raw variable. A proxy cycle raises ``RecursionError``;
    callers fail closed on it.
    """
    resolver = environ.Env()
    resolver.ENVIRON = environment
    value: str = resolver(
        "EHR_ATTACHMENT_ROOT", default=str(DEFAULT_EHR_ATTACHMENT_ROOT)
    )
    return value


def require_synthetic_mode(value: str) -> str:
    if value != SYNTHETIC_DATA_MODE:
        message = "synthetic data mode is required"
        raise ImproperlyConfigured(message)
    return value


def require_data_mode(value: str, environment: Mapping[str, str] | None = None) -> str:
    """Resolve the process data mode; live requires an approved activation.

    ``synthetic`` is always allowed and stays the default, but it can never
    reuse storage bound to a live activation: the startup gate refuses a
    configured database endpoint claimed in the live registry or an
    attachment root carrying a live ownership marker — independently of
    whether the caller still supplies the activation-state pointer — and
    also refuses storage that a supplied activation record binds. ``live``
    is allowed only when
    ``ops.release.activation.require_live_activation`` confirms a valid,
    active, non-rehearsal activation record bound to this release,
    environment, evidence and storage; anything else fails closed with
    ``ImproperlyConfigured`` — there is no silent fallback to synthetic.
    """
    if value == SYNTHETIC_DATA_MODE:
        # Lazy import: ops.release.activation imports this module, so the
        # isolation check is loaded only when a mode is resolved.
        from ops.release.activation import (  # noqa: PLC0415
            synthetic_isolation_findings,
        )

        findings = synthetic_isolation_findings(
            os.environ if environment is None else environment
        )
        if findings:
            message = "synthetic data mode is not isolated: " + "; ".join(findings)
            raise ImproperlyConfigured(message)
        return value
    if value == LIVE_DATA_MODE:
        # Lazy import: ops.release.activation imports this module, so the
        # activation contract is loaded only when live mode is requested.
        from ops.release.activation import (  # noqa: PLC0415
            require_live_activation,
        )

        return require_live_activation(
            os.environ if environment is None else environment
        )
    message = "CLINIC_DATA_MODE must be 'synthetic' or an approved 'live'"
    raise ImproperlyConfigured(message)


def resolved_data_mode() -> str | None:
    """Return the process data mode, or None when unset or invalid.

    ``require_data_mode`` validates ``CLINIC_DATA_MODE`` at startup, so a
    missing or unknown value only appears when the setting is absent at
    call time (for example when a test deletes it). Callers at gate
    boundaries must fail closed on ``None`` rather than let Django's
    ``AttributeError`` escape as a denial signal.
    """
    from django.conf import settings  # noqa: PLC0415

    value: str | None = getattr(settings, "CLINIC_DATA_MODE", None)
    if value not in (SYNTHETIC_DATA_MODE, LIVE_DATA_MODE):
        return None
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


SECRET_BACKENDS: Final = frozenset({"synthetic-file"})


def validate_secret_store_env(
    backend: str | None, directory: str | None
) -> tuple[str | None, str | None]:
    """Bind the managed-secret backend pair or fail closed.

    ``CLINIC_SECRET_BACKEND`` selects the only approved secret source; there
    is no default and no plaintext fallback. ``synthetic-file`` requires
    ``CLINIC_SECRET_DIR``; a directory without a backend is rejected so a
    stray path can never masquerade as configuration.
    """
    if backend is None:
        if directory is not None:
            message = "CLINIC_SECRET_DIR requires CLINIC_SECRET_BACKEND"
            raise ImproperlyConfigured(message)
        return None, None
    if backend not in SECRET_BACKENDS:
        message = "CLINIC_SECRET_BACKEND must be one of: " + ", ".join(
            sorted(SECRET_BACKENDS)
        )
        raise ImproperlyConfigured(message)
    if directory is None or not directory:
        message = "CLINIC_SECRET_DIR is required for CLINIC_SECRET_BACKEND"
        raise ImproperlyConfigured(message)
    return backend, directory


def _secret_failure() -> None:
    message = "SECRET_KEY violates the runtime secret contract"
    raise ImproperlyConfigured(message)


def _host_failure() -> None:
    message = "ALLOWED_HOSTS violates the production host contract"
    raise ImproperlyConfigured(message)


def _ssl_host_failure() -> None:
    message = "SECURE_SSL_HOST violates the SSL host contract"
    raise ImproperlyConfigured(message)

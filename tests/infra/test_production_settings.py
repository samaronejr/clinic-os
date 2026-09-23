from __future__ import annotations

import importlib
import os
import subprocess
import sys
from typing import TYPE_CHECKING, Protocol, runtime_checkable
from urllib.parse import urlencode

import pytest
from django.core.exceptions import ImproperlyConfigured

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


def _valid_runtime_token() -> str:
    return "Aa0!Bb1@Cc2#Dd3$Ee4%Ff5^Gg6&Hh7*Ii8(Jj9)Kk0_Ll1+Mm2=Nn3?Oo4Pp5[Q"


VALID_RUNTIME_TOKEN = _valid_runtime_token()
VALID_HOSTS = "app.qa.clinic-os.dev,staff.qa.clinic-os.dev"
_run_process = subprocess.run


@runtime_checkable
class _SecretValidator(Protocol):
    def __call__(self, value: str, *, minimum_length: int = 64) -> str: ...


@runtime_checkable
class _HostParser(Protocol):
    def __call__(self, value: str) -> list[str]: ...


@runtime_checkable
class _SslHostValidator(Protocol):
    def __call__(self, value: str, allowed_hosts: list[str]) -> str: ...


def _contract(name: str, contract: type[object]) -> Callable[..., object]:
    module = importlib.import_module("config.settings.contracts")
    candidate = getattr(module, name, None)
    assert isinstance(candidate, contract)
    assert callable(candidate)
    return candidate


@pytest.mark.parametrize(
    "value",
    [
        VALID_RUNTIME_TOKEN[:-1],
        VALID_RUNTIME_TOKEN * 2 + "A",
        "Aa0!" * 16,
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789!@#$%^&*()_+-=[]{}|;:,.<>?/AB",
        "abcdefghijklmnopqrstuvwxyz0123456789!@#$%^&*()_+-=[]{}|;:,.<>?/ab",
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz!@#$%^&*()_+-=AB",
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789AB",
        f"development{VALID_RUNTIME_TOKEN}",
        f"test{VALID_RUNTIME_TOKEN}",
        f"changeme{VALID_RUNTIME_TOKEN}",
        f"secret{VALID_RUNTIME_TOKEN}",
        f"insecure{VALID_RUNTIME_TOKEN}",
        f"development-only-secret-key{VALID_RUNTIME_TOKEN}",
        VALID_RUNTIME_TOKEN[:-1] + "\n",
        VALID_RUNTIME_TOKEN[:-1] + "é",
    ],
    ids=[
        "length-63",
        "length-129",
        "low-entropy-and-distinct",
        "missing-lower",
        "missing-upper",
        "missing-digit",
        "missing-punctuation",
        "development-prefix",
        "test-prefix",
        "changeme-prefix",
        "secret-prefix",
        "insecure-prefix",
        "foundation-prefix",
        "non-printable",
        "non-ascii",
    ],
)
def test_runtime_secret_has_exact_strength_boundaries(value: str) -> None:
    validator = _contract("validate_runtime_secret", _SecretValidator)
    assert validator(VALID_RUNTIME_TOKEN) == VALID_RUNTIME_TOKEN
    with pytest.raises(ImproperlyConfigured, match="secret contract"):
        validator(value)


@pytest.mark.parametrize(
    "value",
    [
        "",
        " app.qa.clinic-os.dev",
        "app.qa.clinic-os.dev ",
        "app.qa.clinic-os.dev, staff.qa.clinic-os.dev",
        "app..clinic-os.dev",
        "app_qa.clinic-os.dev",
        "-app.qa.clinic-os.dev",
        "app-.qa.clinic-os.dev",
        "APP.qa.clinic-os.dev",
        "app.qa.clínic-os.dev",
        "app.qa.clinic-os.dev,app.qa.clinic-os.dev",
        "*.qa.clinic-os.dev",
        "app.qa.clinic-os.dev.",
        "127.0.0.1",
        "::1",
        "localhost",
        "app.local",
        "app.internal",
        "app.test",
        "app.invalid",
        "app.example",
        "app.example.com",
        "example.net",
        "example.org",
        "singlelabel",
        f"{'a' * 64}.qa.clinic-os.dev",
        ".".join(("a" * 63, "b" * 63, "c" * 63, "d" * 62)),
    ],
    ids=[
        "empty",
        "leading-space",
        "trailing-space",
        "interior-space",
        "empty-label",
        "underscore",
        "leading-hyphen",
        "trailing-hyphen",
        "uppercase",
        "non-ascii-idna",
        "duplicate",
        "wildcard",
        "trailing-dot",
        "ipv4",
        "ipv6",
        "localhost",
        "local",
        "internal",
        "test",
        "invalid",
        "example",
        "example-com",
        "example-net",
        "example-org",
        "single-label",
        "label-64",
        "total-254",
    ],
)
def test_production_hosts_are_normalized_ascii_ldh(value: str) -> None:
    parser = _contract("parse_production_hosts", _HostParser)
    assert parser(VALID_HOSTS) == [
        "app.qa.clinic-os.dev",
        "staff.qa.clinic-os.dev",
    ]
    with pytest.raises(ImproperlyConfigured, match="host contract"):
        parser(value)


@pytest.mark.parametrize(
    "value",
    [
        "outside.qa.clinic-os.dev",
        "app.qa.clinic-os.dev:0",
        "app.qa.clinic-os.dev:65536",
        "app.qa.clinic-os.dev:0443",
        "app.qa.clinic-os.dev:not-a-port",
        " APP.qa.clinic-os.dev",
    ],
)
def test_ssl_redirect_host_is_empty_or_an_exact_allowed_host(value: str) -> None:
    validator = _contract("validate_secure_ssl_host", _SslHostValidator)
    allowed = ["app.qa.clinic-os.dev", "staff.qa.clinic-os.dev"]
    assert validator("", allowed) == ""
    assert validator("app.qa.clinic-os.dev", allowed) == "app.qa.clinic-os.dev"
    with_port = validator("app.qa.clinic-os.dev:443", allowed)
    assert isinstance(with_port, str)
    assert with_port.endswith(":443")
    with pytest.raises(ImproperlyConfigured, match="SSL host contract"):
        validator(value, allowed)


def test_production_direct_process_trusts_no_forwarded_header(tmp_path: Path) -> None:
    ca = tmp_path / "db-ca.pem"
    ca.write_text("synthetic CA fixture\n", encoding="ascii")
    ca.chmod(0o444)
    query = urlencode(
        {
            "connect_timeout": "2",
            "sslmode": "verify-full",
            "sslrootcert": str(ca),
        }
    )
    environment = os.environ.copy()
    environment.update(
        {
            "ALLOWED_HOSTS": VALID_HOSTS,
            "APP_DATABASE_URL": (
                "postgresql://clinic_app:synthetic@db.qa.clinic-os.dev:5432/"
                f"clinic?{query}"
            ),
            "CLINIC_DATA_MODE": "synthetic",
            "DJANGO_SETTINGS_MODULE": "config.settings.prod",
            "SECRET_KEY": VALID_RUNTIME_TOKEN,
            "SECURE_SSL_HOST": "app.qa.clinic-os.dev",
        }
    )
    result = _run_process(
        (
            sys.executable,
            "-c",
            "import config.settings.prod as s; print(s.SECURE_PROXY_SSL_HEADER)",
        ),
        check=False,
        capture_output=True,
        env=environment,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "None\n"

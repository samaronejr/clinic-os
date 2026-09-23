from __future__ import annotations

import importlib
import os
import subprocess
import sys
from typing import TYPE_CHECKING, NotRequired, Protocol, TypedDict, runtime_checkable
from urllib.parse import urlencode

import pytest
from django.core.exceptions import ImproperlyConfigured

from infra.test_production_settings import VALID_HOSTS, VALID_RUNTIME_TOKEN

if TYPE_CHECKING:
    from pathlib import Path

_run_process = subprocess.run


class _UrlChanges(TypedDict):
    scheme: NotRequired[str]
    role: NotRequired[str]
    sslmode: NotRequired[str]
    timeout: NotRequired[str]
    root: NotRequired[str | None]
    extra: NotRequired[tuple[tuple[str, str], ...]]


@runtime_checkable
class _DatabaseParser(Protocol):
    def __call__(
        self,
        value: str,
        *,
        required_role: str,
        required_host: str | None = None,
        required_hostaddr: str | None = None,
        database_prefix: str | None = None,
    ) -> dict[str, object]: ...


def _parser() -> _DatabaseParser:
    module = importlib.import_module("config.settings.database")
    candidate = getattr(module, "parse_database_url", None)
    assert isinstance(candidate, _DatabaseParser)
    return candidate


def _ca(tmp_path: Path) -> Path:
    path = tmp_path / "db-ca.pem"
    path.write_text("synthetic CA fixture\n", encoding="ascii")
    path.chmod(0o444)
    return path


def _url(
    ca: Path,
    changes: _UrlChanges | None = None,
) -> str:
    options = changes or {}
    scheme = options.get("scheme", "postgresql")
    role = options.get("role", "clinic_app")
    sslmode = options.get("sslmode", "verify-full")
    timeout = options.get("timeout", "2")
    root = options.get("root")
    extra = options.get("extra", ())
    query = [
        ("sslmode", sslmode),
        ("sslrootcert", str(ca) if root is None else root),
        ("connect_timeout", timeout),
        *extra,
    ]
    return (
        f"{scheme}://{role}:synthetic@db.qa.clinic-os.dev:5432/clinic?"
        f"{urlencode(query)}"
    )


@pytest.mark.parametrize(
    ("case", "changes"),
    [
        ("sqlite", {"scheme": "sqlite"}),
        ("owner-role", {"role": "clinic_owner"}),
        ("plain", {"sslmode": "disable"}),
        ("require", {"sslmode": "require"}),
        ("verify-ca", {"sslmode": "verify-ca"}),
        ("timeout-zero", {"timeout": "0"}),
        ("timeout-four", {"timeout": "4"}),
        ("timeout-text", {"timeout": "slow"}),
        ("relative-ca", {"root": "relative-ca.pem"}),
        ("unknown-option", {"extra": (("application_name", "unsafe"),)}),
        ("duplicate-option", {"extra": (("sslmode", "verify-full"),)}),
    ],
)
def test_runtime_database_rejects_unsafe_role_tls_and_timeout(
    tmp_path: Path,
    case: str,
    changes: _UrlChanges,
) -> None:
    del case
    parser = _parser()
    ca = _ca(tmp_path)
    assert parser(_url(ca), required_role="clinic_app")["USER"] == "clinic_app"
    with pytest.raises(ImproperlyConfigured, match="database contract"):
        parser(_url(ca, changes), required_role="clinic_app")


def test_runtime_database_requires_a_readable_canonical_ca(tmp_path: Path) -> None:
    parser = _parser()
    ca = _ca(tmp_path)
    missing = tmp_path / "missing.pem"
    with pytest.raises(ImproperlyConfigured, match="database contract"):
        parser(_url(ca, {"root": str(missing)}), required_role="clinic_app")

    target = tmp_path / "target.pem"
    target.write_text("synthetic CA fixture\n", encoding="ascii")
    link = tmp_path / "linked.pem"
    link.symlink_to(target)
    with pytest.raises(ImproperlyConfigured, match="database contract"):
        parser(_url(ca, {"root": str(link)}), required_role="clinic_app")


def test_production_database_has_one_app_role_alias(tmp_path: Path) -> None:
    ca = _ca(tmp_path)
    database_url = _url(ca)
    environment = os.environ.copy()
    environment.update(
        {
            "ALLOWED_HOSTS": VALID_HOSTS,
            "APP_DATABASE_URL": database_url,
            "CLINIC_DATA_MODE": "synthetic",
            "DJANGO_SETTINGS_MODULE": "config.settings.prod",
            "SECRET_KEY": VALID_RUNTIME_TOKEN,
            "SECURE_SSL_HOST": "app.qa.clinic-os.dev",
        }
    )
    code = (
        "import config.settings.prod as s; "
        "d=s.DATABASES; assert list(d)==['default']; "
        "x=d['default']; assert x['USER']=='clinic_app'; "
        "assert x['OPTIONS']['sslmode']=='verify-full'; "
        "assert x['OPTIONS']['connect_timeout']==2"
    )
    result = _run_process(
        (sys.executable, "-c", code),
        check=False,
        capture_output=True,
        env=environment,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    wrong_url = _url(ca, {"role": "clinic_owner"})
    environment["APP_DATABASE_URL"] = wrong_url
    rejected = _run_process(
        (sys.executable, "-c", "import config.settings.prod"),
        check=False,
        capture_output=True,
        env=environment,
        text=True,
    )
    assert rejected.returncode != 0
    assert "database contract" in rejected.stderr
    assert wrong_url not in rejected.stderr
    assert "synthetic" not in rejected.stderr

from __future__ import annotations

import os
import subprocess
import sys
from typing import TYPE_CHECKING
from urllib.parse import urlencode

import pytest

from infra.test_production_settings import VALID_HOSTS, VALID_RUNTIME_TOKEN

if TYPE_CHECKING:
    from pathlib import Path

_run_process = subprocess.run


def _release_environment(tmp_path: Path, role: str = "clinic_owner") -> dict[str, str]:
    ca = tmp_path / "release-ca.pem"
    ca.write_text("synthetic release CA fixture\n", encoding="ascii")
    ca.chmod(0o444)
    query = urlencode(
        {
            "connect_timeout": "3",
            "sslmode": "verify-full",
            "sslrootcert": str(ca),
        }
    )
    environment = os.environ.copy()
    environment.update(
        {
            "ALLOWED_HOSTS": VALID_HOSTS,
            "CLINIC_DATA_MODE": "synthetic",
            "DJANGO_SETTINGS_MODULE": "config.settings.release",
            "MIGRATION_DATABASE_URL": (
                f"postgresql://{role}:synthetic@db.qa.clinic-os.dev:5432/clinic?{query}"
            ),
            "SECRET_KEY": VALID_RUNTIME_TOKEN,
            "SECURE_SSL_HOST": "app.qa.clinic-os.dev",
        }
    )
    environment.pop("APP_DATABASE_URL", None)
    return environment


def test_release_is_owner_only_command_settings_not_wsgi(tmp_path: Path) -> None:
    environment = _release_environment(tmp_path)
    code = (
        "import config.settings.release as s; "
        "assert s.CLINIC_PROCESS_ROLE=='clinic_owner'; "
        "assert list(s.DATABASES)==['default']; "
        "assert s.DATABASES['default']['USER']=='clinic_owner'; "
        "assert s.SECURE_PROXY_SSL_HEADER is None; "
        "assert s.DEBUG is False; "
        "assert not hasattr(s, 'WSGI_APPLICATION')"
    )
    result = _run_process(
        (sys.executable, "-c", code),
        check=False,
        capture_output=True,
        env=environment,
        text=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("role", ["clinic_app", "postgres", "clinic_super"])
def test_release_rejects_every_non_owner_database_role(
    tmp_path: Path, role: str
) -> None:
    environment = _release_environment(tmp_path, role)
    database_url = environment["MIGRATION_DATABASE_URL"]
    result = _run_process(
        (sys.executable, "-c", "import config.settings.release"),
        check=False,
        capture_output=True,
        env=environment,
        text=True,
    )
    assert result.returncode != 0
    assert "database contract" in result.stderr
    assert database_url not in result.stderr
    assert "synthetic" not in result.stderr

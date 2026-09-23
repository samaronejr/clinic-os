from __future__ import annotations

import subprocess
import sys
from typing import TYPE_CHECKING
from urllib.parse import urlencode

import pytest

from test_production_settings import VALID_HOSTS, VALID_RUNTIME_TOKEN

if TYPE_CHECKING:
    from pathlib import Path

_run_process = subprocess.run


@pytest.mark.parametrize("transport", [False, True], ids=["bare", "tls"])
@pytest.mark.parametrize("supplied_search", [False, True], ids=["default", "override"])
def test_base_database_preserves_transport_when_building_backend_parameters(
    tmp_path: Path, *, transport: bool, supplied_search: bool
) -> None:
    # Given: an isolated URL with optional TLS transport and a hostile schema option.
    ca = tmp_path / "private-ca.pem"
    _ = ca.write_text("synthetic CA fixture\n", encoding="ascii")
    ca.chmod(0o600)
    query = (
        {
            "hostaddr": "127.0.0.1",
            "sslmode": "verify-full",
            "sslrootcert": str(ca),
            "connect_timeout": "2",
        }
        if transport
        else {}
    )
    if supplied_search:
        query["options"] = "-c search_path=untrusted,public -c role=clinic_owner"
    environment = {
        "DJANGO_SETTINGS_MODULE": "config.settings.base",
        "APP_DATABASE_URL": (
            "postgresql://clinic_app:synthetic@db.qa.clinic-os.dev:5432/clinic?"
            + urlencode(query)
        ),
    }
    code = """
import sys
from django.db import connections

database = connections['default']
params = database.get_connection_params()
assert params['host'] == 'db.qa.clinic-os.dev'
assert params['user'] == 'clinic_app'
assert params['dbname'] == 'clinic'
assert database.settings_dict['ATOMIC_REQUESTS'] is False
assert params['options'] == '-c search_path=clinic_app,public'
if sys.argv[1] == 'True':
    assert params.get('hostaddr') == '127.0.0.1', 'hostaddr lost'
    assert params.get('sslmode') == 'verify-full', 'sslmode lost'
    assert params.get('sslrootcert') == sys.argv[2], 'sslrootcert lost'
    assert params.get('connect_timeout') == 2, 'connect_timeout lost'
else:
    assert not {'hostaddr', 'sslmode', 'sslrootcert', 'connect_timeout'} & params.keys()
print('backend_parameters_verified')
"""
    # When: real settings and Django's backend construct parameters in a fresh process.
    result = _run_process(
        [sys.executable, "-c", code, str(transport), str(ca)],
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    # Then: transport survives, while the application schema and role remain fixed.
    assert result.returncode == 0, result.stderr
    assert result.stdout == "backend_parameters_verified\n"


@pytest.mark.parametrize(
    "query",
    [
        "sslmode=disable&connect_timeout=2",
        "sslmode=verify-full&connect_timeout=slow",
        "sslmode=verify-full&connect_timeout=2&hostaddr=not-an-address",
        "sslmode=verify-full&connect_timeout=2&options=-c+search_path%3Dpublic",
        "sslmode=verify-full&connect_timeout=2&sslmode=disable",
        "sslmode=verify-full",
    ],
    ids=["insecure", "malformed", "hostaddr", "schema", "duplicate", "missing"],
)
def test_production_rejects_invalid_transport_when_base_preserves_options(
    tmp_path: Path, query: str
) -> None:
    # Given: otherwise valid production settings with an invalid transport query.
    ca = tmp_path / "private-ca.pem"
    _ = ca.write_text("synthetic CA fixture\n", encoding="ascii")
    ca.chmod(0o600)
    environment = {
        "ALLOWED_HOSTS": VALID_HOSTS,
        "APP_DATABASE_URL": (
            "postgresql://clinic_app:synthetic@db.qa.clinic-os.dev:5432/clinic?"
            + urlencode({"sslrootcert": str(ca)})
            + "&"
            + query
        ),
        "DJANGO_SETTINGS_MODULE": "config.settings.prod",
        "SECRET_KEY": VALID_RUNTIME_TOKEN,
        "SECURE_SSL_HOST": "app.qa.clinic-os.dev",
    }
    # When: production settings are imported in a fresh process.
    result = _run_process(
        [sys.executable, "-c", "import config.settings.prod"],
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    # Then: the existing fail-closed contract rejects them without exposing the URL.
    assert result.returncode != 0
    assert "database contract" in result.stderr
    assert environment["APP_DATABASE_URL"] not in result.stderr
    assert "synthetic" not in result.stderr

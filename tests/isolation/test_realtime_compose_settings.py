"""The realtime Compose profile's own environment starts its settings."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
_DEFAULT = re.compile(r"\$\{[A-Z0-9_]+:-([^}]*)\}")


def _services() -> dict[str, dict[str, str]]:
    """Return each realtime-profile service's resolved Django environment."""
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    return {
        name: {key: _DEFAULT.sub(r"\1", str(value)) for key, value in env.items()}
        for name, service in compose["services"].items()
        if "realtime" in service.get("profiles", [])
        and "DJANGO_SETTINGS_MODULE" in (env := service.get("environment") or {})
    }


def test_the_realtime_profile_has_django_services() -> None:
    assert _services()


@pytest.mark.parametrize("name", sorted(_services()))
def test_realtime_profile_settings_start_with_the_lock_identity(
    name: str, tmp_path: Path
) -> None:
    environment = {
        **_services()[name],
        # Only process plumbing is added; the database, lock and secret
        # settings are exactly the profile's (the secret directory is local).
        "CLINIC_SECRET_DIR": str(tmp_path),
        "HOME": str(tmp_path),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(ROOT),
    }
    code = (
        "import django; django.setup(); from django.conf import settings as s; "
        "print(s.DATABASES['locks']['USER'], s.DATABASES['default']['USER'])"
    )
    started = subprocess.run(  # noqa: S603 - fixed interpreter and arguments
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert started.returncode == 0, started.stderr
    assert started.stdout.split() == ["clinic_lock", "clinic_app"]

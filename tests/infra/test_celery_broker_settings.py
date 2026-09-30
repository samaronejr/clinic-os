"""The test settings module must never reach for the workstation's Redis.

``config.settings.base`` defaults ``CELERY_BROKER_URL`` to
``redis://localhost:6379/0``; on a developer machine that port can belong
to an unrelated project's broker. ``config.settings.test`` pins the
in-memory transport so bare ``pytest`` runs and the renewal coverage gate
never publish outside the process, while an explicit ``CELERY_BROKER_URL``
still wins so the real-broker gate (``CLINIC_BROKER_GATE=required``) keeps
working. These probes run in a scrubbed child so the caller's environment
cannot leak into the assertion.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import cast

REPOSITORY = Path(__file__).resolve().parents[2]

_CHILD_CODE = (
    "import json; "
    "import config.settings.test as settings; "
    "print(json.dumps({"
    "'broker': settings.CELERY_BROKER_URL, "
    "'backend': settings.CELERY_RESULT_BACKEND, "
    "}))"
)


def _resolved(extra: dict[str, str]) -> dict[str, object]:
    environment = {
        "HOME": os.environ.get("HOME", "/"),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(REPOSITORY),
        **extra,
    }
    completed = subprocess.run(  # noqa: S603 - fixed interpreter, closed argv
        (sys.executable, "-c", _CHILD_CODE),
        check=False,
        capture_output=True,
        cwd=REPOSITORY,
        env=environment,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    return cast("dict[str, object]", json.loads(completed.stdout.strip()))


def test_test_settings_default_the_broker_to_memory() -> None:
    resolved = _resolved({})
    assert resolved["broker"] == "memory://"
    # The result backend stays disabled, never a host Redis default.
    assert resolved["backend"] is None


def test_test_settings_honor_an_explicit_broker_url() -> None:
    """The real-broker gate sets its own URL; the test module must not eat it."""
    resolved = _resolved(
        {
            "CELERY_BROKER_URL": "redis://127.0.0.1:6390/7",
            "CELERY_RESULT_BACKEND": "redis://127.0.0.1:6390/8",
        }
    )
    assert resolved["broker"] == "redis://127.0.0.1:6390/7"
    assert resolved["backend"] == "redis://127.0.0.1:6390/8"

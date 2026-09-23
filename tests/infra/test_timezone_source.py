from __future__ import annotations

import os
import sys
import sysconfig
import zoneinfo
from importlib.metadata import version
from pathlib import Path

import pytest
from config.runtime import enforce_wheel_timezone
from ops.testing.process_helpers import run_process
from ops.testing.timezone_contract import validate_tzdata_lock

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_startup_uses_only_the_exact_tzdata_wheel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PYTHONTZPATH", "/usr/share/zoneinfo")

    enforce_wheel_timezone()

    assert os.environ["PYTHONTZPATH"] == ""
    assert version("tzdata") == "2026.3"
    assert zoneinfo.TZPATH == ()


def test_django_shared_startup_enforces_wheel_timezone_without_sitecustomize() -> None:
    purelib = sysconfig.get_paths()["purelib"]
    script = """
import os
import zoneinfo

os.environ["PYTHONTZPATH"] = "/usr/share/zoneinfo"
import config.settings.base

assert os.environ["PYTHONTZPATH"] == ""
assert zoneinfo.TZPATH == ()
""".strip()

    result = run_process(
        (
            "/usr/bin/env",
            f"PYTHONPATH={PROJECT_ROOT}:{purelib}",
            sys.executable,
            "-S",
            "-c",
            script,
        ),
    )

    assert result.returncode == 0, result.stderr


def test_tzdata_lock_rejects_a_wrong_wheel_hash(tmp_path: Path) -> None:
    lock_bytes = (PROJECT_ROOT / "uv.lock").read_bytes()
    validate_tzdata_lock(lock_bytes)

    altered = lock_bytes.replace(
        b"dc096730c87af6cab1b171c9d532be840741ff5d459015e7f6947bd7d7e54931",
        b"0c096730c87af6cab1b171c9d532be840741ff5d459015e7f6947bd7d7e54931",
    )
    with pytest.raises(RuntimeError, match="timezone lock contract failed"):
        validate_tzdata_lock(altered)


def test_tzdata_lock_cli_fails_on_a_wrong_wheel_hash(tmp_path: Path) -> None:
    altered = (
        (PROJECT_ROOT / "uv.lock")
        .read_bytes()
        .replace(
            b"dc096730c87af6cab1b171c9d532be840741ff5d459015e7f6947bd7d7e54931",
            b"0c096730c87af6cab1b171c9d532be840741ff5d459015e7f6947bd7d7e54931",
        )
    )
    lock_path = tmp_path / "uv.lock"
    lock_path.write_bytes(altered)

    result = run_process(
        (sys.executable, "-m", "ops.testing.timezone_contract", str(lock_path)),
    )

    assert result.returncode != 0
    assert "timezone lock contract failed" in result.stderr

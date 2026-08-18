from __future__ import annotations

from pathlib import Path
from typing import Self

import pytest
from ops.container import release

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class OwnerCursor:
    def __init__(self, role: str) -> None:
        self.role = role

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def execute(self, sql: str) -> None:
        assert sql == "SELECT current_user"

    def fetchone(self) -> tuple[str]:
        return (self.role,)


class OwnerConnection:
    def __init__(self, role: str) -> None:
        self.role = role

    def cursor(self) -> OwnerCursor:
        return OwnerCursor(self.role)


def test_release_owner_probe_rejects_runtime_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(release, "connection", OwnerConnection("clinic_owner"))
    release.assert_owner_connection()

    monkeypatch.setattr(release, "connection", OwnerConnection("clinic_app"))
    with pytest.raises(RuntimeError, match="release owner contract failed"):
        release.assert_owner_connection()


def test_release_plans_then_migrates_then_checks_without_wsgi() -> None:
    release_lines = (PROJECT_ROOT / "ops/container/release.sh").read_text().splitlines()
    assert release_lines[-5:] == [
        "/app/.venv/bin/python -m ops.container.release",
        "/app/.venv/bin/python manage.py showmigrations --plan",
        "/app/.venv/bin/python manage.py migrate --plan",
        "/app/.venv/bin/python manage.py migrate --noinput",
        "/app/.venv/bin/python manage.py migrate --check",
    ]
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text()
    default_command = dockerfile.rsplit("\nCMD ", 1)[-1]
    assert "migrate" not in default_command
    assert "release.sh" not in default_command

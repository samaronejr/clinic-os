from __future__ import annotations

from contextlib import nullcontext
from typing import TYPE_CHECKING, Self

from apps.core import readiness
from django.db import transaction

if TYPE_CHECKING:
    import pytest


class ReadyCursor:
    def __init__(self) -> None:
        self.statement = ""
        self.parameters: tuple[str, ...] = ()
        self.statements: list[str] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def execute(self, sql: str, parameters: tuple[str, ...] = ()) -> None:
        self.statement = sql
        self.parameters = parameters
        self.statements.append(sql)

    def fetchone(self) -> tuple[str, str] | tuple[str] | tuple[bool]:
        if "current_user" in self.statement:
            return ("clinic_app", "clinic_app")
        if "to_regclass" in self.statement:
            return (True,)
        return (True,)


class ReadyConnection:
    def __init__(self) -> None:
        self.probe = ReadyCursor()

    def cursor(self) -> ReadyCursor:
        return self.probe


def test_readiness_uses_bounded_app_role_metadata_probes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = ReadyConnection()
    monkeypatch.setattr(readiness, "connections", {"default": connection})
    monkeypatch.setattr(transaction, "atomic", nullcontext)

    assert readiness.probe_database_ready() is True
    assert connection.probe.statements[0] == "SET LOCAL statement_timeout = 2000"
    assert connection.probe.statements[1] == "SELECT current_user, current_schema()"
    assert tuple(readiness.REQUIRED_RELATIONS) == (
        "django_migrations",
        "django_session",
        "identity_organization",
        "identity_user",
        "identity_clinic",
        "identity_userclinicrole",
        "otp_totp_totpdevice",
        "audit_event",
        "intake_patient",
        "intake_patientclinicenrollment",
        "scheduling_availabilityblock",
        "scheduling_appointment",
    )

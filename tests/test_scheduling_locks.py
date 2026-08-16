import os
from concurrent.futures import ThreadPoolExecutor
from importlib import import_module
from importlib.util import find_spec
from threading import Barrier, Event
from uuid import UUID

import psycopg
import pytest
from django.db import connection
from psycopg.errors import LockNotAvailable

from database_urls import database_url_for_name


def test_advisory_keys_are_domain_separated_and_globally_ordered() -> None:
    module = (
        import_module("apps.scheduling.locks")
        if find_spec("apps.scheduling.locks") is not None
        else None
    )
    assert module is not None

    organization_id = UUID("11111111-1111-4111-8111-111111111111")
    clinic_id = UUID("22222222-2222-4222-8222-222222222222")
    patient_id = UUID("33333333-3333-4333-8333-333333333333")
    user_a = UUID("44444444-4444-4444-8444-444444444444")
    user_b = UUID("55555555-5555-4555-8555-555555555555")

    assert module.identity_lock_keys("  Alice  ", "  A@EXAMPLE.COM  ") == (
        "clinic-lock-v1:identity-email:a@example.com",
        "clinic-lock-v1:identity-username:alice",
    )
    assert module.clinic_lock_key(clinic_id) == f"clinic-lock-v1:clinic:{clinic_id}"
    assert module.patient_lock_key(organization_id, patient_id) == (
        f"clinic-lock-v1:patient:{organization_id}:{patient_id}"
    )
    assert module.user_lock_keys((user_b, user_a, user_b)) == (
        f"clinic-lock-v1:user:{user_a}",
        f"clinic-lock-v1:user:{user_b}",
    )


def _hold_clinic_gate(
    database_url: str,
    key: str,
    start: Barrier,
    contender_finished: Event,
) -> str:
    with psycopg.connect(database_url) as raw_connection:
        raw_connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            [key],
        )
        start.wait(timeout=5)
        if not contender_finished.wait(timeout=5):
            raise TimeoutError
    return "holder-committed"


def _contend_for_clinic_gate(
    database_url: str,
    key: str,
    start: Barrier,
    contender_finished: Event,
) -> str:
    with psycopg.connect(database_url) as raw_connection:
        start.wait(timeout=5)
        raw_connection.execute("SET LOCAL lock_timeout = '250ms'")
        try:
            raw_connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                [key],
            )
        except LockNotAvailable as error:
            raw_connection.rollback()
            return error.sqlstate or ""
        finally:
            contender_finished.set()
    return "unexpectedly-acquired"


@pytest.mark.django_db(transaction=True)
def test_opposite_timing_clinic_gates_fail_without_deadlock() -> None:
    module = import_module("apps.scheduling.locks")
    assert callable(getattr(module, "acquire_advisory_locks", None))
    clinic_id = UUID("66666666-6666-4666-8666-666666666666")
    key = module.clinic_lock_key(clinic_id)
    database_name = connection.settings_dict["NAME"]
    assert isinstance(database_name, str)
    database_url = database_url_for_name(
        os.environ["MIGRATION_DATABASE_URL"],
        database_name,
    )
    start = Barrier(2)
    contender_finished = Event()

    with ThreadPoolExecutor(max_workers=2) as executor:
        holder = executor.submit(
            _hold_clinic_gate,
            database_url,
            key,
            start,
            contender_finished,
        )
        contender = executor.submit(
            _contend_for_clinic_gate,
            database_url,
            key,
            start,
            contender_finished,
        )
        assert contender.result(timeout=5) == "55P03"
        assert holder.result(timeout=5) == "holder-committed"

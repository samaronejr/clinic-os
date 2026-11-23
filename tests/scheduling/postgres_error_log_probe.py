"""Run against the task's disposable synthetic PostgreSQL only.

Never print/retain the captured log, SQL, binds, keys, or fixture identifiers.
Exit 1 specifically for the SC-7 leak; fail separately if the witness is invalid.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from apps.tenancy.db import tenant_context
from apps.tenancy.envelope import EnvelopeFormatError, protect
from django.db import connection, transaction
from django.test import Client
from ops.perf.dataset import AGENDA_DAY, Tenant, seed_agenda, seed_patients, seed_tenant
from ops.perf.profiles import profile_named
from ops.perf.runtime import bootstrap_django, runtime_role
from ops.perf.staff_http import provision_logins, sign_in


def _count_reads(tenant: Tenant) -> int:
    with (
        runtime_role(),
        tenant_context(tenant.front_desk_id, tenant.organization_id),
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "SELECT count(*) FROM clinic_app.audit_event_tenant "
            "WHERE event_type='scheduling.agenda.viewed'"
        )
        return int(cursor.fetchone()[0])


def _fault_batch(
    tenant: Tenant,
) -> tuple[bytes, tuple[bytes, ...], tuple[bytes, ...]]:
    with (
        runtime_role(),
        tenant_context(tenant.front_desk_id, tenant.organization_id),
    ):
        wrong = protect(purpose="intake.patient.birth_date", plaintext=b"2000-01-01")
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.current_tenant', %s, true)",
            [str(tenant.organization_id)],
        )
        cursor.execute(
            "SELECT a.patient_id, p.full_name, a.id "
            "FROM clinic_app.scheduling_appointment a "
            "JOIN clinic_app.intake_patient p ON p.id=a.patient_id "
            "WHERE a.clinic_id=%s ORDER BY a.start_at, a.practitioner_id, a.id",
            [tenant.clinic_id],
        )
        rows = cursor.fetchall()
        assert len(rows) == 3
        # This synthetic fault injection changes one stored bytea as the table
        # owner. It does not alter the runtime role, RLS, crypto or logging.
        cursor.execute(
            "ALTER TABLE clinic_app.intake_patient "
            "DISABLE TRIGGER intake_patient_mirror_guard"
        )
        cursor.execute(
            "UPDATE clinic_app.intake_patient SET full_name=%s WHERE id=%s",
            [wrong, rows[1][0]],
        )
        assert cursor.rowcount == 1
        cursor.execute(
            "ALTER TABLE clinic_app.intake_patient "
            "ENABLE TRIGGER intake_patient_mirror_guard"
        )
    neighbors = tuple(bytes(rows[i][1]) for i in (0, 2))
    ids = (*[r[0] for r in rows], *[r[2] for r in rows], *tenant.sample_enrollments)
    return wrong, neighbors, tuple(str(value).encode().lower() for value in ids)


async def _docker_capture(*args: str) -> bytes:
    docker = shutil.which("docker")
    assert docker is not None
    proc = await asyncio.create_subprocess_exec(
        docker,
        *args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    raw, _ = await proc.communicate()
    assert proc.returncode == 0, "could not inspect the owned PostgreSQL container"
    return raw


def _error_log(container: str, since: str, pid: int) -> bytes:
    log = asyncio.run(_docker_capture("logs", "--since", since, container)).lower()
    backend = [line for line in log.splitlines() if f"[{pid}]".encode() in line]
    assert any(
        b"error:" in line and b"envelope purpose does not match" in line
        for line in backend
    ), "no real PostgreSQL purpose error"
    assert any(
        b"statement:" in line and b"protected_decrypt" in line for line in backend
    ), "no failed decrypt statement in server log"
    return log


def verify() -> bool:
    """Compare the real failure log to synthetic fixture values in memory only."""
    container = os.environ["OPTION_C_PG"]
    assert container.startswith("cpi-t83c-pg-")
    assert connection.settings_dict["HOST"] == "127.0.0.1"
    port = str(connection.settings_dict["PORT"])
    assert port.isdecimal()
    assert port != "5432"
    bindings = asyncio.run(_docker_capture("port", container)).splitlines()
    assert [line.partition(b" -> ")[2] for line in bindings] == [
        f"127.0.0.1:{port}".encode()
    ]
    with connection.cursor() as cursor:
        cursor.execute("SHOW log_min_error_statement")
        assert cursor.fetchone() == ("error",)
        cursor.execute("SHOW log_statement")
        assert cursor.fetchone() == ("none",)

    # Seed three names in one lane of the small profile's three practitioners,
    # using the same real services without running the load/profile runner.
    tenant = seed_tenant(profile_named("P-small"), index=0)
    tenant.physician_ids[:] = tenant.physician_ids[:1]
    seed_patients(tenant, 3, sample=3)
    assert seed_agenda(tenant, visits_per_clinician=3) == 3
    [login] = provision_logins(tenant, 1)
    url = f"/scheduling/clinics/{tenant.clinic_id}/agenda/board/day/{AGENDA_DAY}/"
    client = Client(HTTP_HOST="localhost", raise_request_exception=False)
    with runtime_role():
        sign_in(client, login)
        warm = client.get(url)
    assert warm.status_code == 200
    assert all(name.encode() in warm.content for name in tenant.sample_names)
    wrong, neighbors, patient_ids = _fault_batch(tenant)
    before = _count_reads(tenant)
    assert before == 1
    with runtime_role():
        pid = connection.connection.info.backend_pid
        since = datetime.now(UTC).isoformat()
        response = client.get(url)
    assert response.status_code == 500
    exc_info = getattr(response, "exc_info", None)
    assert isinstance(exc_info, tuple)
    assert exc_info[0] is EnvelopeFormatError
    assert _count_reads(tenant) == before
    assert all(name.encode() not in response.content for name in tenant.sample_names)

    log = _error_log(container, since, pid)
    # Scan the entire captured slice, not just the primary statement. That also
    # covers nested CONTEXT, DETAIL and multiline entries in this default log.
    unrelated = sum(
        value.hex().encode() in log or value.lower() in log for value in neighbors
    )
    failed = int(wrong.hex().encode() in log or wrong.lower() in log)
    identifiers = sum(value in log for value in patient_ids)
    plaintext = sum(name.encode().lower() in log for name in tenant.sample_names)
    sys.stdout.write(
        "SC7: PostgreSQL purpose error, HTTP 500, audit delta 0; "
        f"neighbor_envelopes={unrelated}/2 failing_envelope={failed}/1 "
        f"patient_appointment_enrollment_identifiers={identifiers}/9 "
        f"names={plaintext}/3\n"
    )
    return unrelated == failed == identifiers == plaintext == 0


if __name__ == "__main__":
    with bootstrap_django(Path(os.environ["OPTION_C_ROOT"])):
        passed = verify()
    raise SystemExit(0 if passed else 1)

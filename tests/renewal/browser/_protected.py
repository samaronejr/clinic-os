"""Fixture access to the protected-field envelope boundary.

Protected columns hold tenant envelopes, never plaintext: a fixture that
writes raw values produces rows the runtime cannot decrypt, and a fixture
that reads raw bytes sees ciphertext. These helpers encrypt and decrypt
through ``clinic_app.protected_encrypt``/``protected_decrypt`` with the
run's synthetic KEK — the same boundary the runtime uses — so seeded rows
are indistinguishable from runtime writes. The caller's connection must
already carry ``app.current_tenant`` (or a live patient-session GUC), which
is what resolves the tenant inside the database.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import psycopg

KEK_SECRET_FILE = "tenant-kek.secret"  # noqa: S105 - a file name, not a secret


def kek() -> str:
    """Return the run's synthetic tenant KEK from the runner secret store."""
    secret_dir = Path(os.environ["CLINIC_SECRET_DIR"])
    return (secret_dir / KEK_SECRET_FILE).read_text(encoding="ascii").strip()


def encrypt(conn: psycopg.Connection[Any], purpose: str, plaintext: bytes) -> bytes:
    """Encrypt ``plaintext`` under the resolved tenant's active DEK."""
    row = conn.execute(
        "SELECT clinic_app.protected_encrypt(%s, %s, %s)",
        [kek(), purpose, plaintext],
    ).fetchone()
    assert row is not None
    return bytes(row[0])


def decrypt(
    conn: psycopg.Connection[Any], purpose: str, envelope: bytes | None
) -> bytes | None:
    """Decrypt one stored envelope; ``NULL`` stays ``NULL``."""
    if envelope is None:
        return None
    row = conn.execute(
        "SELECT clinic_app.protected_decrypt(%s, %s, %s)",
        [kek(), purpose, envelope],
    ).fetchone()
    assert row is not None
    return bytes(row[0])


def rename_patient(  # noqa: PLR0913 - one registry correction needs its bindings
    conn: psycopg.Connection[Any],
    *,
    organization: str,
    clinic: str,
    patient: str,
    enrollment: str,
    actor: str,
    name: str,
) -> None:
    """Rename one seeded patient the way the registry contract requires.

    The registry name mirrors the latest demographics version, and the
    database admits the change only alongside that version and its
    correction receipt in the same transaction (todo 17), so a fixture
    rename appends both before it updates the registry row. The caller's
    connection transaction commits all three together.
    """
    row = conn.execute(
        "SELECT id, version FROM clinic_app.intake_patientdemographics "
        "WHERE organization_id = %s AND patient_id = %s "
        "ORDER BY version DESC LIMIT 1",
        [organization, patient],
    ).fetchone()
    previous, version = (row[0], int(row[1]) + 1) if row is not None else (None, 1)
    stored = conn.execute(
        "SELECT birth_date FROM clinic_app.intake_patient WHERE id = %s", [patient]
    ).fetchone()
    assert stored is not None
    # The version records the exact registry envelopes it mirrors; the
    # database refuses a registry row that differs from them.
    registry_name = encrypt(conn, "intake.patient.full_name", name.encode())
    produced = conn.execute(
        "INSERT INTO clinic_app.intake_patientdemographics "
        "(id, organization_id, patient_id, clinic_id, enrollment_id, version, "
        "legal_name, registry_full_name, registry_birth_date, source, created_at) "
        "VALUES (gen_random_uuid(), %s, %s, %s, %s, %s, %s, %s, %s, "
        "'staff_recorded', now()) RETURNING id",
        [
            organization,
            patient,
            clinic,
            enrollment,
            version,
            encrypt(conn, "intake.patientdemographics.legal_name", name.encode()),
            registry_name,
            stored[0],
        ],
    ).fetchone()
    assert produced is not None
    conn.execute(
        "INSERT INTO clinic_app.intake_demographicscorrection "
        "(id, organization_id, clinic_id, patient_id, demographics_id, "
        "previous_id, actor_id, actor_label, changed_fields, created_at) "
        "VALUES (gen_random_uuid(), %s, %s, %s, %s, %s, %s, 'fixture', "
        "'[\"legal_name\"]'::jsonb, now())",
        [organization, clinic, patient, produced[0], previous, actor],
    )
    conn.execute(
        "UPDATE clinic_app.intake_patient SET full_name = %s WHERE id = %s",
        [registry_name, patient],
    )

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

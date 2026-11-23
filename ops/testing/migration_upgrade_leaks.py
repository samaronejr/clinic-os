"""Fail the upgrade gate when a live secret value reaches a published file.

``python -m ops.testing.migration_upgrade_leaks <work_root>`` runs after the
leaf verifier, while the upgrade database still exists. It reads every live
secret the run holds - TOTP seeds (in ``identity_totpsecret`` and any device
key a migration has not cleared), static recovery tokens, wrapped data keys,
the gate KEK and the run's role passwords - and scans every file the gate
publishes from ``work_root`` (the base checkout excepted) for each value in
raw, hex, base64, base64url and base32 form. Any match fails the gate.
"""

from __future__ import annotations

import base64
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import psycopg

from ops.testing.migration_upgrade_witness import super_dsn

if TYPE_CHECKING:
    from collections.abc import Iterable

# Shorter values (an empty key, a version byte) would match unrelated text.
MINIMUM_SECRET_BYTES = 8

# (label, table, column): every column that holds secret material at the leaf.
SECRET_SOURCES = (
    ("totp seed", "identity_totpsecret", "secret"),
    ("totp device key", "otp_totp_totpdevice", "key"),
    ("static token", "otp_static_statictoken", "token"),
    ("wrapped tenant key", "tenancy_tenantdatakey", "wrapped_key"),
    ("wrapped audio key", "scribe_audiochunk", "wrapped_key"),
)
PASSWORD_VARIABLES = (
    "CLINIC_AGENT_PASSWORD",
    "CLINIC_OWNER_PASSWORD",
    "CLINIC_APP_PASSWORD",
    "CLINIC_SUPER_PASSWORD",
)


def encodings(secret: bytes) -> set[bytes]:
    """Return every textual form one secret can take in a JSON or log file."""
    forms = {
        secret,
        secret.hex().encode(),
        secret.hex().upper().encode(),
        base64.b64encode(secret),
        base64.urlsafe_b64encode(secret),
        base64.b32encode(secret),
    }
    forms |= {form.rstrip(b"=") for form in list(forms)}
    forms |= {base64.b32encode(secret).lower().rstrip(b"=")}
    return {form for form in forms if len(form) >= MINIMUM_SECRET_BYTES}


def _secret_bytes(value: object) -> list[bytes]:
    if isinstance(value, (bytes, memoryview)):
        return [bytes(value)]
    if isinstance(value, str) and value:
        text = value.encode()
        try:
            return [text, bytes.fromhex(value)]
        except ValueError:
            return [text]
    return []


def live_secrets(
    connection: psycopg.Connection[tuple[object, ...]],
) -> list[tuple[str, bytes]]:
    """Return every secret value the upgrade database and the run hold."""
    found: list[tuple[str, bytes]] = []
    for label, table, column in SECRET_SOURCES:
        exists = connection.execute(
            "SELECT to_regclass(%s) IS NOT NULL", [f"clinic_app.{table}"]
        ).fetchone()
        if not exists or not exists[0]:
            continue
        rows = connection.execute(
            f"SELECT {column} FROM clinic_app.{table}"  # noqa: S608 - fixed names
        ).fetchall()
        found.extend(
            (label, secret) for (value,) in rows for secret in _secret_bytes(value)
        )
    directory = os.environ.get("CLINIC_SECRET_DIR", "")
    if directory:
        found.extend(
            ("gate key file", secret)
            for path in sorted(Path(directory).glob("*.secret"))
            for secret in _secret_bytes(path.read_text("utf-8").strip())
        )
    found.extend(
        (name.lower(), os.environ[name].encode())
        for name in PASSWORD_VARIABLES
        if os.environ.get(name)
    )
    return [
        (label, secret)
        for label, secret in found
        if len(secret) >= MINIMUM_SECRET_BYTES
    ]


def leak_failures(
    paths: Iterable[Path], secrets: Iterable[tuple[str, bytes]]
) -> list[str]:
    """Name each file that holds any form of any secret (never the value)."""
    keyed = [(label, encodings(secret)) for label, secret in secrets]
    failures: list[str] = []
    for path in sorted(paths):
        blob = path.read_bytes()
        failures.extend(
            f"{path.name} holds a {label}"
            for label, variants in keyed
            if any(form in blob for form in variants)
        )
    return sorted(set(failures))


def published_files(work_root: Path) -> list[Path]:
    """Every file the gate leaves in ``work_root`` (not the base checkout)."""
    return [
        path
        for path in work_root.rglob("*")
        if path.is_file() and path.relative_to(work_root).parts[0] != "base"
    ]


def main() -> int:
    """Scan the published files for the run's live secrets."""
    work_root = Path(sys.argv[1])
    with psycopg.connect(super_dsn()) as connection:
        secrets = live_secrets(connection)
    files = published_files(work_root)
    failures = leak_failures(files, secrets)
    for failure in failures:
        print(f"upgrade-leaks FAILED: {failure}")  # noqa: T201 - gate transcript
    print(  # noqa: T201 - gate transcript
        f"upgrade-leaks: scanned {len(files)} files for {len(secrets)} live secret "
        f"values: {len(failures)} matches"
    )
    return 1 if failures or not secrets else 0


if __name__ == "__main__":
    sys.exit(main())

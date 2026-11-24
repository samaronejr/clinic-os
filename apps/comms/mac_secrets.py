"""Provision provider MAC secrets into the database without SQL text.

Each synthetic provider account's secret already lives in the adapter's own
configuration (``CLINIC_SECRET_DIR``). The database keeps a resolver-only copy
in ``comms_providermacsecret`` so its definers can verify MACs; it is written
here by ``COPY`` as ``clinic_resolver``, so the value never appears in any
statement, setting or log. An account without a provisioned secret fails
closed: its webhooks and provider answers are refused.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import connections, transaction

from apps.comms.inbox_inbound import secret_name
from apps.core.secrets import SecretUnavailableError, secret_store

if TYPE_CHECKING:
    from uuid import UUID


def provision_mac_secrets(*, using: str = "default") -> tuple[int, list[UUID]]:
    """Copy every missing account secret in; return (added, still missing).

    Must run on a connection whose login may assume ``clinic_resolver`` (the
    owner or an operator), never as the runtime role.
    """
    connection = connections[using]
    missing: list[UUID] = []
    with transaction.atomic(using=using), connection.cursor() as cursor:
        cursor.execute("SELECT pg_catalog.current_setting('role')")
        prior = cursor.fetchone()
        cursor.execute("SET LOCAL ROLE clinic_resolver")
        cursor.execute(
            "SELECT a.id FROM clinic_app.comms_provideraccount a WHERE NOT EXISTS"
            " (SELECT 1 FROM clinic_app.comms_providermacsecret s"
            " WHERE s.account_id = a.id) ORDER BY a.id"
        )
        accounts = [row[0] for row in cursor.fetchall()]
        rows: list[tuple[UUID, bytes]] = []
        for account_id in accounts:
            try:
                key = secret_store().get_secret(secret_name(account_id))
                rows.append((account_id, bytes.fromhex(key)))
            except (SecretUnavailableError, ValueError):
                missing.append(account_id)
        # Only accounts still missing a secret are copied, under the
        # transaction's row locks on the account list it just read.
        with cursor.cursor.copy(
            "COPY clinic_app.comms_providermacsecret (account_id, secret) FROM STDIN"
        ) as copy:
            for row in rows:
                copy.write_row(row)
        added = len(rows)
        # Hand the transaction back with the caller's own role.
        cursor.execute(
            "SELECT pg_catalog.set_config('role', %s, true)",
            [prior[0] if prior else "none"],
        )
    return added, missing

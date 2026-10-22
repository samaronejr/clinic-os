"""Owner-provisioned fake lab identity; assertions always use the runtime role."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

import psycopg

from renewal.browser._fixture_secrets import FixtureSecret
from renewal.browser.results_support import SOURCE

if TYPE_CHECKING:
    from uuid import UUID


@dataclass(frozen=True, slots=True)
class LabDelivery:
    """A signed synthetic delivery with no retained signing key."""

    event: UUID
    body: bytes
    headers: dict[str, str]


def seed_delivery(staff: dict[str, str], data: dict[str, str]) -> LabDelivery:
    """Seed only setup records; the real HTTP callback performs every result write."""
    principal, account, order, correlation, command, event = (uuid4() for _ in range(6))
    key = FixtureSecret(secrets.token_hex(32))
    with psycopg.connect(staff["dsn"]) as conn:
        conn.execute(
            "SELECT set_config('app.current_tenant',%s,true),"
            "set_config('app.current_user_id',%s,true)",
            [staff["organization"], staff["physician_a_id"]],
        )
        conn.execute(
            "INSERT INTO clinic_app.identity_serviceprincipal "
            "(id,organization_id,clinic_id,name,db_identity,purpose,"
            "grant_set_version,active) VALUES(%s,%s,%s,%s,%s,'lab_ingest',1,true)",
            [
                principal,
                staff["organization"],
                staff["clinic_a"],
                f"lab-{principal.hex}",
                f"clinic_agent_lab_{principal.hex}",
            ],
        )
        conn.execute(
            "INSERT INTO clinic_app.orders_labaccount "
            "(id,organization_id,clinic_id,created_at,principal_id,provider,"
            "signing_key_digest,active) "
            "VALUES(%s,%s,%s,now(),%s,'synthetic-lab-v1',%s,true)",
            [
                account,
                staff["organization"],
                staff["clinic_a"],
                principal,
                hashlib.sha256(key.encode()).hexdigest(),
            ],
        )
        conn.execute(
            "INSERT INTO clinic_app.orders_order "
            "(id,organization_id,clinic_id,created_at,enrollment_id,ordered_by_id,"
            "command_id,fingerprint,lab_account_id,correlation) "
            "VALUES(%s,%s,%s,now(),%s,%s,%s,%s,%s,%s)",
            [
                order,
                staff["organization"],
                staff["clinic_a"],
                data["enrollment"],
                staff["physician_a_id"],
                command,
                hashlib.sha256(str(order).encode()).hexdigest(),
                account,
                correlation,
            ],
        )
        conn.execute(
            "INSERT INTO clinic_app.orders_orderitem "
            "(id,organization_id,clinic_id,created_at,order_id,"
            "catalog_version,exam_code) "
            "VALUES(%s,%s,%s,now(),%s,1,'cbc')",
            [uuid4(), staff["organization"], staff["clinic_a"], order],
        )
        for sequence, state in enumerate(("draft", "placed"), 1):
            conn.execute(
                "INSERT INTO clinic_app.orders_orderevent "
                "(id,organization_id,clinic_id,created_at,order_id,sequence,state,"
                "actor_id,command_id) VALUES(%s,%s,%s,now(),%s,%s,%s,%s,%s)",
                [
                    uuid4(),
                    staff["organization"],
                    staff["clinic_a"],
                    order,
                    sequence,
                    state,
                    staff["physician_a_id"],
                    uuid4(),
                ],
            )
    secret = Path(os.environ["CLINIC_SECRET_DIR"]) / f"orders-lab-{account.hex}.secret"
    secret.write_text(key, encoding="ascii")
    secret.chmod(0o600)
    body = json.dumps(
        {
            "correlation": str(correlation),
            "event_id": str(event),
            "identity": {
                "birth_date": data["source_born"],
                "name": data["source_name"],
                "identifier_kind": "",
                "identifier": "",
            },
            "content_type": "application/pdf",
            "data_base64": base64.b64encode(SOURCE).decode("ascii"),
            "observations": [],
            "critical": False,
            "result_id": None,
            "predecessor_id": None,
        }
    ).encode()
    return LabDelivery(
        event,
        body,
        {
            "X-Lab-Account": str(account),
            "X-Lab-Signature": FixtureSecret(
                hmac.digest(bytes.fromhex(key), body, hashlib.sha256).hex()
            ),
            "Content-Type": "application/json",
        },
    )


def runtime_effects(staff: dict[str, str], event: UUID) -> tuple[int, int]:
    """Observe source and task counts only as the authorized runtime clinician."""
    with psycopg.connect(staff["dsn"]) as conn:
        conn.execute("SET LOCAL ROLE clinic_app")
        conn.execute(
            "SELECT set_config('app.current_tenant',%s,true),"
            "set_config('app.current_user_id',%s,true)",
            [staff["organization"], staff["physician_a_id"]],
        )
        row = conn.execute(
            "SELECT (SELECT count(*) FROM clinic_app.orders_resultversion "
            "WHERE event_id=%s),(SELECT count(*) "
            "FROM clinic_app.orders_resultworkitem w "
            "JOIN clinic_app.orders_resultversion v ON v.id=w.version_id "
            "WHERE v.event_id=%s)",
            [event, event],
        ).fetchone()
        assert row is not None
        return row[0], row[1]

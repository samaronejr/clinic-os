"""Owner-provisioned fake provider account and signed deliveries for the inbox."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import psycopg

from renewal.browser._fixture_secrets import FixtureSecret, worker_dsn
from renewal.browser._protected import encrypt

if TYPE_CHECKING:
    from datetime import datetime

    from playwright.sync_api import APIResponse, Page

PHONE_NUMBER_ID = "100000000000777"
CONTACT = "5511999990777"
TIMESTAMP = "1790000000"


@dataclass(frozen=True, slots=True)
class Provider:
    """One WhatsApp account; the signing key is never retained after seeding."""

    account: UUID
    staff: dict[str, str]
    key: FixtureSecret
    # Database time of the seed: counts cover only what this scenario created.
    since: datetime

    @property
    def path(self) -> str:
        return f"/comms/webhooks/whatsapp/{self.account}/"

    def headers(self, body: bytes) -> dict[str, str]:
        digest = hmac.digest(bytes.fromhex(self.key), body, hashlib.sha256).hex()
        return {
            "X-Hub-Signature-256": FixtureSecret("sha256=" + digest),
            "Content-Type": "application/json",
        }

    def deliver(
        self, page: Page, base: str, body: bytes, *, forged: bool = False
    ) -> APIResponse:
        headers = self.headers(body)
        if forged:
            headers["X-Hub-Signature-256"] = FixtureSecret("sha256=" + "0" * 64)
        return page.request.post(base + self.path, data=body, headers=headers)


def seed_provider(staff: dict[str, str]) -> Provider:
    """Provision principal, account and secret the way the owner CLI would.

    The clinic is shared by every scenario of the run, so an earlier scenario's
    account is retired and its open threads are closed first: one external
    account id is unique per provider, and a live thread is unique per contact.
    """
    principal, account = uuid4(), uuid4()
    key = FixtureSecret(secrets.token_hex(32))
    with psycopg.connect(staff["dsn"]) as conn:
        conn.execute(
            "SELECT set_config('app.current_tenant',%s,true),"
            "set_config('app.current_user_id',%s,true)",
            [staff["organization"], staff["physician_a_id"]],
        )
        conn.execute(
            "UPDATE clinic_app.comms_conversation SET state='closed' "
            "WHERE clinic_id=%s AND state<>'closed'",
            [staff["clinic_a"]],
        )
        conn.execute(
            "UPDATE clinic_app.comms_provideraccount SET active=false, "
            "external_account_id='retired-'||id::text "
            "WHERE clinic_id=%s AND external_account_id=%s",
            [staff["clinic_a"], PHONE_NUMBER_ID],
        )
        conn.execute(
            "INSERT INTO clinic_app.identity_serviceprincipal "
            "(id,organization_id,clinic_id,name,db_identity,purpose,"
            "grant_set_version,active) "
            "VALUES(%s,%s,%s,%s,%s,'messaging_ingest',1,true)",
            [
                principal,
                staff["organization"],
                staff["clinic_a"],
                f"inbox-{principal.hex}",
                f"clinic_agent_msg_{principal.hex}",
            ],
        )
        conn.execute(
            "INSERT INTO clinic_app.comms_provideraccount "
            "(id,organization_id,clinic_id,channel,provider,external_account_id,"
            "principal_id,signing_key_digest,verify_token_digest,active,created_at) "
            "VALUES(%s,%s,%s,'whatsapp','whatsapp-cloud-v1',%s,%s,%s,'',true,now())",
            [
                account,
                staff["organization"],
                staff["clinic_a"],
                PHONE_NUMBER_ID,
                principal,
                hashlib.sha256(key.encode()).hexdigest(),
            ],
        )
        row = conn.execute("SELECT clock_timestamp()").fetchone()
    assert row is not None
    secret = Path(os.environ["CLINIC_SECRET_DIR"]) / f"comms-inbox-{account.hex}.secret"
    secret.write_text(key, encoding="ascii")
    secret.chmod(0o600)
    return Provider(account, staff, key, row[0])


def message_body(
    text: str,
    *,
    event: str | None = None,
    phone: str = PHONE_NUMBER_ID,
    sender: str = CONTACT,
) -> bytes:
    return _body(
        phone,
        messages=[
            {
                "from": sender,
                "id": event or f"wamid.{uuid4().hex}",
                "timestamp": TIMESTAMP,
                "type": "text",
                "text": {"body": text},
            }
        ],
    )


def status_body(reference: str, status: str) -> bytes:
    return _body(
        PHONE_NUMBER_ID,
        statuses=[
            {
                "id": reference,
                "status": status,
                "timestamp": TIMESTAMP,
                "recipient_id": CONTACT,
                "pricing": {
                    "billable": True,
                    "pricing_model": "PMP",
                    "type": "regular",
                    "category": "utility",
                },
            }
        ],
    )


def _body(
    phone: str,
    *,
    messages: list[dict[str, object]] | None = None,
    statuses: list[dict[str, object]] | None = None,
) -> bytes:
    value = {
        "messaging_product": "whatsapp",
        "metadata": {"display_phone_number": "551100000777", "phone_number_id": phone},
        "contacts": [{"profile": {"name": "Paciente Sintetico"}, "wa_id": CONTACT}],
        "messages": messages or [],
        "statuses": statuses or [],
    }
    return json.dumps(
        {
            "object": "whatsapp_business_account",
            "entry": [
                {"id": "waba-777", "changes": [{"field": "messages", "value": value}]}
            ],
        }
    ).encode()


def seed_expired_thread(staff: dict[str, str], provider: Provider) -> UUID:
    """Insert a thread whose last inbound message is older than the 24 hour window."""
    conversation = uuid4()
    with psycopg.connect(staff["dsn"]) as conn:
        conn.execute(
            "SELECT set_config('app.current_tenant',%s,true)", [staff["organization"]]
        )
        conn.execute(
            "INSERT INTO clinic_app.comms_conversation "
            "(id,organization_id,clinic_id,provider_account_id,channel,purpose,state,"
            "contact_fingerprint,contact_address,contact_label,last_inbound_at,"
            "last_activity_at,needs_clinician,triage_category,revision,created_at,"
            "updated_at) VALUES (%s,%s,%s,%s,'whatsapp','transactional','open',%s,%s,"
            "NULL,now()-interval '25 hours',now()-interval '25 hours',false,'',1,"
            "now()-interval '25 hours',now()-interval '25 hours')",
            [
                conversation,
                staff["organization"],
                staff["clinic_a"],
                provider.account,
                hashlib.sha256(conversation.bytes).hexdigest(),
                encrypt(
                    conn,
                    "comms.conversation.contact_address",
                    b"+5511999990888",
                ),
            ],
        )
    return conversation


def latest_operation(staff: dict[str, str]) -> tuple[str, str | None]:
    """Return the newest message operation id and its provider reference."""
    with psycopg.connect(staff["dsn"]) as conn:
        conn.execute(
            "SELECT set_config('app.current_tenant',%s,true)", [staff["organization"]]
        )
        row = conn.execute(
            "SELECT id, provider_reference FROM clinic_app.comms_integrationoperation "
            "WHERE subject_type='comms.message' ORDER BY created_at DESC, id DESC "
            "LIMIT 1"
        ).fetchone()
    assert row is not None
    return str(row[0]), row[1]


def count(provider: Provider, table: str, where: str = "true") -> int:
    """Count inbox rows created since this scenario's provider was seeded."""
    assert table.startswith("comms_")
    with psycopg.connect(provider.staff["dsn"]) as conn:
        conn.execute(
            "SELECT set_config('app.current_tenant',%s,true)",
            [provider.staff["organization"]],
        )
        row = conn.execute(
            f"SELECT count(*) FROM clinic_app.{table} "  # noqa: S608
            f"WHERE created_at >= %s AND ({where})",
            [provider.since],
        ).fetchone()
    assert row is not None
    return int(row[0])


def run_worker(
    staff: dict[str, str], operation: str, outcome: str, root: Path
) -> list[str]:
    """Run the stored operation through the real clinic_app worker boundary."""
    env = {
        **os.environ,
        "APP_DATABASE_URL": worker_dsn(),
        "DJANGO_SETTINGS_MODULE": "config.settings.base",
        "CLINIC_DATA_MODE": "synthetic",
        "PYTHONPATH": str(Path.cwd()) + os.pathsep + str(Path.cwd() / "tests"),
    }
    result = subprocess.run(  # noqa: S603 - fixed test entrypoint and owned DSN
        [
            sys.executable,
            "-c",
            "import django; django.setup(); "
            "from renewal.messages_worker import main; main()",
            operation,
            outcome,
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    (root / f"worker-{operation}-{outcome}.log").write_text(
        result.stdout + result.stderr
    )
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    assert receipt["runtime_role"] == "clinic_app"
    return list(receipt["outcomes"])


GRANT_MESSAGES = """
import sys
from uuid import UUID

import django

django.setup()
from apps.realtime.scopes import grant_clinic_topic
from apps.tenancy.db import tenant_context

actor, organization, clinic, user = map(UUID, sys.argv[1:5])
with tenant_context(actor, organization):
    print(grant_clinic_topic(
        clinic_id=clinic, user_id=user, kind="messages", permission="demographics.read"
    ))
"""


def grant_messages(
    staff: dict[str, str], operator: str, user_id: str, root: Path
) -> None:
    """Opt one staff user into the messages topic through the operator grant.

    An organization administrator holds ``staff.organization``; the lease lives on
    this suite's own broker, which the served stream reads.
    """
    env = {
        **os.environ,
        "APP_DATABASE_URL": worker_dsn(),
        "DJANGO_SETTINGS_MODULE": "config.settings.base",
        "CLINIC_DATA_MODE": "synthetic",
        "REALTIME_ENABLED": "true",
        "REALTIME_REDIS_URL": os.environ["CLINIC_RENEWAL_REALTIME_REDIS_URL"],
        "PYTHONPATH": str(Path.cwd()) + os.pathsep + str(Path.cwd() / "tests"),
    }
    args = (operator, staff["organization"], staff["clinic_a"], user_id)
    result = subprocess.run(  # noqa: S603 - fixed test entrypoint and owned DSN
        [sys.executable, "-c", GRANT_MESSAGES, *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    (root / "grant-messages.log").write_text(result.stdout + result.stderr)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"clinic:{staff['clinic_a']}:messages"


def latest_enrollment(staff: dict[str, str]) -> str:
    """Return the newest clinic-A enrollment id; no patient identity is read."""
    with psycopg.connect(staff["dsn"]) as conn:
        conn.execute(
            "SELECT set_config('app.current_tenant',%s,true)", [staff["organization"]]
        )
        row = conn.execute(
            "SELECT id FROM clinic_app.intake_patientclinicenrollment "
            "WHERE clinic_id=%s ORDER BY created_at DESC, id DESC LIMIT 1",
            [staff["clinic_a"]],
        ).fetchone()
    assert row is not None
    return str(row[0])


def add_care_team(staff: dict[str, str], enrollment: str, user_id: str) -> None:
    """Put one physician on the patient's care team, as the owner would."""
    with psycopg.connect(staff["dsn"]) as conn:
        conn.execute(
            "SELECT set_config('app.current_tenant',%s,true)", [staff["organization"]]
        )
        conn.execute(
            "INSERT INTO clinic_app.identity_careteammembership "
            "(id,organization_id,clinic_id,patient_enrollment_id,user_id,role,"
            "valid_from) VALUES (%s,%s,%s,%s,%s,'physician',now()-interval '1 day')",
            [uuid4(), staff["organization"], staff["clinic_a"], enrollment, user_id],
        )


def seed_quick_reply(staff: dict[str, str], title: str, body: str) -> None:
    """Store one transactional snippet; a human still reviews it before sending."""
    with psycopg.connect(staff["dsn"]) as conn:
        conn.execute(
            "SELECT set_config('app.current_tenant',%s,true)", [staff["organization"]]
        )
        conn.execute(
            "INSERT INTO clinic_app.comms_quickreply "
            "(id,organization_id,clinic_id,title,body,purpose,active,created_by_id,"
            "created_at) VALUES (%s,%s,%s,%s,%s,'transactional',true,%s,now())",
            [
                uuid4(),
                staff["organization"],
                staff["clinic_a"],
                title,
                body,
                staff["receptionist_id"],
            ],
        )

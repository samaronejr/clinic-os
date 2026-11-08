"""Synthetic fiscal history seeded through the real role and transition guards."""

from __future__ import annotations

import json
import secrets
from uuid import uuid4

import psycopg

from renewal.browser._protected import encrypt


def seed_fiscal_history(
    staff: dict[str, str], manager: dict[str, str], invoice_id: str
) -> None:
    """Retain reviewed fixture terms, authorization and safe rejection evidence."""
    configuration, legal_entity = uuid4(), uuid4()
    with psycopg.connect(staff["dsn"]) as connection:
        connection.execute(
            "SELECT set_config('app.current_tenant',%s,true),"
            "set_config('app.current_user_id',%s,true)",
            [staff["organization"], manager["id"]],
        )
        for role in ("finance", "owner"):
            connection.execute(
                "INSERT INTO clinic_app.identity_userclinicrole "
                "(id,user_id,organization_id,clinic_id,role,workflow_bundle_version) "
                "VALUES (%s,%s,%s,%s,%s,1)",
                [
                    uuid4(),
                    manager["id"],
                    staff["organization"],
                    staff["clinic_a"],
                    role,
                ],
            )
        connection.execute("SET ROLE clinic_app")
        book = connection.execute(
            "SELECT id FROM clinic_app.billing_journalbook WHERE clinic_id=%s",
            [staff["clinic_a"]],
        ).fetchone()
        if book is None:
            book = connection.execute(
                "INSERT INTO clinic_app.billing_journalbook "
                "(id,organization_id,clinic_id,policy_version,currency,created_at) "
                "VALUES (%s,%s,%s,'completion-net-v1','BRL',now()) RETURNING id",
                [uuid4(), staff["organization"], staff["clinic_a"]],
            ).fetchone()
        assert book is not None
        taxes = encrypt(
            connection,
            "billing.fiscal.configuration_taxes",
            json.dumps({"ISS": {"classification": "SYNTHETIC"}}).encode(),
        )
        connection.execute("RESET ROLE")
        connection.execute(
            "INSERT INTO clinic_app.billing_fiscalconfiguration "
            "(id,organization_id,clinic_id,book_id,legal_entity_reference,legal_entity,"
            "provider,municipality_code,service_code,tax_regime,ibs_cbs_applicability,"
            "tax_fields,review_reference,review_kind,reviewed_at,expires_at,"
            "cancellation_window_seconds,created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,'national','0000000','SYNTHETIC-SERVICE',"
            "'SYNTHETIC','not_applicable',%s,%s,'synthetic_fixture',now(),"
            "now()+interval '1 day',3600,now())",
            [
                configuration,
                staff["organization"],
                staff["clinic_a"],
                book[0],
                legal_entity,
                encrypt(
                    connection,
                    "billing.fiscal.legal_entity",
                    b'{"name":"Synthetic clinic"}',
                ),
                taxes,
                uuid4(),
            ],
        )
        connection.execute("SET ROLE clinic_app")
        for outcome in ("authorized", "rejected"):
            document, command = uuid4(), uuid4()
            connection.execute(
                "INSERT INTO clinic_app.billing_fiscaldocument "
                "(id,organization_id,clinic_id,book_id,configuration_id,invoice_id,"
                "legal_entity_reference,provider,municipality_code,service_code,tax_fields,"
                "amount_minor,rehearsal,status,create_command_id,create_fingerprint,"
                "last_command_id,dps_id,request_xml,rejection_code,created_at) "
                "SELECT %s,i.organization_id,i.clinic_id,%s,%s,i.id,%s,'national',"
                "'0000000','SYNTHETIC-SERVICE',%s,i.amount_minor,true,'draft',%s,%s,%s,"
                "%s,%s,'',now() FROM clinic_app.billing_invoice i WHERE i.id=%s",
                [
                    document,
                    book[0],
                    configuration,
                    legal_entity,
                    taxes,
                    command,
                    secrets.token_bytes(32),
                    command,
                    encrypt(
                        connection,
                        "billing.fiscal.dps_id",
                        f"DPS-SYNTHETIC-{document.hex}".encode(),
                    ),
                    encrypt(
                        connection,
                        "billing.fiscal.request_xml",
                        b"<DPS>SYNTHETIC-NOT-FISCAL</DPS>",
                    ),
                    invoice_id,
                ],
            )
            connection.execute(
                "UPDATE clinic_app.billing_fiscaldocument "
                "SET status='submitted',last_command_id=%s WHERE id=%s",
                [uuid4(), document],
            )
            if outcome == "authorized":
                connection.execute(
                    "UPDATE clinic_app.billing_fiscaldocument SET status='authorized',"
                    "last_command_id=%s,xml_reference=id,xml_payload=%s,"
                    "provider_reference=%s "
                    "WHERE id=%s",
                    [
                        uuid4(),
                        encrypt(
                            connection,
                            "billing.fiscal.xml",
                            b"<rehearsal>SYNTHETIC-NOT-FISCAL</rehearsal>",
                        ),
                        encrypt(
                            connection,
                            "billing.fiscal.provider_reference",
                            f"SYNTHETIC-{document.hex}".encode(),
                        ),
                        document,
                    ],
                )
            else:
                connection.execute(
                    "UPDATE clinic_app.billing_fiscaldocument SET status='rejected',"
                    "last_command_id=%s,rejection_code='E0004' WHERE id=%s",
                    [uuid4(), document],
                )

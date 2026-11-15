"""The guardian's legal evidence for sec.12 row 19 (todo 75).

Only the evidence attachment is seeded here: a synthetic legal document has no
upload screen whose scan the browser could drive. The grant itself is
requested, verified and activated through the identity services
(``flagship_fixture`` ``delegate-grant``), and no delegate read is inserted:
every access event the scenario asserts is written by the product's own
recorder when the guardian reads through the portal.
"""

from __future__ import annotations

import secrets
from uuid import uuid4

import psycopg


def seed_evidence(staff: dict[str, str], represented: dict[str, str]) -> str:
    """Return an available, scanned evidence attachment for the patient."""
    encounter, attachment = str(uuid4()), str(uuid4())
    with psycopg.connect(staff["dsn"]) as conn:
        conn.execute(
            "SELECT set_config('app.current_tenant',%s,true)", [staff["organization"]]
        )
        conn.execute(
            "SELECT set_config('app.current_user_id',%s,true)",
            [staff["physician_a_id"]],
        )
        conn.execute(
            "INSERT INTO clinic_app.ehr_encounter (id,state,revision,created_at,"
            "clinic_id,organization_id,patient_id,physician_id,unscheduled_reason) "
            "VALUES (%s,'open',1,now(),%s,%s,%s,%s,'documentation_only')",
            [
                encounter,
                staff["clinic_a"],
                staff["organization"],
                represented["patient"],
                staff["physician_a_id"],
            ],
        )
        conn.execute(
            "INSERT INTO clinic_app.ehr_clinicalattachment (id,storage_key,"
            "file_name,declared_type,detected_type,size_bytes,sha256,state,"
            "scan_attempts,scan_reason,created_at,clinic_id,encounter_id,"
            "organization_id,patient_id,uploader_id) VALUES (%s,%s,"
            "'autoridade-sintetica.pdf','application/pdf','application/pdf',64,%s,"
            "'quarantined',0,'',now(),%s,%s,%s,%s,%s)",
            [
                attachment,
                secrets.token_hex(32),
                secrets.token_hex(32),
                staff["clinic_a"],
                encounter,
                staff["organization"],
                represented["patient"],
                staff["physician_a_id"],
            ],
        )
        conn.execute(
            "UPDATE clinic_app.ehr_clinicalattachment SET state='available',"
            "scan_attempts=1,scanned_at=now() WHERE id=%s",
            [attachment],
        )
    return attachment

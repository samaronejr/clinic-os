"""Logical-recovery verification proofs: chain, keys, objects, sequences."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

import psycopg
import pytest
from apps.audit.services import record_phase1_event
from apps.intake.models import Patient
from apps.tenancy.db import tenant_context
from django.db import connection
from ops.testing import restore_rehearsal, restore_verification
from ops.testing.restore_contract import RestoreContractError

from otp_test_support import runtime_role

if TYPE_CHECKING:
    from pathlib import Path

    from conftest import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)

ORGANIZATION_ID = "10000000-0000-4000-8000-000000000020"
KEK = "ab" * 32
ENVELOPE = "01" + "00" * 64
DIGEST = "cd" * 32


class StubClient:
    """Return queued SQL responses; the last line is the result."""

    def __init__(self, responses: dict[str, str] | None = None) -> None:
        self.responses = responses or {}
        self.statements: list[str] = []

    def sql(self, statement: str) -> str:
        self.statements.append(statement)
        for marker, response in self.responses.items():
            if marker in statement:
                return response
        return ""


class DjangoClient:
    """Adapt the Django connection to the read-only SQL client protocol."""

    def sql(self, statement: str) -> str:
        with connection.cursor() as cursor:
            cursor.execute(statement)
            rows = cursor.fetchall()
        return "\n".join(str(row[0]) for row in rows)


class SuperuserClient:
    """Run verification SQL over the superuser DSN the rehearsal uses."""

    def __init__(self, database_url: str) -> None:
        self._database_url = database_url

    def sql(self, statement: str) -> str:
        with psycopg.connect(self._database_url) as raw:
            cursor = raw.execute(statement)
            try:
                rows = cursor.fetchall()
            except psycopg.ProgrammingError:
                rows = []
        return "\n".join(_psql_value(row[0]) for row in rows)


def _psql_value(value: object) -> str:
    """Render a psycopg scalar the way psql --tuples-only would."""
    if isinstance(value, bool):
        return "t" if value else "f"
    return str(value)


class AppClient:
    """Run verification SQL over the runtime-role DSN the rehearsal uses.

    psql accepts ``SET ...;SELECT ...`` in one command; psycopg's extended
    protocol does not, so statements are split and executed inside one
    transaction where the session SET applies to the following SELECT.
    """

    def __init__(self, database_url: str) -> None:
        self._database_url = database_url

    def sql(self, statement: str) -> str:
        result = ""
        with psycopg.connect(self._database_url) as raw:
            for part in statement.split(";"):
                if not part.strip():
                    continue
                cursor = raw.execute(part)
                try:
                    rows = cursor.fetchall()
                except psycopg.ProgrammingError:
                    rows = []
                if rows:
                    result = "\n".join(_psql_value(row[0]) for row in rows) + "\n"
        return result


def test_audit_chain_verification_detects_breaks(rbac_graph: RbacGraph) -> None:
    graph = rbac_graph
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        record_phase1_event(
            "intake.patient_access.issued",
            clinic_id=graph.clinic_a,
            affected_record_id=graph.clinic_a,
        )
        record_phase1_event(
            "intake.patient_access.issued",
            clinic_id=graph.clinic_a,
            affected_record_id=graph.clinic_b,
        )
    restore_verification.require_audit_chain(DjangoClient())
    with pytest.raises(RestoreContractError, match="audit hash chain"):
        restore_verification.require_audit_chain(StubClient({"audit_event": "3\n"}))


def test_audit_content_verification_rejects_fabricated_rows(
    rbac_graph: RbacGraph,
) -> None:
    """A fabricated row with a consistent link but fake content must fail."""
    graph = rbac_graph
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        record_phase1_event(
            "intake.patient_access.issued",
            clinic_id=graph.clinic_a,
            affected_record_id=graph.clinic_a,
        )
    client = DjangoClient()
    verified = restore_verification.require_audit_content(client)
    assert verified >= 1
    # Fabricate one row: genesis prev_hash, a syntactically valid but
    # content-free curr_hash. Linkage accepts it; content proof must not.
    fabricated = json.dumps(
        {
            "seq": 999,
            "organization_id": str(graph.organization_a),
            "actor_user_id": str(graph.physician),
            "event_type": "intake.patient_access.issued",
            "component_id": "clinic-os-web",
            "component_ip": None,
            "affected_record_type": "intake.patient_access",
            "affected_record_id": str(graph.clinic_a),
            "occurred_at_utc": "2026-09-22T00:00:00.000000Z",
            "payload": {"clinic_id": str(graph.clinic_a)},
            "prev_hash": "00" * 32,
            "curr_hash": "ab" * 32,
        }
    )
    stub = StubClient({"audit_event": fabricated + "\n"})
    with pytest.raises(RestoreContractError, match="content hash"):
        restore_verification.require_audit_content(stub)


def test_posture_rejects_role_and_acl_drift(
    rbac_graph: RbacGraph, superuser_database_url: str
) -> None:
    """BYPASSRLS drift and closed-table grants must fail the posture proof."""
    client = SuperuserClient(superuser_database_url)
    restore_verification.require_owner_rls_acl_posture(client)
    with psycopg.connect(superuser_database_url, autocommit=True) as raw:
        raw.execute("ALTER ROLE clinic_app BYPASSRLS")
        try:
            with pytest.raises(RestoreContractError, match="bypassrls"):
                restore_verification.require_owner_rls_acl_posture(client)
        finally:
            raw.execute("ALTER ROLE clinic_app NOBYPASSRLS")
        raw.execute("GRANT SELECT ON clinic_app.tenancy_tenantdatakey TO clinic_app")
        try:
            with pytest.raises(RestoreContractError, match="acl"):
                restore_verification.require_owner_rls_acl_posture(client)
        finally:
            raw.execute(
                "REVOKE SELECT ON clinic_app.tenancy_tenantdatakey FROM clinic_app"
            )
        # A PUBLIC grant names no clinic_app acl entry, yet the effective
        # privilege check must still reject it.
        raw.execute("GRANT SELECT ON clinic_app.tenancy_tenantdatakey TO PUBLIC")
        try:
            with pytest.raises(RestoreContractError, match="acl"):
                restore_verification.require_owner_rls_acl_posture(client)
        finally:
            raw.execute("REVOKE SELECT ON clinic_app.tenancy_tenantdatakey FROM PUBLIC")


def test_empty_target_gate_rejects_populated_database(
    rbac_graph: RbacGraph, superuser_database_url: str
) -> None:
    """A target holding domain rows is never a valid restore destination."""
    restore_verification.require_empty_target(StubClient({"": "0\n"}))
    client = SuperuserClient(superuser_database_url)
    with pytest.raises(RestoreContractError, match="not empty"):
        restore_verification.require_empty_target(client)


def test_app_role_probes_require_rls_denial() -> None:
    """The app-role probes fail when cross-tenant reads are not denied."""
    good = StubClient(
        {
            "WHERE organization_id": "SET\n0\n",
            "app.current_tenant": "SET\n1\n",
            "intake_patient": "0\n",
            "has_table_privilege": "f\n",
            "has_function_privilege": "f\n",
        }
    )
    restore_verification.require_app_role_probes(
        good, organization_id=ORGANIZATION_ID, expected_patient_count=1
    )
    leaked = StubClient(
        {
            "WHERE organization_id": "SET\n2\n",
            "app.current_tenant": "SET\n1\n",
            "intake_patient": "0\n",
            "has_table_privilege": "f\n",
            "has_function_privilege": "f\n",
        }
    )
    with pytest.raises(RestoreContractError, match="cross-tenant"):
        restore_verification.require_app_role_probes(
            leaked, organization_id=ORGANIZATION_ID, expected_patient_count=1
        )
    drifted = StubClient(
        {
            "WHERE organization_id": "SET\n0\n",
            "app.current_tenant": "SET\n1\n",
            "intake_patient": "0\n",
            "has_table_privilege": "t\n",
            "has_function_privilege": "f\n",
        }
    )
    with pytest.raises(RestoreContractError, match="closed-table"):
        restore_verification.require_app_role_probes(
            drifted, organization_id=ORGANIZATION_ID, expected_patient_count=1
        )
    with pytest.raises(RestoreContractError, match="invalid"):
        restore_verification.require_app_role_probes(
            good, organization_id=ORGANIZATION_ID, expected_patient_count=0
        )


def test_app_role_probes_reject_permissive_policy(
    rbac_graph: RbacGraph,
    app_database_url: str,
    superuser_database_url: str,
) -> None:
    """A permissive USING(true) policy must fail against a real foreign row."""
    graph = rbac_graph
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        Patient.objects.create(
            organization_id=graph.organization_a,
            full_name="Probe Patient",
            birth_date=date(2000, 1, 2),
        )
    privileged = SuperuserClient(superuser_database_url)
    restore_verification.seed_foreign_probe_tenant(privileged)
    app = AppClient(app_database_url)
    restore_verification.require_app_role_probes(
        app,
        organization_id=str(graph.organization_a),
        expected_patient_count=1,
    )
    with psycopg.connect(superuser_database_url, autocommit=True) as raw:
        raw.execute(
            "CREATE POLICY leaky_probe ON clinic_app.intake_patient "
            "FOR SELECT TO clinic_app USING (true)"
        )
        try:
            with pytest.raises(RestoreContractError):
                restore_verification.require_app_role_probes(
                    app,
                    organization_id=str(graph.organization_a),
                    expected_patient_count=1,
                )
        finally:
            raw.execute("DROP POLICY leaky_probe ON clinic_app.intake_patient")
            raw.execute(
                "DELETE FROM clinic_app.intake_patient WHERE id = %s",
                [restore_verification.FOREIGN_PROBE_PATIENT],
            )
            raw.execute(
                "DELETE FROM clinic_app.identity_organization WHERE id = %s",
                [restore_verification.FOREIGN_PROBE_ORGANIZATION],
            )


def test_tenant_key_status_must_match_source() -> None:
    source = StubClient({"tenantdatakey": "1|active\n2|retired\n"})
    target = StubClient({"tenantdatakey": "1|active\n2|retired\n"})
    assert (
        restore_verification.require_equal_tenant_key_status(source, target)
        == "1|active\n2|retired"
    )
    drifted = StubClient({"tenantdatakey": "1|active\n"})
    with pytest.raises(RestoreContractError, match="key status"):
        restore_verification.require_equal_tenant_key_status(source, drifted)


def test_key_probe_binds_envelope_and_digest() -> None:
    statement_marker = "tenant_decrypt"
    client = StubClient({statement_marker: f"SET\n{DIGEST}\n"})
    restore_verification.require_key_probe(
        client,
        organization_id=ORGANIZATION_ID,
        kek=KEK,
        envelope_hex=ENVELOPE,
        expected_sha256=DIGEST,
    )
    assert statement_marker in client.statements[0]
    assert ORGANIZATION_ID in client.statements[0]
    with pytest.raises(RestoreContractError, match="probe"):
        restore_verification.require_key_probe(
            StubClient({statement_marker: "SET\n" + "ef" * 32 + "\n"}),
            organization_id=ORGANIZATION_ID,
            kek=KEK,
            envelope_hex=ENVELOPE,
            expected_sha256=DIGEST,
        )
    with pytest.raises(RestoreContractError, match="invalid"):
        restore_verification.require_key_probe(
            client,
            organization_id="not-a-uuid",
            kek=KEK,
            envelope_hex=ENVELOPE,
            expected_sha256=DIGEST,
        )
    with pytest.raises(RestoreContractError, match="invalid"):
        restore_verification.require_key_probe(
            client,
            organization_id=ORGANIZATION_ID,
            kek="short",
            envelope_hex=ENVELOPE,
            expected_sha256=DIGEST,
        )


def test_object_store_manifest_binds_every_object(tmp_path: Path) -> None:
    payload = b"%PDF-1.4\nsynthetic\n"
    key = "aa" * 32
    digest = hashlib.sha256(payload).hexdigest()
    root = tmp_path / "objects"
    root.mkdir(mode=0o700)
    (root / key).write_bytes(payload)
    manifest = f"{key}|{ORGANIZATION_ID}|{digest}|{len(payload)}\n"
    client = StubClient(
        {
            "ehr_clinicalattachment": manifest,
            "tenant_decrypt": f"SET\n{digest}\n",
        }
    )
    assert restore_verification.verify_object_store(client, root, KEK) == 1

    (root / ("bb" * 32)).write_bytes(b"extra")
    with pytest.raises(RestoreContractError, match="object store"):
        restore_verification.verify_object_store(client, root, KEK)
    (root / ("bb" * 32)).unlink()

    wrong = StubClient(
        {
            "ehr_clinicalattachment": manifest,
            "tenant_decrypt": "SET\n" + "00" * 32 + "\n",
        }
    )
    with pytest.raises(RestoreContractError, match="digest"):
        restore_verification.verify_object_store(wrong, root, KEK)
    (root / key).unlink()
    with pytest.raises(RestoreContractError, match="object store"):
        restore_verification.verify_object_store(client, root, KEK)


def test_sequence_headroom_covers_every_restored_sequence() -> None:
    responses = {
        "audit_event_seq_seq": "5|4\n",
        "otp_totp_totpdevice_id_seq": "3|2\n",
        "scheduling_waitlistentry_id_seq": "7|6\n",
        "teleconsult_teleconsultevent_id_seq": "9|8\n",
    }
    client = StubClient(responses)
    observed = restore_verification.require_sequence_headroom(client)
    assert {name for name, _, _ in observed} == set(responses)
    assert all(next_value > maximum for _, next_value, maximum in observed)
    stalled = StubClient({**responses, "audit_event_seq_seq": "4|4\n"})
    with pytest.raises(RestoreContractError, match="sequence"):
        restore_verification.require_sequence_headroom(stalled)


def test_probe_binding_file_must_be_private_and_exact(tmp_path: Path) -> None:
    probe = {
        "envelope_hex": ENVELOPE,
        "expected_sha256": DIGEST,
        "kek": KEK,
        "organization_id": ORGANIZATION_ID,
    }
    path = tmp_path / "probe.json"
    path.write_text(json.dumps(probe))
    path.chmod(0o600)
    parsed = restore_rehearsal._read_probe(path)
    assert parsed.organization_id == ORGANIZATION_ID
    path.chmod(0o644)
    with pytest.raises(RestoreContractError, match="private"):
        restore_rehearsal._read_probe(path)
    path.chmod(0o600)
    path.write_text(json.dumps({**probe, "extra": "field"}))
    with pytest.raises(RestoreContractError, match="invalid"):
        restore_rehearsal._read_probe(path)
    with pytest.raises(RestoreContractError, match="invalid"):
        restore_rehearsal._read_probe(tmp_path / "absent.json")

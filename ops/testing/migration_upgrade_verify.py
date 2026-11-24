"""Verify a base->candidate upgrade preserved data and boundaries.

Piped into ``manage.py shell`` on the *candidate* worktree, pointed at the
upgraded database. Reads ``UPGRADE_SEED_FILE`` markers written by
``migration_upgrade_seed.py`` and asserts, exit-gate style:

* every seeded row is still present and identical;
* the protected-field transform actually ran (``full_name``/``birth_date``
  are ciphertext envelopes, not the seeded plaintext);
* the model boundary still decrypts to the same logical values;
* FORCE RLS stays on for tenant tables and the runtime role sees no
  cross-tenant row;
* the two seeded audit events still verify as a hash chain;
* grants on the new comms tables remain narrow;
* seeded inbox histories keep their causal attempt order, and unordered ones
  hold every send until staff reconcile them;
* ``makemigrations --check --dry-run`` reports no drift (run separately).

The script exits non-zero with named failures when any check fails.
"""

import json
import os
import sys
from datetime import date
from importlib import import_module
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
from apps.audit.services import verify_chain
from apps.audit.verification import AuditChainVerificationError
from apps.intake.models import Patient
from django.db import connection

from ops.testing.migration_upgrade_ai_verify import verify_ai
from ops.testing.migration_upgrade_fixture_oracle import check_fixtures
from ops.testing.migration_upgrade_inbox_fixture import PARITY_STATES
from ops.testing.migration_upgrade_mutation_sweep import verify_sweep_coverage
from ops.testing.migration_upgrade_receipt_reader import (
    database_identity,
    require_completed_seed,
)
from ops.testing.migration_upgrade_rows import (
    verify_forward_defaults,
    verify_seed_cardinality,
    verify_seed_envelope_keys,
)

failures: list[str] = []


def check(condition: bool, name: str) -> None:
    """Record one failed upgrade assertion."""
    if not condition:
        failures.append(name)


seed_contents = Path(os.environ["UPGRADE_SEED_FILE"]).read_text("utf-8")
required_seeds = require_completed_seed(seed_contents)
markers = json.loads(seed_contents)
failures.extend(verify_seed_cardinality())
failures.extend(verify_forward_defaults())
failures.extend(verify_seed_envelope_keys())
fixture = check_fixtures(required_seeds)
failures.extend(fixture.failures)
if not failures and os.environ.get("UPGRADE_FIXTURE_SWEEP") == "1":
    verify_sweep_coverage(required_seeds)
inbox_fixture = fixture.inbox
organization_id = UUID(markers["organization_id"])
patient_id = UUID(markers["patient_id"])
appointment_id = UUID(markers["appointment_id"])
availability_id = UUID(markers["availability_id"])

# Every check below runs as the runtime role inside the seeded tenant:
# ``clinic_app`` + session-scoped tenant/user GUCs, the same boundary the
# application enforces in production traffic.
with connection.cursor() as cursor:
    cursor.execute("SET ROLE clinic_app")
    cursor.execute(
        "SELECT pg_catalog.set_config('app.current_tenant', %s, false)",
        [str(organization_id)],
    )
    cursor.execute(
        "SELECT pg_catalog.set_config('app.current_user_id', %s, false)",
        [markers["owner_id"]],
    )

patient = Patient.objects.filter(pk=patient_id).first()
check(patient is not None, "seeded patient missing after upgrade")
if patient is not None:
    check(
        patient.full_name == markers["patient_name"],
        "patient full_name does not decrypt to the seeded value",
    )
    check(
        patient.birth_date == date.fromisoformat(markers["patient_birth_date"]),
        "patient birth_date does not survive the upgrade",
    )

with connection.cursor() as cursor:
    cursor.execute(
        "SELECT count(*) FROM clinic_app.scheduling_appointment WHERE id=%s",
        [appointment_id],
    )
    check(cursor.fetchone()[0] == 1, "seeded appointment missing after upgrade")
    cursor.execute(
        "SELECT count(*) FROM clinic_app.scheduling_availabilityblock "
        "WHERE id=%s AND clinic_id=%s AND practitioner_id=%s "
        "AND retired_at IS NULL AND start_at=%s AND end_at=%s",
        [
            availability_id,
            markers["clinic_id"],
            markers["practitioner_id"],
            "2035-08-03T08:00:00-03:00",
            "2035-08-03T12:00:00-03:00",
        ],
    )
    check(cursor.fetchone()[0] == 1, "seeded availability changed after upgrade")
    cursor.execute(
        "SELECT octet_length(full_name) > 0, "
        "position('Synthetic' in full_name::text) "
        "FROM clinic_app.intake_patient WHERE id=%s",
        [patient_id],
    )
    length, plaintext_at = cursor.fetchone()
    check(
        length and plaintext_at == 0,
        "patient full_name still stores readable plaintext",
    )
    cursor.execute(
        "SELECT count(*) FROM pg_tables t JOIN pg_class c ON c.relname=t.tablename "
        "AND c.relnamespace='clinic_app'::regnamespace "
        "WHERE t.schemaname='clinic_app' AND c.relrowsecurity "
        "AND NOT c.relforcerowsecurity"
    )
    check(
        cursor.fetchone()[0] == 0,
        "tenant tables lost FORCE ROW LEVEL SECURITY",
    )
    cursor.execute(
        "SELECT has_table_privilege('clinic_app', "
        "'clinic_app.comms_integrationoperation', 'SELECT'), "
        "has_table_privilege('clinic_app', "
        "'clinic_app.comms_integrationoperation', 'DELETE')"
    )
    readable, deletable = cursor.fetchone()
    check(readable, "clinic_app lost required SELECT on comms operations")
    check(not deletable, "clinic_app gained forbidden DELETE on comms operations")

# The runtime role must see zero rows under a foreign tenant context.
other_tenant = uuid4()
app_dsn = os.environ["UPGRADE_APP_DATABASE_URL"]
with (
    psycopg.connect(app_dsn) as app_connection,
    app_connection.cursor() as app_cursor,
):
    app_cursor.execute(
        "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
        [str(other_tenant)],
    )
    app_cursor.execute("SELECT count(*) FROM clinic_app.intake_patient")
    foreign_count = app_cursor.fetchone()
    check(
        foreign_count is not None and foreign_count[0] == 0,
        "runtime role can read another tenant's patients after upgrade",
    )
    app_cursor.execute(
        "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
        [str(organization_id)],
    )
    app_cursor.execute("SELECT count(*) FROM clinic_app.intake_patient")
    tenant_count = app_cursor.fetchone()
    check(
        tenant_count is not None and tenant_count[0] >= 1,
        "runtime role lost tenant access after upgrade",
    )

# audit_event is intentionally not readable by the runtime role; count and
# chain verification run as the migration owner inside the same tenant GUC
# (set_config(..., false) above is session-scoped and survives RESET ROLE).
with connection.cursor() as cursor:
    cursor.execute("RESET ROLE")
with connection.cursor() as cursor:
    cursor.execute(
        "SELECT count(*) FROM clinic_app.audit_event "
        "WHERE affected_record_type='upgrade.seed' AND affected_record_id=%s",
        [str(appointment_id)],
    )
    check(
        cursor.fetchone()[0] == markers["audit_events"],
        "seeded audit events missing after upgrade",
    )

try:
    chain = verify_chain(organization_id)
except AuditChainVerificationError as error:
    failures.append(f"audit chain verification failed: {error}")
else:
    check(
        chain.row_count >= markers["audit_events"],
        "audit chain lost rows after upgrade",
    )

failures.extend(verify_ai(markers, required="ai" in required_seeds))
if inbox_fixture is not None and not inbox_fixture.failures:
    inbox = import_module("ops.testing.migration_upgrade_inbox_verify")
    markers["inbox"] = {
        "accounts": inbox_fixture.accounts,
        "manager_id": inbox_fixture.manager_id,
        "cases": {
            case: {
                **keys,
                **(
                    {"base_state": PARITY_STATES[case]} if case in PARITY_STATES else {}
                ),
            }
            for case, keys in inbox_fixture.cases.items()
        },
    }
    failures.extend(inbox.verify_inbox(markers))

if failures:
    for name in failures:
        print(  # noqa: T201 - piped diagnostics
            f"upgrade-check FAILED: {name}", file=sys.stderr
        )
    sys.exit(1)
Path(os.environ["UPGRADE_VERIFIED_FILE"]).write_text(
    json.dumps(
        {
            "schema_version": 1,
            "result": "PASS",
            "base_sha": os.environ["UPGRADE_BASE_SHA"],
            "upgrade_db": database_identity()[0],
            "seeds": required_seeds,
            "database_recreated": os.environ["UPGRADE_DATABASE_RECREATED"] == "1",
        }
    )
    + "\n",
    encoding="utf-8",
)
print("upgrade-check: all assertions passed")  # noqa: T201 - piped diagnostics

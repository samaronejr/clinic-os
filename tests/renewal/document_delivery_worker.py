"""Browser QA worker: real clinic_app boundary for document delivery."""

from __future__ import annotations

import json
import sys
from uuid import UUID

from apps.comms.models import IntegrationOperation
from apps.comms.tasks import execute_operation
from apps.tenancy.db import tenant_context
from django.db import connection


def main() -> None:
    operation_id = UUID(sys.argv[1])
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_user")
        assert cursor.fetchone() == ("clinic_app",)
        cursor.execute(
            "SELECT * FROM clinic_app.comms_operation_scope(%s)", [operation_id]
        )
        organization_id, _, actor_id = cursor.fetchone()
    with tenant_context(actor_id, organization_id):
        operation = IntegrationOperation.objects.get(pk=operation_id)
    assert operation.subject_type == "prescription.document"
    results = [execute_operation(operation_id=str(operation_id))]
    sys.stdout.write(
        json.dumps(
            {"runtime_role": "clinic_app", "outcomes": results, "synthetic": True}
        )
        + "\n"
    )


if __name__ == "__main__":
    main()

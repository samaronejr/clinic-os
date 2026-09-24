"""Browser QA worker: real clinic_app boundary, explicit synthetic clock/provider."""

from __future__ import annotations

import hashlib
import hmac
import json
import sys
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID

from apps.comms.models import IntegrationOperation
from apps.comms.tasks import execute_operation
from apps.core.integration import (
    receive_provider_callback,
    register_callback_authenticator,
)
from apps.tenancy.db import tenant_context
from django.db import connection
from django.test import override_settings

from renewal.test_integration_boundary import (
    SECRET,
    SyntheticAuthenticator,
)


def main() -> None:
    operation_id, outcome = UUID(sys.argv[1]), sys.argv[2]
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_user")
        assert cursor.fetchone() == ("clinic_app",)
        cursor.execute(
            "SELECT * FROM clinic_app.comms_operation_scope(%s)", [operation_id]
        )
        organization_id, _, actor_id = cursor.fetchone()
    with tenant_context(actor_id, organization_id):
        operation = IntegrationOperation.objects.get(pk=operation_id)
    clock = SimpleNamespace(now=lambda: operation.not_before)
    with (
        override_settings(
            COMMS_SYNTHETIC_CHANNELS=("email", "sms", "whatsapp"),
            COMMS_SYNTHETIC_FAILURE_CHANNELS=(operation.channel,)
            if outcome == "fail"
            else (),
        ),
        patch("apps.core.integration.timezone", clock),
    ):
        results = [execute_operation(operation_id=str(operation_id))]
        if outcome == "fail":
            results.extend(
                execute_operation(operation_id=str(operation_id)) for _ in range(3)
            )
        if outcome == "delivered":
            with tenant_context(actor_id, organization_id):
                operation.refresh_from_db()
            authenticator = SyntheticAuthenticator()
            authenticator.provider = operation.provider
            register_callback_authenticator(authenticator)
            body = json.dumps(
                {
                    "event_id": f"synthetic-receipt-{operation_id}",
                    "provider_reference": operation.provider_reference,
                    "status": "delivered",
                }
            ).encode()
            headers = {
                "x-synthetic-signature": hmac.new(
                    SECRET, body, hashlib.sha256
                ).hexdigest()
            }
            results.append(
                receive_provider_callback(
                    provider=operation.provider, headers=headers, body=body
                )
            )
    sys.stdout.write(
        json.dumps(
            {"runtime_role": "clinic_app", "outcomes": results, "synthetic": True}
        )
        + "\n"
    )


if __name__ == "__main__":
    main()

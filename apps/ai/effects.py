"""Retained output keeps its original permission at every human effect."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from django.db import connection

from apps.ai.contracts import GatewayAccessDeniedError, GatewayRefusedError, Purpose
from apps.ai.evaluation.versions import source_versions
from apps.ai.models import ModelConfiguration
from apps.ai.policy import check_configuration, check_rollout_binding, enabled_policy
from apps.core.secrets import secret_store
from apps.tenancy.envelope import KEK_SECRET_NAME, protect

if TYPE_CHECKING:
    from uuid import UUID


def check_invocation(*, clinic_id: UUID, invocation_id: UUID) -> None:
    """Recheck the pinned source and authenticate its current transaction to SQL."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT * FROM clinic_app.ai_effect_source(%s,%s)",
            [invocation_id, clinic_id],
        )
        source = cursor.fetchone()
    if source is None:
        raise GatewayAccessDeniedError
    configuration = ModelConfiguration.objects.get(pk=source[0])
    purpose = Purpose(source[1])
    enabled_policy(purpose)
    check_configuration(configuration, clinic_id)
    check_rollout_binding(clinic_id=clinic_id, approval_id=source[2])
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_current_xact_id()::text")
        transaction_id = cursor.fetchone()[0]
    receipt = protect(
        purpose="ai.effect.sources",
        plaintext=json.dumps(
            {
                "subject": str(invocation_id),
                "transaction": transaction_id,
                "sources": source_versions(purpose),
            },
            separators=(",", ":"),
        ).encode(),
    )
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.ai_authorize_effect(%s,%s,%s,%s)",
            [
                invocation_id,
                clinic_id,
                receipt,
                secret_store().get_secret(KEK_SECRET_NAME),
            ],
        )
        if cursor.fetchone() != (True,):
            raise GatewayRefusedError(reason="configuration_unavailable")

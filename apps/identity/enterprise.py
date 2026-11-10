"""Organization-approved federation and hash-only SCIM credential lifecycle."""

from __future__ import annotations

import hashlib
import re
import secrets
from datetime import timedelta
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from django.db import connection, transaction
from django.db.models.functions import Now
from django.utils import timezone

from apps.audit.services import record_phase1_event
from apps.identity.enterprise_authority import (
    GovernanceDeniedError,
    admin_scope,
    require_synthetic_identity,
)
from apps.identity.models import (
    ScimResource,
    ScimRoleMapping,
    ScimToken,
    SsoConnection,
    UnitGovernanceChange,
    UserClinicRole,
)
from apps.providers.services import current_version

if TYPE_CHECKING:
    from uuid import UUID

__all__ = (
    "GovernanceDeniedError",
    "approve_group_mapping",
    "configure_connection",
    "issue_scim_token",
    "revoke_scim_token",
)


def configure_connection(
    *, clinic_id: UUID, issuer: str, client_id: str, session_key: str
) -> SsoConnection:
    """Append a version; configuration never imports roles or grants a factor."""
    require_synthetic_identity()
    parsed = urlsplit(issuer)
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or not parsed.hostname.endswith(".invalid")
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", client_id) is None
    ):
        raise GovernanceDeniedError
    with (
        transaction.atomic(),
        admin_scope(clinic_id=clinic_id, session_key=session_key) as (actor, clinic),
    ):
        version = current_version("enterprise_identity", clinic_id=clinic_id)
        if (
            version is None
            or version.provider != "deterministic-oidc"
            or version.environment != "synthetic"
            or version.state not in {"researched", "selected_in_plan"}
        ):
            raise GovernanceDeniedError
        for previous in SsoConnection.objects.select_for_update().filter(
            organization_id=clinic.organization_id, revoked_at__isnull=True
        ):
            previous.revoked_at = timezone.now()
            previous.save(update_fields=("revoked_at",))
            record_phase1_event(
                "identity.sso.revoked",
                clinic_id=previous.clinic_id,
                affected_record_id=previous.pk,
            )
        result = SsoConnection.objects.create(
            organization_id=clinic.organization_id,
            clinic_id=clinic_id,
            created_by_id=actor,
            provider_key="enterprise_identity",
            configuration={
                "issuer": issuer.rstrip("/"),
                "client_id": client_id,
                "subject_source": "externalId",
                "transport": "synthetic",
            },
        )
        record_phase1_event(
            "identity.sso.configured", clinic_id=clinic_id, affected_record_id=result.pk
        )
        if not UnitGovernanceChange.objects.filter(
            clinic_id=clinic_id, kind="recovery_exercise"
        ).exists():
            slot = UnitGovernanceChange.objects.create(
                organization_id=clinic.organization_id,
                clinic_id=clinic_id,
                kind="recovery_exercise",
                references={"status": "awaiting_todo_72"},
                created_by_id=actor,
            )
            record_phase1_event(
                "identity.recovery.slot_recorded",
                clinic_id=clinic_id,
                affected_record_id=slot.pk,
            )
        return result


def issue_scim_token(*, clinic_id: UUID, connection_id: UUID, session_key: str) -> str:
    """Return a new high-entropy bearer once; only its SHA-256 is persisted."""
    require_synthetic_identity()
    with (
        transaction.atomic(),
        admin_scope(clinic_id=clinic_id, session_key=session_key) as (actor, clinic),
    ):
        target = SsoConnection.objects.filter(
            pk=connection_id, clinic_id=clinic_id, revoked_at__isnull=True
        ).first()
        if target is None:
            raise GovernanceDeniedError
        secret = secrets.token_urlsafe(32)
        token = ScimToken.objects.create(
            organization_id=clinic.organization_id,
            clinic_id=clinic_id,
            connection=target,
            secret_hash=hashlib.sha256(secret.encode("ascii")).digest(),
            issued_by_id=actor,
            expires_at=timezone.now() + timedelta(days=30),
            window_start=timezone.now(),
        )
        record_phase1_event(
            "identity.scim.token_issued",
            clinic_id=clinic_id,
            affected_record_id=token.pk,
        )
        return secret


def revoke_scim_token(*, clinic_id: UUID, token_id: UUID, session_key: str) -> None:
    """Revoke immediately and idempotently, including after the token expired."""
    with (
        transaction.atomic(),
        admin_scope(clinic_id=clinic_id, session_key=session_key),
    ):
        token = (
            ScimToken.objects.select_for_update()
            .filter(pk=token_id, clinic_id=clinic_id)
            .first()
        )
        if token is None:
            raise GovernanceDeniedError
        if token.revoked_at is None:
            ScimToken.objects.filter(pk=token.pk).update(revoked_at=Now())
            record_phase1_event(
                "identity.scim.token_revoked",
                clinic_id=clinic_id,
                affected_record_id=token.pk,
            )


def approve_group_mapping(
    *, clinic_id: UUID, group_id: UUID, unit_id: UUID, bundle: str, session_key: str
) -> ScimRoleMapping:
    """Approve one explicit group/unit/bundle; IdP claims cannot call this boundary."""
    with (
        transaction.atomic(),
        admin_scope(clinic_id=clinic_id, session_key=session_key) as (actor, clinic),
    ):
        group = ScimResource.objects.filter(
            pk=group_id, clinic_id=clinic_id, kind="Groups", deleted_at__isnull=True
        ).first()
        if (
            group is None
            or bundle not in UserClinicRole.Role.values
            or not type(clinic)
            .objects.filter(pk=unit_id, organization_id=clinic.organization_id)
            .exists()
        ):
            raise GovernanceDeniedError
        result, created = ScimRoleMapping.objects.get_or_create(
            group=group,
            unit_id=unit_id,
            bundle=bundle,
            revoked_at__isnull=True,
            defaults={
                "clinic_id": clinic_id,
                "organization_id": clinic.organization_id,
                "approved_by_id": actor,
            },
        )
        if created:
            record_phase1_event(
                "identity.scim.mapping_approved",
                clinic_id=clinic_id,
                affected_record_id=result.pk,
            )
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM clinic_app.enterprise_sync_directory(%s)",
                    [group.connection_id],
                )
                changes = cursor.fetchall()
            for binding_id, unit_clinic_id, verb in changes:
                record_phase1_event(
                    f"identity.scim.binding_{verb}",
                    clinic_id=unit_clinic_id,
                    affected_record_id=binding_id,
                )
        return result

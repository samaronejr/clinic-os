"""Unit membership, immutable template propagation and directory key rotation."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from uuid import UUID

from django.db import connection, transaction
from django.db.models.functions import Now

from apps.audit.services import record_phase1_event
from apps.ehr.models import SpecialtyTemplate
from apps.ehr.template_schema import validate_template
from apps.identity.enterprise import issue_scim_token
from apps.identity.enterprise_authority import GovernanceDeniedError, admin_scope
from apps.identity.models import ScimToken, SsoConnection, UnitGovernanceChange

if TYPE_CHECKING:
    from collections.abc import Mapping


def _receipt(
    *, clinic_id: UUID, command_id: UUID, kind: str, references: Mapping[str, object]
) -> UnitGovernanceChange | None:
    existing = UnitGovernanceChange.objects.filter(
        pk=command_id, clinic_id=clinic_id
    ).first()
    if existing is not None and (
        existing.kind != kind or existing.references != dict(references)
    ):
        raise GovernanceDeniedError
    return existing


def revoke_unit_access(
    *, clinic_id: UUID, user_id: UUID, command_id: UUID, session_key: str
) -> None:
    """Revoke canonical unit membership, not a parallel authorization overlay."""
    terms = {"user_id": str(user_id)}
    with (
        transaction.atomic(),
        admin_scope(clinic_id=clinic_id, session_key=session_key) as (actor, clinic),
    ):
        if (
            _receipt(
                clinic_id=clinic_id,
                command_id=command_id,
                kind="unit_membership",
                references=terms,
            )
            is not None
        ):
            return
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT clinic_app.enterprise_unit_member_revoke(%s,%s)",
                [clinic_id, user_id],
            )
            row = cursor.fetchone()
            if row is None or row[0] == 0:
                raise GovernanceDeniedError
        receipt = UnitGovernanceChange.objects.create(
            id=command_id,
            clinic_id=clinic_id,
            organization_id=clinic.organization_id,
            kind="unit_membership",
            references=terms,
            created_by_id=actor,
            target_user_id=user_id,
            access_denied=True,
            policy_version=1,
        )
        record_phase1_event(
            "identity.unit_policy.changed",
            clinic_id=clinic_id,
            affected_record_id=receipt.pk,
        )


def propagate_template(
    *,
    clinic_id: UUID,
    source_id: UUID,
    unit_id: UUID,
    command_id: UUID,
    session_key: str,
) -> UUID:
    """Append a new unit version; existing drafts and signed bytes never change."""
    terms = {"source_id": str(source_id), "unit_id": str(unit_id)}
    with (
        transaction.atomic(),
        admin_scope(clinic_id=clinic_id, session_key=session_key) as (actor, clinic),
    ):
        existing = UnitGovernanceChange.objects.filter(
            pk=command_id, clinic_id=clinic_id
        ).first()
        if existing is not None:
            if (
                existing.kind != "template_propagation"
                or {key: existing.references.get(key) for key in terms} != terms
            ):
                raise GovernanceDeniedError
            return UUID(existing.references["published_id"])
        source = SpecialtyTemplate.objects.filter(
            pk=source_id, clinic_id=clinic_id
        ).first()
        if source is None or unit_id == clinic_id:
            raise GovernanceDeniedError
        validate_template(source.prompts, clinician_id=actor)
        if any(
            phrase.get("scope") == "clinician"
            for phrase in source.prompts.get("phrases", [])
        ):
            # Personal phrases cannot silently become another unit's clinic template.
            raise GovernanceDeniedError
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT clinic_app.enterprise_template_propagate(%s,%s)",
                [source_id, unit_id],
            )
            row = cursor.fetchone()
            if row is None or row[0] is None:
                raise GovernanceDeniedError
            published_id = cast("UUID", row[0])
        receipt = UnitGovernanceChange.objects.create(
            id=command_id,
            organization_id=clinic.organization_id,
            clinic_id=clinic_id,
            kind="template_propagation",
            references={**terms, "published_id": str(published_id)},
            created_by_id=actor,
        )
        record_phase1_event(
            "identity.unit_template.propagated",
            clinic_id=clinic_id,
            affected_record_id=receipt.pk,
        )
        return published_id


def rotate_scim_credentials(
    *, clinic_id: UUID, connection_id: UUID, session_key: str
) -> str:
    """Rotate the governed directory credential; no provider lifecycle is activated."""
    with (
        transaction.atomic(),
        admin_scope(clinic_id=clinic_id, session_key=session_key) as (actor, clinic),
    ):
        target = (
            SsoConnection.objects.select_for_update()
            .filter(pk=connection_id, clinic_id=clinic_id, revoked_at__isnull=True)
            .first()
        )
        if target is None:
            raise GovernanceDeniedError
        old_ids = tuple(
            ScimToken.objects.filter(
                connection=target, revoked_at__isnull=True
            ).values_list("id", flat=True)
        )
        for token_id in old_ids:
            ScimToken.objects.filter(pk=token_id).update(revoked_at=Now())
            record_phase1_event(
                "identity.scim.token_revoked",
                clinic_id=clinic_id,
                affected_record_id=token_id,
            )
        secret = issue_scim_token(
            clinic_id=clinic_id, connection_id=connection_id, session_key=session_key
        )
        receipt = UnitGovernanceChange.objects.create(
            organization_id=clinic.organization_id,
            clinic_id=clinic_id,
            kind="credential_rotation",
            created_by_id=actor,
            references={
                "connection_id": str(connection_id),
                "provider_key": target.provider_key,
                "revoked_ids": [str(value) for value in old_ids],
            },
        )
        record_phase1_event(
            "identity.credential.rotated",
            clinic_id=clinic_id,
            affected_record_id=receipt.pk,
        )
        return secret

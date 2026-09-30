"""Audited owner provisioning of machine identities; no runtime write grants.

Use the owner lifecycle's authenticated operator transaction. Labels are
machine slugs, never personal names, prompts, credentials or clinical data.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import transaction

from apps.audit.services import record_phase1_event
from apps.identity.current_context import CurrentActorError
from apps.identity.models import ServicePrincipal, ServicePrincipalGrant
from apps.identity.scope_provisioning import _owner_clinic

if TYPE_CHECKING:
    from uuid import UUID

_DENIED = "current actor unauthorized"


@transaction.atomic
def register_principal(
    *, clinic_id: UUID, name: str, db_identity: str, purpose: str
) -> ServicePrincipal:
    """Bind one database login to one immutable principal in the authorized clinic."""
    clinic = _owner_clinic(clinic_id)
    principal = ServicePrincipal.objects.create(
        organization_id=clinic.organization_id,
        clinic=clinic,
        name=name,
        db_identity=db_identity,
        purpose=purpose,
    )
    record_phase1_event(
        "identity.principal.created",
        clinic_id=clinic_id,
        affected_record_id=principal.pk,
    )
    return principal


@transaction.atomic
def grant_principal(
    *, clinic_id: UUID, principal_id: UUID, permission: str, subject_scope: str
) -> ServicePrincipalGrant:
    """Append a v1 scope, never reactivating an old grant or a revoked principal."""
    _owner_clinic(clinic_id)
    principal = (
        ServicePrincipal.objects.select_for_update()
        .filter(pk=principal_id, clinic_id=clinic_id, active=True)
        .first()
    )
    if principal is None:
        raise CurrentActorError(_DENIED)
    grant = ServicePrincipalGrant.objects.create(
        organization_id=principal.organization_id,
        principal=principal,
        permission=permission,
        subject_scope=subject_scope,
    )
    record_phase1_event(
        "identity.principal_grant.created",
        clinic_id=clinic_id,
        affected_record_id=grant.pk,
    )
    return grant


@transaction.atomic
def revoke_principal(*, clinic_id: UUID, principal_id: UUID) -> ServicePrincipal:
    """Revoke irreversibly and idempotently; RLS observes the committed change."""
    _owner_clinic(clinic_id)
    principal = (
        ServicePrincipal.objects.select_for_update()
        .filter(pk=principal_id, clinic_id=clinic_id)
        .first()
    )
    if principal is None:
        raise CurrentActorError(_DENIED)
    if principal.active:
        principal.active = False
        principal.save(update_fields=("active",))
        record_phase1_event(
            "identity.principal.revoked",
            clinic_id=clinic_id,
            affected_record_id=principal.pk,
        )
    return principal


@transaction.atomic
def revoke_principal_grant(*, clinic_id: UUID, grant_id: UUID) -> ServicePrincipalGrant:
    """Revoke one grant without deleting its attributable authority history."""
    _owner_clinic(clinic_id)
    grant = (
        ServicePrincipalGrant.objects.select_for_update()
        .filter(pk=grant_id, principal__clinic_id=clinic_id)
        .first()
    )
    if grant is None:
        raise CurrentActorError(_DENIED)
    if grant.active:
        grant.active = False
        grant.save(update_fields=("active",))
        record_phase1_event(
            "identity.principal_grant.revoked",
            clinic_id=clinic_id,
            affected_record_id=grant.pk,
        )
    return grant

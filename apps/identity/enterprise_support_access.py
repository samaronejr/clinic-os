"""Customer-approved support sees a fixed masked projection, never staff tables."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, TypedDict

from django.db import connection, transaction
from django.db.models import Q
from django.db.models.functions import Now
from django.utils import timezone

from apps.audit.services import record_phase1_event
from apps.identity import enterprise_authority as clock
from apps.identity.enterprise_authority import GovernanceDeniedError, admin_scope
from apps.identity.models import SupportAccessGrant
from apps.identity.session_inventory import session_hash
from apps.providers.models import ProviderCapability
from apps.tenancy.db import clear_connection_tenant_gucs

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

MAX_SUPPORT_PURPOSE = 512


class MaskedProvider(TypedDict):
    """Only fixed metadata reaches diagnostics; never accounts, payloads or names."""

    capability: str
    state: str


@dataclass(frozen=True, slots=True)
class SupportSnapshot:
    """A read-only diagnostic result has no clinical data or mutation capability."""

    providers: tuple[MaskedProvider, ...]
    masked: bool = True
    clinical_write: bool = False


def approve_support_access(
    *,
    clinic_id: UUID,
    support_user_id: UUID,
    purpose: str,
    expires_at: datetime,
    session_key: str,
) -> SupportAccessGrant:
    """Grant at most four hours to a named support user under customer approval."""
    if (
        not isinstance(purpose, str)
        or not 1 <= len(purpose.strip()) <= MAX_SUPPORT_PURPOSE
        or timezone.is_naive(expires_at)
        or not clock.utc_now() < expires_at <= clock.utc_now() + timedelta(hours=4)
    ):
        raise GovernanceDeniedError
    with (
        transaction.atomic(),
        admin_scope(clinic_id=clinic_id, session_key=session_key) as (actor, clinic),
    ):
        result = SupportAccessGrant.objects.create(
            organization_id=clinic.organization_id,
            clinic_id=clinic_id,
            support_user_id=support_user_id,
            approved_by_id=actor,
            purpose=purpose.strip(),
            expires_at=expires_at,
        )
        record_phase1_event(
            "identity.support.approved",
            clinic_id=clinic_id,
            affected_record_id=result.pk,
        )
        return result


def support_diagnostics(
    *, clinic_id: UUID, grant_id: UUID, session_key: str
) -> SupportSnapshot:
    """Authenticate the stored session/grant without entering a staff tenant context."""
    if connection.in_atomic_block:
        raise GovernanceDeniedError
    clear_connection_tenant_gucs()
    try:
        with transaction.atomic(durable=True), connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM clinic_app.enterprise_support_scope(%s,%s,%s)",
                [grant_id, clinic_id, session_hash(session_key)],
            )
            row = cursor.fetchone()
            if row is None or clock.utc_now() >= row[2]:
                raise GovernanceDeniedError
            organization_id, support_user_id, _expires_at = row
            cursor.execute(
                "SELECT set_config('app.current_tenant',%s,true),"
                "set_config('app.current_support_grant',%s,true),"
                "set_config('app.current_user_id','',true)",
                [str(organization_id), str(grant_id)],
            )
            providers = tuple(
                MaskedProvider(
                    capability=provider.key,
                    state=(
                        provider.current_version.state
                        if provider.current_version is not None
                        else "unavailable"
                    ),
                )
                for provider in ProviderCapability.objects.filter(
                    Q(clinic_id=clinic_id) | Q(clinic_id__isnull=True)
                )
                .select_related("current_version")
                .order_by("key", "id")
            )
            # Attribute this fixed audit append to the authenticated named user.
            # No staff service executes while that audit-only GUC is populated.
            cursor.execute(
                "SELECT set_config('app.current_user_id',%s,true)",
                [str(support_user_id)],
            )
            record_phase1_event(
                "identity.support.read",
                clinic_id=clinic_id,
                affected_record_id=grant_id,
            )
            cursor.execute("SELECT set_config('app.current_user_id','',true)")
            return SupportSnapshot(providers)
    finally:
        clear_connection_tenant_gucs()


def revoke_support_access(*, clinic_id: UUID, grant_id: UUID, session_key: str) -> None:
    """Revoke customer-approved access irreversibly at the next request."""
    with (
        transaction.atomic(),
        admin_scope(clinic_id=clinic_id, session_key=session_key),
    ):
        grant = (
            SupportAccessGrant.objects.select_for_update()
            .filter(pk=grant_id, clinic_id=clinic_id)
            .first()
        )
        if grant is None:
            raise GovernanceDeniedError
        if grant.revoked_at is None:
            SupportAccessGrant.objects.filter(pk=grant.pk).update(revoked_at=Now())
            record_phase1_event(
                "identity.support.revoked",
                clinic_id=clinic_id,
                affected_record_id=grant.pk,
            )

"""Install immutable clinic bundles and retire future use without deleting history."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from django.core.exceptions import ValidationError
from django.db import connection
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.audit.services import record_phase1_event
from apps.ehr.pack_catalog import (
    LICENSE_REF,
    PACKS,
    questionnaire_definition,
    template_definition,
)
from apps.ehr.pack_models import SpecialtyPack
from apps.ehr.services import (
    ClinicalAccessDeniedError,
    ClinicalConflictError,
    decided,
    publish_template,
)
from apps.identity.current_context import require_current_actor_clinic_roles
from apps.identity.models import Clinic, UserClinicRole
from apps.intake.questionnaires import publish_template as publish_questionnaire

if TYPE_CHECKING:
    from uuid import UUID

ADMIN_ROLES = (UserClinicRole.Role.OWNER, UserClinicRole.Role.CLINIC_ADMIN)
READ_ROLES = (*ADMIN_ROLES, UserClinicRole.Role.PHYSICIAN)


def lock_catalog(*, clinic_id: UUID) -> None:
    """Serialize all authorized clinic install, retire and reorder decisions."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            [f"ehr-pack-catalog:{clinic_id}"],
        )


def install_pack(*, clinic_id: UUID, pack_key: str, version: int) -> SpecialtyPack:
    """Record the installing administrator as the local content steward."""
    authority = partial(require_current_actor_clinic_roles, clinic_id, ADMIN_ROLES)
    actor = authority()
    definition = PACKS.get(pack_key)
    if definition is None or type(version) is not int or version != definition.version:
        raise ValidationError(_("Choose a published specialty pack version."))
    with decided(authority):
        lock_catalog(clinic_id=clinic_id)
        previous = SpecialtyPack.objects.filter(
            clinic_id=clinic_id, key=pack_key, version=version
        ).first()
        if previous is not None:
            if previous.uninstalled_at is not None:
                reason = "pack_retired"
                raise ClinicalConflictError(reason)
            return previous
        clinic = Clinic.objects.get(pk=clinic_id)
        key = f"pack_{pack_key}_v{version}"
        template = publish_template(
            clinic_id=clinic_id,
            key=key,
            title=str(definition.title),
            prompts=template_definition(definition),
        )
        questionnaire = publish_questionnaire(
            clinic_id=clinic_id,
            key=key,
            title=str(definition.title),
            questions=questionnaire_definition(definition),
        )
        pack = SpecialtyPack.objects.create(
            organization_id=clinic.organization_id,
            clinic_id=clinic_id,
            key=pack_key,
            version=version,
            position=SpecialtyPack.objects.filter(clinic_id=clinic_id).count() + 1,
            template=template,
            questionnaire=questionnaire,
            calculators=[
                {"key": calculator, "version": 1}
                for calculator in definition.calculators
            ],
            license_ref=LICENSE_REF,
            clinical_owner_id=actor,
        )
        record_phase1_event(
            "ehr.pack.installed", clinic_id=clinic_id, affected_record_id=pack.pk
        )
        return pack


def uninstall_pack(*, clinic_id: UUID, pack_key: str, version: int) -> SpecialtyPack:
    """Retirement is terminal, idempotent and does not alter templates or notes."""
    authority = partial(require_current_actor_clinic_roles, clinic_id, ADMIN_ROLES)
    authority()
    with decided(authority):
        lock_catalog(clinic_id=clinic_id)
        pack = (
            SpecialtyPack.objects.select_for_update()
            .filter(clinic_id=clinic_id, key=pack_key, version=version)
            .first()
        )
        if pack is None:
            raise ClinicalAccessDeniedError
        if pack.uninstalled_at is None:
            pack.uninstalled_at = timezone.now()
            pack.save(update_fields=("uninstalled_at",))
            record_phase1_event(
                "ehr.pack.uninstalled", clinic_id=clinic_id, affected_record_id=pack.pk
            )
        return pack


def installed_packs(*, clinic_id: UUID) -> tuple[SpecialtyPack, ...]:
    """List future-use bundles in the clinic administrator's stored order."""
    require_current_actor_clinic_roles(clinic_id, READ_ROLES)
    return tuple(
        SpecialtyPack.objects.filter(
            clinic_id=clinic_id, uninstalled_at__isnull=True
        ).order_by("position", "key", "version")
    )


def reorder_packs(
    *, clinic_id: UUID, pack_keys: tuple[str, ...]
) -> tuple[SpecialtyPack, ...]:
    """Require a complete, duplicate-free order of this clinic's active packs."""
    authority = partial(require_current_actor_clinic_roles, clinic_id, ADMIN_ROLES)
    authority()
    with decided(authority):
        lock_catalog(clinic_id=clinic_id)
        rows = {row.key: row for row in installed_packs(clinic_id=clinic_id)}
        if len(pack_keys) != len(rows) or set(pack_keys) != set(rows):
            raise ValidationError(_("Choose each installed pack exactly once."))
        changed = False
        for position, key in enumerate(pack_keys, 1):
            row = rows[key]
            if row.position != position:
                row.position = position
                row.save(update_fields=("position",))
                changed = True
        if changed:
            record_phase1_event(
                "ehr.pack.reordered", clinic_id=clinic_id, affected_record_id=clinic_id
            )
        return tuple(rows[key] for key in pack_keys)

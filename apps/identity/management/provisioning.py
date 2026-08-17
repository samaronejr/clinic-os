"""Owner-only staff provisioning transaction."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import Field, Q

from apps.audit.services import record_phase1_event
from apps.identity.identifiers import canonicalize_email, canonicalize_username
from apps.identity.management.base import LifecycleCommandError
from apps.identity.management.context import (
    LifecycleContext,
    assume_runtime_owner,
    scoped_owner_gucs,
)
from apps.identity.models import Clinic, User, UserClinicRole
from apps.scheduling.locks import (
    acquire_advisory_locks,
    clinic_lock_key,
    identity_lock_keys,
    user_lock_keys,
)

if TYPE_CHECKING:
    from uuid import UUID


@dataclass(frozen=True, slots=True)
class ProvisionStaffRequest:
    """Canonical staff identity and exact initial clinic role input."""

    user_id: UUID
    username: str
    email: str
    role: UserClinicRole.Role


def _validated_user(request: ProvisionStaffRequest, raw_password: str) -> User:
    username = canonicalize_username(request.username)
    email = canonicalize_email(request.email)
    user = User(id=request.user_id, username=username, email=email)
    try:
        username_field = User._meta.get_field("username")  # noqa: SLF001
        email_field = User._meta.get_field("email")  # noqa: SLF001
        if not isinstance(username_field, Field) or not isinstance(email_field, Field):
            raise LifecycleCommandError
        username_field.clean(username, user)
        email_field.clean(email, user)
        validate_email(email)
        validate_password(raw_password, user)
    except ValidationError as error:
        raise LifecycleCommandError from error
    user.set_password(raw_password)
    return user


def provision_staff(
    context: LifecycleContext,
    request: ProvisionStaffRequest,
    raw_password: str,
) -> None:
    """Create one new identity, one role, and one tenant audit event."""
    username = canonicalize_username(request.username)
    email = canonicalize_email(request.email)
    keys = (
        *identity_lock_keys(username, email),
        clinic_lock_key(context.clinic_id),
        *user_lock_keys((request.user_id,)),
    )
    with transaction.atomic(), scoped_owner_gucs(context):
        acquire_advisory_locks(keys)
        with assume_runtime_owner(context):
            pass
        clinic = Clinic.objects.filter(
            pk=context.clinic_id,
            organization_id=context.organization_id,
        ).first()
        if (
            clinic is None
            or User.objects.filter(
                Q(pk=request.user_id) | Q(username=username) | Q(email=email)
            ).exists()
        ):
            raise LifecycleCommandError
        user = _validated_user(request, raw_password)
        user.save(force_insert=True)
        role = UserClinicRole.objects.create(
            user=user,
            organization_id=context.organization_id,
            clinic=clinic,
            role=request.role,
        )
        record_phase1_event(
            "identity.staff.provisioned",
            clinic_id=clinic.pk,
            affected_record_id=role.pk,
        )

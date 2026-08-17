"""Actorless first-clinic bootstrap transaction."""

from __future__ import annotations

import re
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
from apps.identity.management.context import scoped_lifecycle_gucs
from apps.identity.models import Clinic, Organization, User, UserClinicRole
from apps.scheduling.locks import (
    acquire_advisory_locks,
    clinic_lock_key,
    identity_lock_keys,
    user_lock_keys,
)
from apps.scheduling.timezones import validate_iana_timezone

if TYPE_CHECKING:
    from uuid import UUID

BRAZILIAN_UFS = frozenset(
    [
        "AC",
        "AL",
        "AP",
        "AM",
        "BA",
        "CE",
        "DF",
        "ES",
        "GO",
        "MA",
        "MT",
        "MS",
        "MG",
        "PA",
        "PB",
        "PR",
        "PE",
        "PI",
        "RJ",
        "RN",
        "RS",
        "RO",
        "RR",
        "SC",
        "SP",
        "SE",
        "TO",
    ]
)
NAME_MAX_LENGTH = 255


@dataclass(frozen=True, slots=True)
class BootstrapRequest:
    """Carry explicit first-clinic and first-owner identifiers."""

    organization_id: UUID
    organization_name: str
    cnpj: str
    clinic_id: UUID
    clinic_name: str
    crm_uf: str
    timezone: str
    owner_user_id: UUID
    owner_username: str
    owner_email: str


def _validated_user(request: BootstrapRequest, raw_password: str) -> User:
    username = canonicalize_username(request.owner_username)
    email = canonicalize_email(request.owner_email)
    user = User(id=request.owner_user_id, username=username, email=email)
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


def _validate_request(request: BootstrapRequest) -> None:
    if (
        not request.organization_name.strip()
        or len(request.organization_name) > NAME_MAX_LENGTH
        or not request.clinic_name.strip()
        or len(request.clinic_name) > NAME_MAX_LENGTH
        or re.fullmatch(r"[0-9]{14}", request.cnpj) is None
        or request.crm_uf not in BRAZILIAN_UFS
    ):
        raise LifecycleCommandError
    try:
        validate_iana_timezone(request.timezone)
    except ValidationError as error:
        raise LifecycleCommandError from error


def bootstrap_clinic(request: BootstrapRequest, raw_password: str) -> None:
    """Create the first owner and append one actorless system event."""
    _validate_request(request)
    username = canonicalize_username(request.owner_username)
    email = canonicalize_email(request.owner_email)
    keys = (
        *identity_lock_keys(username, email),
        clinic_lock_key(request.clinic_id),
        *user_lock_keys((request.owner_user_id,)),
    )
    with transaction.atomic(), scoped_lifecycle_gucs(request.organization_id, None):
        acquire_advisory_locks(keys)
        if (
            Organization.objects.filter(pk=request.organization_id).exists()
            or Clinic.objects.filter(pk=request.clinic_id).exists()
            or User.objects.filter(
                Q(pk=request.owner_user_id) | Q(username=username) | Q(email=email)
            ).exists()
        ):
            raise LifecycleCommandError
        user = _validated_user(request, raw_password)
        organization = Organization.objects.create(
            id=request.organization_id,
            name=request.organization_name.strip(),
            cnpj=request.cnpj,
        )
        clinic = Clinic.objects.create(
            id=request.clinic_id,
            organization=organization,
            name=request.clinic_name.strip(),
            crm_uf=request.crm_uf,
            timezone=request.timezone,
        )
        user.save(force_insert=True)
        UserClinicRole.objects.create(
            user=user,
            organization=organization,
            clinic=clinic,
            role=UserClinicRole.Role.OWNER,
        )
        record_phase1_event(
            "ops.clinic.bootstrapped",
            clinic_id=clinic.pk,
            affected_record_id=organization.pk,
        )

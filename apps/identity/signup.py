"""Purpose-bound, protected self-signup without runtime membership or a trial."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass, field
from typing import Final
from uuid import UUID, uuid4

import rfc8785
from django.contrib.auth.hashers import make_password
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.validators import validate_email
from django.db import DatabaseError, connection, transaction
from django.views.decorators.debug import sensitive_variables

from apps.audit.models import SYSTEM_ORG_ID
from apps.audit.services import record_phase1_event
from apps.identity.cnpj import validate_cnpj
from apps.identity.identifiers import canonicalize_email, canonicalize_username
from apps.identity.management.context import scoped_lifecycle_gucs
from apps.identity.models import Organization, SignupRegistration, User
from apps.tenancy.envelope import _kek

NAME_LIMIT: Final = 255
USERNAME_LIMIT: Final = 150
EMAIL_LIMIT: Final = 254
REGISTRATION_LIMIT: Final = 512
SIGNUP_RESULT_COLUMNS: Final = 3


class SignupDeniedError(PermissionDenied):
    """Refuse malformed or conflicting signup without reflecting identity."""


@dataclass(frozen=True, slots=True)
class SignupCapture:
    """Untrusted public form values, parsed once at the signup boundary."""

    organization_name: str
    cnpj: str
    legal_responsible: str
    username: str
    email: str
    password: str = field(repr=False)
    professional_registration: str = ""


@dataclass(frozen=True, slots=True)
class SignupReceipt:
    """Opaque pending identifiers; never an activation or login receipt."""

    organization_id: UUID
    first_admin_id: UUID
    created: bool


@sensitive_variables()
def _payload(capture: SignupCapture) -> dict[str, str]:
    """Parse the capture while leaving existing owner bootstrap rules intact."""
    organization_name = capture.organization_name.strip()
    legal_responsible = capture.legal_responsible.strip()
    username = canonicalize_username(capture.username)
    email = canonicalize_email(capture.email)
    registration = capture.professional_registration.strip()
    if (
        not 1 <= len(organization_name) <= NAME_LIMIT
        or not 1 <= len(legal_responsible) <= NAME_LIMIT
        or not 1 <= len(username) <= USERNAME_LIMIT
        or not 1 <= len(email) <= EMAIL_LIMIT
        or len(registration) > REGISTRATION_LIMIT
    ):
        raise SignupDeniedError
    candidate = User(username=username, email=email)
    try:
        candidate.username_validator(username)
        validate_email(email)
        validate_password(capture.password, candidate)
        cnpj = validate_cnpj(capture.cnpj)
    except ValidationError as error:
        raise SignupDeniedError from error
    return {
        "organization_name": organization_name,
        "cnpj": cnpj,
        "legal_responsible": legal_responsible,
        "username": username,
        "email": email,
        "professional_registration": registration,
        "intended_bundle": "org_admin",
    }


@sensitive_variables()
def submit_signup(
    *,
    capture: SignupCapture,
    idempotency_key: UUID,
) -> SignupReceipt:
    """Create only pending, owner-visible material through one fixed resolver.

    No caller-selected organization, actor, privilege, key version or audit
    vocabulary enters the SQL boundary. The resolver creates its own inactive
    administrator and cannot issue a key for an existing tenant.
    """
    if not isinstance(idempotency_key, UUID):
        raise SignupDeniedError
    payload = _payload(capture)
    kek = _kek()
    fingerprint_capture: dict[str, str] = {
        **payload,
        "password": capture.password,
        "idempotency_key": str(idempotency_key),
    }
    fingerprint = hmac.digest(
        bytes.fromhex(kek),
        b"clinic-signup-v1\x00" + rfc8785.dumps(fingerprint_capture),
        hashlib.sha256,
    )
    registration_id, organization_id, first_admin_id = uuid4(), uuid4(), uuid4()
    try:
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT organization_id, first_admin_id, created
                FROM clinic_app.identity_signup_register(
                    %s, %s, %s, %s, %s::jsonb, %s, %s, %s
                )
                """,
                [
                    registration_id,
                    organization_id,
                    first_admin_id,
                    idempotency_key,
                    json.dumps(payload),
                    make_password(capture.password),
                    fingerprint,
                    kek,
                ],
            )
            row = cursor.fetchone()
    except DatabaseError as error:
        raise SignupDeniedError from error
    if (
        row is None
        or len(row) != SIGNUP_RESULT_COLUMNS
        or not isinstance(row[0], UUID)
        or not isinstance(row[1], UUID)
        or type(row[2]) is not bool
    ):
        raise SignupDeniedError
    return SignupReceipt(row[0], row[1], row[2])


def pending_signups() -> tuple[SignupRegistration, ...]:
    """Inspect pending captures only on the dedicated owner CLI connection.

    The owner command supplies each row's tenant while decrypting it; this
    selector returns deferred protected fields, never decrypts globally.
    """
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_user")
        if cursor.fetchone() != ("clinic_owner",):
            raise SignupDeniedError
    pending: list[SignupRegistration] = []
    with transaction.atomic():
        for registration in SignupRegistration.objects.defer("payload").order_by(
            "created_at", "pk"
        ):
            with scoped_lifecycle_gucs(registration.organization_id, None):
                if Organization.objects.filter(
                    pk=registration.organization_id,
                    status=Organization.Status.PENDING_ACTIVATION,
                ).exists():
                    pending.append(registration)
    return tuple(pending)


@sensitive_variables()
def inspect_pending_signup(*, signup_id: UUID) -> dict[str, str]:
    """Decrypt one pending capture on an owner connection, under its tenant."""
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SELECT current_user")
        if cursor.fetchone() != ("clinic_owner",):
            raise SignupDeniedError
        registration = (
            SignupRegistration.objects.defer("payload").filter(pk=signup_id).first()
        )
        if registration is None:
            raise SignupDeniedError
        with scoped_lifecycle_gucs(registration.organization_id, None):
            if not Organization.objects.filter(
                pk=registration.organization_id,
                status=Organization.Status.PENDING_ACTIVATION,
            ).exists():
                raise SignupDeniedError
            raw: object = registration.payload
            if not isinstance(raw, dict) or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in raw.items()
            ):
                raise SignupDeniedError
            capture = {str(key): str(value) for key, value in raw.items()}
            record_phase1_event(
                "identity.signup.inspected",
                clinic_id=SYSTEM_ORG_ID,
                affected_record_id=registration.pk,
            )
            return capture

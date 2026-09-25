"""Computed roles, binding helpers, authentication and terminal denial guards."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from uuid import uuid4

from apps.ehr import services as ehr
from apps.identity import current_context, otp, preferences
from apps.identity.auth_backends import ClinicBackend
from apps.identity.models import User, UserClinicRole
from apps.intake import contacts, patient_access, questionnaire_views
from apps.retention import services as retention
from apps.teleconsult import services as teleconsult
from django.contrib.auth.models import AnonymousUser
from django.db import connection
from django.http import HttpResponse

from identity.legacy_parity_support import (
    ADMINS,
    LEGACY,
    PHYSICIAN,
    Boundary,
    has_rows,
    http_allowed,
)
from rbac_fixtures import RBAC_RAW_CREDENTIAL

if TYPE_CHECKING:
    from identity.legacy_parity_support import LegacyWorld


def _user(w: LegacyWorld, valid: bool) -> User:
    return w.actor if valid else User(id=uuid4())


def _actor_context(w: LegacyWorld, valid: bool) -> object:
    if not valid:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.current_user_id', %s, true)", [str(uuid4())]
            )
    result = current_context._load_current_actor()
    assert result.pk == w.actor.pk
    return result


def _label(w: LegacyWorld, valid: bool, *, contact: bool) -> str:
    if not valid:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.current_user_id', %s, true)", [str(uuid4())]
            )
    return contacts._actor_label() if contact else patient_access._actor_label()


def _unverified(w: LegacyWorld, valid: bool) -> object:
    previous = w.request.user
    # Removing verification must preserve the receptionist's baseline access,
    # while all three privileged legacy roles must still be challenged.
    w.request.user = w.actor if valid else User(id=uuid4())
    try:
        protected = otp.privileged_totp_required(lambda: "/workspace/")(
            lambda _request: HttpResponse(status=204)
        )
        if not valid:
            # An authenticated unknown User is not an anonymous login; the
            # decorator's role-neutral anonymous case is tested separately.
            w.request.user = AnonymousUser()
        return protected(w.request)
    finally:
        w.request.user = previous


def _preferences(_w: LegacyWorld, valid: bool) -> object:
    if not valid:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.current_user_id', %s, true)", [str(uuid4())]
            )
    return preferences.save_ui_preferences(theme="dark", density="compact")


BOUNDARIES = (
    Boundary(
        "apps.identity.preferences.save_ui_preferences", "actor", LEGACY, _preferences
    ),
    Boundary(
        "apps.identity.models.User._has_role",
        "membership",
        PHYSICIAN,
        lambda w, ok: _user(w, ok)._has_role(UserClinicRole.Role.PHYSICIAN),
    ),
    Boundary(
        "apps.identity.models.User.is_physician_anywhere",
        "membership",
        PHYSICIAN,
        lambda w, ok: _user(w, ok).is_physician_anywhere,
    ),
    Boundary(
        "apps.identity.models.User.is_clinic_admin_anywhere",
        "membership",
        ADMINS,
        lambda w, ok: _user(w, ok).is_clinic_admin_anywhere,
    ),
    Boundary(
        "apps.identity.otp.is_privileged_user",
        "membership",
        (*ADMINS, *PHYSICIAN),
        lambda w, ok: otp.is_privileged_user(_user(w, ok)),
    ),
    Boundary(
        "apps.identity.otp.is_confirmed_verified_user",
        "verification",
        LEGACY,
        lambda w, ok: otp.is_confirmed_verified_user(
            cast("User", w.request.user) if ok else w.actor
        ),
    ),
    Boundary(
        "apps.identity.otp.privileged_totp_required#unverified",
        "decorator",
        ("receptionist",),
        _unverified,
        http_allowed,
    ),
    Boundary(
        "apps.identity.current_context._load_current_actor",
        "actor",
        LEGACY,
        _actor_context,
    ),
    Boundary(
        "apps.identity.auth_backends.ClinicBackend.authenticate",
        "authentication",
        LEGACY,
        lambda w, ok: ClinicBackend().authenticate(
            w.request,
            username=w.actor.username,
            password=RBAC_RAW_CREDENTIAL if ok else "synthetic-wrong",
        ),
        has_rows,
    ),
    Boundary(
        "apps.identity.auth_backends.ClinicBackend.get_user",
        "authentication",
        LEGACY,
        lambda w, ok: ClinicBackend().get_user(w.actor.pk if ok else uuid4()),
        has_rows,
    ),
    Boundary(
        "apps.intake.contacts._actor_label",
        "actor",
        LEGACY,
        lambda w, ok: _label(w, ok, contact=True),
    ),
    Boundary(
        "apps.intake.patient_access._actor_label",
        "actor",
        LEGACY,
        lambda w, ok: _label(w, ok, contact=False),
    ),
    # These functions are terminal denial sinks: fabricating an ALLOW oracle
    # would invert their contract. Their paired success paths are the real
    # clinical, retention and teleconsult service probes in this suite.
    Boundary(
        "apps.ehr.services._denied",
        "denial_sink",
        (),
        lambda w, ok: ehr._denied(w.clinic, w.version.pk, "role_denied"),
    ),
    Boundary(
        "apps.retention.services._denied",
        "denial_sink",
        (),
        lambda w, ok: retention._denied(w.clinic, w.version.pk, "role_denied"),
    ),
    Boundary(
        "apps.teleconsult.services._denied",
        "denial_sink",
        (),
        lambda w, ok: teleconsult._denied(w.encounter, w.clinic, "role_denied"),
    ),
    Boundary(
        "apps.intake.questionnaire_views._denied",
        "denial_sink",
        (),
        lambda w, ok: questionnaire_views._denied(w.request),
        http_allowed,
    ),
)

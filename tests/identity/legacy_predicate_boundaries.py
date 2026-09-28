"""Computed roles, binding helpers, authentication and terminal denial guards."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from uuid import uuid4

from apps.core import navigation
from apps.ehr import services as ehr
from apps.identity import current_context, otp, preferences, saved_views
from apps.identity.auth_backends import ClinicBackend
from apps.identity.models import User, UserClinicRole
from apps.identity.permissions import BUNDLES_V1, PROFESSIONAL_PERMISSIONS_V1
from apps.intake import contacts, patient_access, patient_search, questionnaire_views
from apps.retention import services as retention
from apps.teleconsult import services as teleconsult
from django.contrib.auth.models import AnonymousUser
from django.db import connection
from django.http import HttpResponse

from identity.legacy_parity_support import (
    ADMINS,
    LEGACY,
    MANAGERS,
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


def _saved_view(w: LegacyWorld, valid: bool) -> object:
    if not valid:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.current_user_id', %s, true)", [str(uuid4())]
            )
    try:
        return saved_views.save_view(
            clinic_id=w.graph.clinic_a, destination="agenda", params={"view": "week"}
        )
    except saved_views.SavedViewError:
        # Row security hides the clinic from an actor holding no role in it.
        return False


def _granted_permissions(w: LegacyWorld, valid: bool) -> object:
    if not valid:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.current_user_id', %s, true)", [str(uuid4())]
            )
    granted = navigation.granted_permissions(w.graph.clinic_a)
    # Exact value, not presence: the role's v1 bundle within the registry,
    # less the professionally scoped permissions, which need a current
    # professional registration that this world does not seed. An unknown
    # actor must hold nothing, so the same comparison refuses it.
    expected = (
        BUNDLES_V1[w.role] - PROFESSIONAL_PERMISSIONS_V1
    ) & navigation.REGISTRY_PERMISSIONS
    return granted == expected


def _search_exact(w: LegacyWorld, valid: bool) -> object:
    if not valid:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.current_user_id', %s, true)", [str(uuid4())]
            )
    return patient_search.search_patients_exact(
        clinic_id=w.graph.clinic_a, query="Sintetico", limit=5
    )


# Two gates decide the exact search: the manager roles, then demographics.read.
# The owner passes the first and lacks the second; the physician the reverse.
EXACT_SEARCH = tuple(
    role for role in MANAGERS if "demographics.read" in BUNDLES_V1[role]
)


BOUNDARIES = (
    Boundary(
        "apps.identity.preferences.save_ui_preferences", "actor", LEGACY, _preferences
    ),
    Boundary("apps.identity.saved_views.save_view", "actor", LEGACY, _saved_view),
    Boundary(
        "apps.core.navigation.granted_permissions",
        "resolver",
        LEGACY,
        _granted_permissions,
    ),
    Boundary(
        "apps.intake.patient_search.search_patients_exact",
        "denial",
        EXACT_SEARCH,
        _search_exact,
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

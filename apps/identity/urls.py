"""Routes for Clinic OS authentication and TOTP lifecycle screens."""

from django.urls import path, re_path
from django.urls.resolvers import URLPattern

from apps.identity import (
    account_views,
    clinic_settings_views,
    emergency_views,
    enterprise_auth_views,
    enterprise_views,
    invitation_views,
    lifecycle_views,
    onboarding_views,
    otp_views,
    passkey_account_views,
    passkey_assertion_views,
    passkey_views,
    preferences_views,
    scim_views,
    staff_views,
    stepup_views,
    views,
)
from apps.identity.scim_paths import RESOURCE_PATH_PATTERN

app_name = "identity"

urlpatterns: list[URLPattern] = [
    path(
        "clinics/<uuid:clinic_id>/settings/emergency-reviews/",
        emergency_views.emergency_reviews,
        name="emergency-reviews",
    ),
    path(
        "clinics/<uuid:clinic_id>/settings/governance/",
        enterprise_views.governance_view,
        name="governance",
    ),
    path(
        "clinics/<uuid:clinic_id>/settings/governance/grants/",
        enterprise_views.grant_command,
        name="governance-grant-command",
    ),
    path(
        "clinics/<uuid:clinic_id>/account/emergency/",
        emergency_views.emergency_view,
        name="emergency",
    ),
    path(
        "clinics/<uuid:clinic_id>/account/emergency/read/",
        emergency_views.emergency_read,
        name="emergency-read",
    ),
    path(
        "patient/security/access-log/",
        emergency_views.patient_access_log,
        name="patient-access-log",
    ),
    path("auth/sso/begin/", enterprise_auth_views.sso_begin, name="sso-begin"),
    path("auth/sso/finish/", enterprise_auth_views.sso_finish, name="sso-finish"),
    path(
        "support/diagnostics/",
        enterprise_auth_views.support_diagnostics_view,
        name="support-diagnostics",
    ),
    re_path(
        r"^scim/v2/(?P<kind>Users|Groups)$",
        scim_views.scim_endpoint,
        name="scim-collection",
    ),
    re_path(
        r"^scim/v2/(?P<kind>Users|Groups)/\.search$",
        scim_views.scim_endpoint,
        {"search": True},
        name="scim-search",
    ),
    re_path(
        RESOURCE_PATH_PATTERN,
        scim_views.scim_endpoint,
        name="scim-resource",
    ),
    path("account/passkeys/", passkey_account_views.passkeys_view, name="passkeys"),
    path(
        "account/passkeys/remove/",
        passkey_account_views.passkey_remove,
        name="passkey-remove",
    ),
    path(
        "account/passkeys/policy/",
        passkey_account_views.passkey_policy,
        name="passkey-policy",
    ),
    path("account/sessions/", account_views.sessions_view, name="sessions"),
    path(
        "clinics/<uuid:clinic_id>/account/recovery/",
        account_views.recovery_view,
        name="recovery",
    ),
    path(
        "clinics/<uuid:clinic_id>/account/recovery/enroll/",
        account_views.recovery_enrollment,
        name="recovery-enrollment",
    ),
    path(
        "clinics/<uuid:clinic_id>/settings/onboarding/",
        onboarding_views.onboarding_view,
        name="onboarding",
    ),
    path(
        "clinics/<uuid:clinic_id>/settings/onboarding/providers/review/",
        onboarding_views.provider_review,
        name="provider-review",
    ),
    path(
        "clinics/<uuid:clinic_id>/settings/staff/", staff_views.staff_view, name="staff"
    ),
    path(
        "clinics/<uuid:clinic_id>/settings/staff/reset/request/",
        staff_views.reset_request_view,
        name="reset-request",
    ),
    path(
        "clinics/<uuid:clinic_id>/settings/staff/reset/apply/",
        staff_views.reset_execute_view,
        name="reset-execute",
    ),
    path(
        "clinics/<uuid:clinic_id>/settings/staff/offboard/",
        staff_views.offboarding_view,
        name="offboarding",
    ),
    path(
        "clinics/<uuid:clinic_id>/settings/staff/invitations/revoke/",
        staff_views.invitation_revoke_view,
        name="invitation-revoke",
    ),
    path(
        "clinics/<uuid:clinic_id>/settings/",
        clinic_settings_views.clinic_settings,
        name="clinic-settings",
    ),
    path(
        "clinics/<uuid:clinic_id>/settings/logo/",
        clinic_settings_views.clinic_logo,
        name="clinic-logo",
    ),
    path(
        "account/preferences/",
        preferences_views.preferences_view,
        name="preferences",
    ),
    path("auth/login/", views.login_view, name="login"),
    path(
        "auth/passkey/assert/begin/",
        passkey_assertion_views.assertion_begin,
        name="passkey-assert-begin",
    ),
    path(
        "auth/passkey/assert/finish/",
        passkey_assertion_views.assertion_finish,
        name="passkey-assert-finish",
    ),
    path(
        "auth/passkey/register/begin/",
        passkey_views.registration_begin,
        name="passkey-register-begin",
    ),
    path(
        "auth/passkey/register/finish/",
        passkey_views.registration_finish,
        name="passkey-register-finish",
    ),
    path("auth/signup/", lifecycle_views.signup_view, name="signup"),
    path("auth/invitation/", invitation_views.invitation_view, name="invitation"),
    path(
        "auth/identity-notification/callback/",
        invitation_views.identity_notification_callback,
        name="identity-notification-callback",
    ),
    path("auth/logout/", views.logout_view, name="logout"),
    path("auth/enroll/", otp_views.enroll_view, name="enroll"),
    path("auth/verify/", otp_views.verify_view, name="verify"),
    path("auth/step-up/", stepup_views.step_up_view, name="step-up"),
    path("auth/protected/", views.protected_view, name="protected"),
]

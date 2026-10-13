"""Routes for Clinic OS authentication and TOTP lifecycle screens."""

from django.urls import path
from django.urls.resolvers import URLPattern

from apps.identity import (
    account_views,
    clinic_settings_views,
    invitation_views,
    lifecycle_views,
    onboarding_views,
    otp_views,
    preferences_views,
    staff_views,
    stepup_views,
    views,
)

app_name = "identity"

urlpatterns: list[URLPattern] = [
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
        "clinics/<uuid:clinic_id>/settings/staff/reset/execute/",
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

"""Routes for Clinic OS authentication and TOTP lifecycle screens."""

from django.urls import path
from django.urls.resolvers import URLPattern

from apps.identity import (
    clinic_settings_views,
    otp_views,
    preferences_views,
    stepup_views,
    views,
)

app_name = "identity"

urlpatterns: list[URLPattern] = [
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
    path("auth/logout/", views.logout_view, name="logout"),
    path("auth/enroll/", otp_views.enroll_view, name="enroll"),
    path("auth/verify/", otp_views.verify_view, name="verify"),
    path("auth/step-up/", stepup_views.step_up_view, name="step-up"),
    path("auth/protected/", views.protected_view, name="protected"),
]

"""Routes for Clinic OS authentication and TOTP lifecycle screens."""

from django.urls import path
from django.urls.resolvers import URLPattern

from apps.identity import otp_views, stepup_views, views

app_name = "identity"

urlpatterns: list[URLPattern] = [
    path("auth/login/", views.login_view, name="login"),
    path("auth/logout/", views.logout_view, name="logout"),
    path("auth/enroll/", otp_views.enroll_view, name="enroll"),
    path("auth/verify/", otp_views.verify_view, name="verify"),
    path("auth/step-up/", stepup_views.step_up_view, name="step-up"),
    path("auth/protected/", views.protected_view, name="protected"),
]

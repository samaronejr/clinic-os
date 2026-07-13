"""Routes for Clinic OS authentication and TOTP lifecycle screens."""

from django.urls import path
from django.urls.resolvers import URLPattern

from apps.identity import otp_views, views

app_name = "identity"

urlpatterns: list[URLPattern] = [
    path("auth/login/", views.login_view, name="login"),
    path("auth/logout/", views.logout_view, name="logout"),
    path("auth/enroll/", otp_views.enroll_view, name="enroll"),
    path("auth/verify/", otp_views.verify_view, name="verify"),
    path("auth/protected/", views.protected_view, name="protected"),
    path("__ui__/auth/", views.auth_showcase, name="auth-showcase"),
]

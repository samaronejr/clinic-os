"""Routes of external API v1; patient-linked paths carry opaque external ids only."""

from django.urls import path, re_path
from django.urls.resolvers import URLPattern

from apps.interop.api import views

app_name = "interop_api"

urlpatterns: list[URLPattern] = [
    path("oauth/token", views.TokenView.as_view(), name="oauth-token"),
    path("appointments", views.AppointmentListView.as_view(), name="appointments"),
    path(
        "appointments/<str:appointment_id>",
        views.AppointmentDetailView.as_view(),
        name="appointment",
    ),
    path(
        "patients/<str:patient_id>",
        views.PatientDetailView.as_view(),
        name="patient",
    ),
    path("availability", views.AvailabilityView.as_view(), name="availability"),
    path("invoices", views.InvoiceListView.as_view(), name="invoices"),
    path(
        "invoices/<str:invoice_id>",
        views.InvoiceDetailView.as_view(),
        name="invoice",
    ),
    path(
        "webhook-subscriptions",
        views.WebhookCollectionView.as_view(),
        name="webhook-subscriptions",
    ),
    path(
        "webhook-subscriptions/<uuid:subscription_id>/revoke",
        views.WebhookRevokeView.as_view(),
        name="webhook-subscription-revoke",
    ),
    path(
        "webhook-subscriptions/<uuid:subscription_id>/rotate-secret",
        views.WebhookRotateView.as_view(),
        name="webhook-subscription-rotate",
    ),
    # Last: every other path under the prefix gets the JSON 404 contract body.
    re_path(r"", views.not_found, name="not-found"),
]

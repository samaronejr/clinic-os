"""Staff inbox pages, the patient messages page and the provider webhook endpoint."""

from django.urls import path

from apps.comms import inbox_views, portal_views, webhook_views

app_name = "comms"
urlpatterns = [
    path("clinics/<uuid:clinic_id>/messages/", inbox_views.inbox, name="inbox"),
    path(
        "clinics/<uuid:clinic_id>/messages/<uuid:conversation_id>/",
        inbox_views.conversation,
        name="conversation",
    ),
    # Patient-session page (todo 50): thread selectors travel in POST bodies.
    path(
        "patient/messages/",
        portal_views.patient_messages,
        name="patient-messages",
    ),
    # Authenticated by the raw-body HMAC of the stored provider account (D-19).
    path(
        "comms/webhooks/<slug:provider>/<uuid:account_id>/",
        webhook_views.inbound_webhook,
        name="webhook",
    ),
]

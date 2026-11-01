"""Record selectors stay in POST bodies, never in paths or query strings."""

from django.urls import path

from apps.scribe import review_views, transcript_views, views

app_name = "scribe"
urlpatterns = [
    path(
        "clinics/<uuid:clinic_id>/scribe/control/",
        views.capture_control,
        name="control",
    ),
    path("clinics/<uuid:clinic_id>/scribe/chunk/", views.capture_chunk, name="chunk"),
    path(
        "clinics/<uuid:clinic_id>/scribe/transcript/",
        transcript_views.transcript_review,
        name="transcript",
    ),
    path(
        "clinics/<uuid:clinic_id>/scribe/review/",
        review_views.note_review,
        name="review",
    ),
]

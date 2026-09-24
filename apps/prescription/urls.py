"""Clinical selectors are body-only, never query-string identifiers."""

from django.urls import path

from apps.prescription.views import (
    draft_workspace,
    patient_documents_view,
    review_view,
    signature_callback_view,
    signing_status_view,
    verify_document_view,
)

app_name = "prescription"
urlpatterns = [
    path("prescription/clinics/<uuid:clinic_id>/draft/", draft_workspace, name="draft"),
    path(
        "prescription/clinics/<uuid:clinic_id>/encounters/<uuid:encounter_id>/draft/",
        draft_workspace,
        name="draft-encounter",
    ),
    path(
        "prescription/clinics/<uuid:clinic_id>/documents/<uuid:document_id>/review/",
        review_view,
        name="review",
    ),
    path(
        "prescription/clinics/<uuid:clinic_id>/signing/<uuid:operation_id>/",
        signing_status_view,
        name="signing",
    ),
    path(
        "prescription/signing/callback/<str:provider>/",
        signature_callback_view,
        name="signature-callback",
    ),
    path(
        "prescription/verify/<str:handle>/",
        verify_document_view,
        name="verify-document",
    ),
    path(
        "patient/documents/",
        patient_documents_view,
        name="patient-documents",
    ),
]

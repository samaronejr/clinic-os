"""Clinic-scoped intake routes that carry no patient state in their paths."""

from django.urls import path
from django.urls.resolvers import URLPattern

from apps.intake import delegate_views, duplicate_views, questionnaire_views, views

app_name = "intake"

urlpatterns: list[URLPattern] = [
    path(
        "patient/delegation/switch/",
        delegate_views.switch_representation,
        name="delegate-switch",
    ),
    path(
        "patient/delegation/renew/",
        delegate_views.renew_representation,
        name="delegate-renew",
    ),
    path(
        "intake/clinics/<uuid:clinic_id>/patients/duplicates/",
        duplicate_views.patient_duplicates,
        name="patient-duplicates",
    ),
    path(
        "intake/clinics/<uuid:clinic_id>/patients/timeline/",
        duplicate_views.patient_timeline,
        name="patient-timeline",
    ),
    path(
        "intake/clinics/<uuid:clinic_id>/patients/demographics/",
        views.patient_demographics_view,
        name="patient-demographics",
    ),
    path(
        "patient/questionnaires/",
        questionnaire_views.patient_questionnaires,
        name="questionnaires",
    ),
    path(
        "intake/clinics/<uuid:clinic_id>/questionnaires/",
        questionnaire_views.staff_questionnaires,
        name="questionnaire-staff",
    ),
    path(
        "intake/clinics/<uuid:clinic_id>/patients/",
        views.patient_list_view,
        name="patient-list",
    ),
    path(
        "intake/clinics/<uuid:clinic_id>/patients/new/",
        views.patient_create_view,
        name="patient-create",
    ),
    path(
        "intake/clinics/<uuid:clinic_id>/contacts/",
        views.patient_contacts_view,
        name="patient-contacts",
    ),
    path(
        "intake/clinics/<uuid:clinic_id>/access/",
        views.patient_access_view,
        name="patient-access",
    ),
    path(
        "patient/access/<uuid:clinic_id>/",
        views.patient_access_redeem_view,
        name="patient-access-redeem",
    ),
    path(
        "patient/",
        views.patient_home_view,
        name="patient-home",
    ),
    path(
        "patient/logout/",
        views.patient_logout_view,
        name="patient-logout",
    ),
]

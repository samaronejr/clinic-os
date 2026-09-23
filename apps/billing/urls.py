"""Billing routes: clinic scope for staff, session scope for patients.

A charge identifier appears in a URL only where the server re-authorizes it on
every request: the staff routes require an exact clinic role, and the patient
routes resolve through the live patient session, so another patient's link
resolves to nothing.
"""

from django.urls import path
from django.urls.resolvers import URLPattern

from apps.billing import views

app_name = "billing"

urlpatterns: list[URLPattern] = [
    path(
        "billing/clinics/<uuid:clinic_id>/charges/",
        views.charges_workspace,
        name="charges",
    ),
    path(
        "billing/clinics/<uuid:clinic_id>/charges/<uuid:invoice_id>/",
        views.invoice_detail,
        name="invoice",
    ),
    path(
        "billing/clinics/<uuid:clinic_id>/charges/<uuid:invoice_id>/status/",
        views.invoice_status,
        name="invoice-status",
    ),
    path("patient/charges/", views.patient_charges_view, name="patient-charges"),
    path(
        "patient/charges/<uuid:invoice_id>/",
        views.patient_charge_view,
        name="patient-charge",
    ),
    path(
        "patient/charges/<uuid:invoice_id>/status/",
        views.patient_charge_status,
        name="patient-charge-status",
    ),
]

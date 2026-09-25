"""Clinic-scoped scheduling routes whose paths carry no clinical state."""

from django.urls import path
from django.urls.resolvers import URLPattern

from apps.comms.views import reminders_view
from apps.scheduling import (
    agenda_views,
    booking_views,
    patient_views,
    resource_views,
    transition_views,
    views,
    waitlist_views,
)

app_name = "scheduling"

urlpatterns: list[URLPattern] = [
    path(
        "scheduling/clinics/<uuid:clinic_id>/settings/",
        resource_views.resource_settings,
        name="resource-settings",
    ),
    path(
        "scheduling/clinics/<uuid:clinic_id>/reminders/",
        reminders_view,
        name="reminders",
    ),
    path("patient/offers/", waitlist_views.patient_offers_view, name="patient-offers"),
    path(
        "scheduling/clinics/<uuid:clinic_id>/waitlist/",
        waitlist_views.waitlist_view,
        name="waitlist",
    ),
    path(
        "patient/appointments/",
        patient_views.patient_booking_view,
        name="patient-booking",
    ),
    path(
        "scheduling/clinics/<uuid:clinic_id>/availability/",
        views.availability_list_view,
        name="availability-list",
    ),
    path(
        "scheduling/clinics/<uuid:clinic_id>/availability/"
        "<uuid:availability_id>/retire/",
        views.availability_retire_view,
        name="availability-retire",
    ),
    path(
        "scheduling/clinics/<uuid:clinic_id>/appointments/new/",
        booking_views.appointment_create_view,
        name="appointment-create",
    ),
    path(
        "scheduling/clinics/<uuid:clinic_id>/agenda/",
        agenda_views.agenda_view,
        name="agenda",
    ),
    path(
        "scheduling/clinics/<uuid:clinic_id>/agenda/<str:view>/<str:day>/<int:page>/",
        agenda_views.agenda_view,
        name="agenda-at",
    ),
    path(
        "scheduling/appointments/<uuid:appointment_id>/reschedule/",
        transition_views.appointment_reschedule_view,
        name="appointment-reschedule",
    ),
    path(
        "scheduling/appointments/<uuid:appointment_id>/cancel/",
        transition_views.appointment_cancel_view,
        name="appointment-cancel",
    ),
]

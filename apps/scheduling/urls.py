"""Clinic-scoped scheduling routes whose paths carry no clinical state."""

from django.urls import path
from django.urls.resolvers import URLPattern

from apps.scheduling import views

app_name = "scheduling"

urlpatterns: list[URLPattern] = [
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
]

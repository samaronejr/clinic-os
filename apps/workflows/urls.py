"""Record identifiers are accepted only in task form bodies."""

from django.urls import path

from apps.workflows import views

app_name = "workflows"
urlpatterns = [
    path("clinics/<uuid:clinic_id>/tasks/", views.tasks, name="tasks"),
    path(
        "clinics/<uuid:clinic_id>/tasks/exceptions/",
        views.tasks,
        {"exceptions": True},
        name="exceptions",
    ),
]

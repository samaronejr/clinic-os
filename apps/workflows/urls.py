"""Fixed clinic routes; record selectors travel only in POST bodies."""

from django.urls import path

from apps.workflows import inbox_views, views
from apps.workflows.callbacks import callback
from apps.workflows.designer import designer
from apps.workflows.run_views import runs

app_name = "workflows"
urlpatterns = [
    path("workflows/synthetic/callback/", callback, name="synthetic-callback"),
    path("clinics/<uuid:clinic_id>/workflows/runs/", runs, name="runs"),
    path(
        "clinics/<uuid:clinic_id>/workflows/exceptions/",
        runs,
        {"exceptions": True},
        name="run-exceptions",
    ),
    path("clinics/<uuid:clinic_id>/workflows/designer/", designer, name="designer"),
    path("clinics/<uuid:clinic_id>/inbox/", inbox_views.inbox, name="inbox"),
    path("clinics/<uuid:clinic_id>/today/", inbox_views.today, name="today"),
    path("clinics/<uuid:clinic_id>/tasks/", views.tasks, name="tasks"),
    path(
        "clinics/<uuid:clinic_id>/tasks/exceptions/",
        views.tasks,
        {"exceptions": True},
        name="exceptions",
    ),
]

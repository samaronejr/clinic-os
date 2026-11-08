"""Fixed clinic routes; record selectors travel only in POST bodies."""

from django.urls import path

from apps.workflows import coordination_views, inbox_views, views
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
    # Task 55: urgent concerns, their coverage policy and release notices.
    path(
        "clinics/<uuid:clinic_id>/inbox/urgent/",
        coordination_views.urgent_concerns_page,
        name="urgent-concerns",
    ),
    path(
        "clinics/<uuid:clinic_id>/settings/urgent-coverage/",
        coordination_views.urgent_coverage_page,
        name="urgent-coverage",
    ),
    path(
        "clinics/<uuid:clinic_id>/inbox/release-notices/",
        coordination_views.release_notices_page,
        name="release-notices",
    ),
]

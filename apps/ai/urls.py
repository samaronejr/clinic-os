"""Clinic-scoped organization controls; artifact selectors never travel in URLs."""

from django.urls import path

from apps.ai.agent_views import agent_queue, agent_settings
from apps.ai.brief_views import chart_assistant
from apps.ai.decision_views import decision_support
from apps.ai.views import settings_view

app_name = "ai"
urlpatterns = [
    path(
        "clinics/<uuid:clinic_id>/decision-support/",
        decision_support,
        name="decision-support",
    ),
    path(
        "clinics/<uuid:clinic_id>/chart-assistant/",
        chart_assistant,
        name="chart-assistant",
    ),
    path("clinics/<uuid:clinic_id>/settings/ai/", settings_view, name="settings"),
    path("clinics/<uuid:clinic_id>/automations/agents/", agent_queue, name="agents"),
    path(
        "clinics/<uuid:clinic_id>/settings/agents/",
        agent_settings,
        name="agent-settings",
    ),
]

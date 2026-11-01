"""Clinic-scoped organization controls; artifact selectors never travel in URLs."""

from django.urls import path

from apps.ai.agent_views import agent_queue, agent_settings
from apps.ai.brief_views import chart_assistant
from apps.ai.decision_views import decision_support
from apps.ai.evaluation.views import evaluation_view
from apps.ai.extraction_views import extraction_view
from apps.ai.front_desk_views import (
    front_desk_handoffs_page,
    front_desk_knowledge_page,
)
from apps.ai.views import settings_view

app_name = "ai"
urlpatterns = [
    path("clinics/<uuid:clinic_id>/evaluation/", evaluation_view, name="evaluation"),
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
    path("clinics/<uuid:clinic_id>/extraction/", extraction_view, name="extraction"),
    path("clinics/<uuid:clinic_id>/settings/ai/", settings_view, name="settings"),
    path("clinics/<uuid:clinic_id>/automations/agents/", agent_queue, name="agents"),
    path(
        "clinics/<uuid:clinic_id>/settings/agents/",
        agent_settings,
        name="agent-settings",
    ),
    # Task 54: the front-desk assistant's reviewed answers and its handoffs.
    path(
        "clinics/<uuid:clinic_id>/settings/front-desk/",
        front_desk_knowledge_page,
        name="front-desk-knowledge",
    ),
    path(
        "clinics/<uuid:clinic_id>/messages/front-desk/",
        front_desk_handoffs_page,
        name="front-desk-handoffs",
    ),
]

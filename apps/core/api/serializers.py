"""Serializers that adapt service frozen dataclasses; never models."""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from apps.core.api.errors import ERROR_CODES
from apps.scheduling.agenda_queries import AgendaPage


class ErrorSerializer(serializers.Serializer[None]):
    """The ``{code, message_key}`` refusal body shared by every endpoint."""

    code = serializers.ChoiceField(choices=[(code, code) for code in ERROR_CODES])
    message_key = serializers.CharField()


class AgendaQueryRequestSerializer(serializers.Serializer[None]):
    """POST body of ``agenda/query``; the clinic identifier stays in the body."""

    clinic_id = serializers.UUIDField()
    view = serializers.ChoiceField(choices=(("day", "day"), ("week", "week")))
    date = serializers.DateField()
    page = serializers.IntegerField(min_value=1, default=1)


class AgendaItemSerializer(serializers.Serializer[Any]):
    """Adapt ``scheduling.AgendaItem``: one clinic-local appointment row."""

    appointment_id = serializers.UUIDField(read_only=True)
    patient_display_name = serializers.CharField(read_only=True)
    practitioner_id = serializers.UUIDField(read_only=True)
    practitioner_display_identifier = serializers.CharField(read_only=True)
    start_local = serializers.CharField(read_only=True)
    end_local = serializers.CharField(read_only=True)
    status = serializers.CharField(read_only=True)


class AgendaPageSerializer(serializers.Serializer[AgendaPage]):
    """Adapt ``scheduling.AgendaPage``: one bounded agenda page."""

    items = AgendaItemSerializer(many=True, read_only=True)
    view = serializers.ChoiceField(
        choices=(("day", "day"), ("week", "week")), read_only=True
    )
    date = serializers.DateField(read_only=True)
    page = serializers.IntegerField(read_only=True)
    total = serializers.IntegerField(read_only=True)
    page_count = serializers.IntegerField(read_only=True)
    start_at = serializers.DateTimeField(read_only=True)
    end_at = serializers.DateTimeField(read_only=True)

"""Database-blocked schema, gateway, task, locale and rendered-control checks."""

from __future__ import annotations

import inspect
from dataclasses import replace
from html.parser import HTMLParser
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID

import pytest
from apps.ai.contracts import ProviderRequest, Usage
from apps.ai.output_validation import call_provider
from apps.scribe import transcript_views, transcripts
from apps.scribe.asr import AudioInput, parse_batch
from apps.scribe.gateway import ScribeRecordedProvider, ScribeSyntheticProvider
from apps.scribe.models import TranscriptSegment
from apps.scribe.transcript_contracts import (
    CoverageSlot,
    Segment,
    TranscriptVersionDTO,
    Uncertainty,
    finalize,
)
from apps.scribe.transcript_views import _runs
from apps.tenancy.fields import EncryptedJSONField, EncryptedTextField
from config.celery import app
from django.apps import apps
from django.db.migrations.autodetector import MigrationAutodetector
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.questioner import NonInteractiveMigrationQuestioner
from django.db.migrations.state import ProjectState
from django.shortcuts import render
from django.template.loader import render_to_string
from django.test import RequestFactory, override_settings
from django.urls import reverse
from django.utils.translation import gettext, override

from scribe.test_transcripts import visit


class Controls(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.inputs: list[dict[str, str | None]] = []
        self.forms: list[dict[str, str | None]] = []
        self.marks = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "input":
            self.inputs.append(dict(attrs))
        if tag == "form":
            self.forms.append(dict(attrs))
        if tag == "mark":
            self.marks += 1


def test_transcript_migration_matches_models_without_a_database() -> None:
    loader = MigrationLoader(None, ignore_no_migrations=True)
    with override("en"):
        changes = MigrationAutodetector(
            loader.project_state(),
            ProjectState.from_apps(apps),
            NonInteractiveMigrationQuestioner(),
        ).changes(graph=loader.graph)
    assert not changes.get("scribe")
    assert isinstance(TranscriptSegment._meta.get_field("text"), EncryptedTextField)
    assert isinstance(
        TranscriptSegment._meta.get_field("uncertainty"), EncryptedJSONField
    )


def test_both_gateway_adapters_replay_the_same_structured_recording() -> None:
    source = AudioInput(
        UUID(int=1),
        UUID(int=2),
        "audio/webm;codecs=opus",
        2000,
        b"SINTETICO-ASR-FIXTURE:2",
    ).encode()
    request = ProviderRequest(
        "fixture-v1",
        "https://ai.synthetic.invalid/v1/inference",
        "synthetic",
        source,
        Usage(audio_seconds=2),
        30,
    )
    fake = call_provider(
        ScribeSyntheticProvider(), request=request, method="transcribe"
    )
    recording = call_provider(
        ScribeRecordedProvider(),
        request=replace(
            request, endpoint="https://recorded.synthetic.invalid/v1/inference"
        ),
        method="transcribe",
    )
    assert fake == recording
    assert fake.usage.audio_seconds == 2
    assert parse_batch(fake.content, duration_ms=2000)[-1].segments[0].uncertainty


def test_reference_only_asr_task_is_discovered_without_starting_a_worker() -> None:
    app.autodiscover_tasks(force=True)
    assert "apps.scribe.tasks.transcribe" in app.tasks


@override_settings(
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
        },
    }
)
def test_native_review_forms_render_without_clinical_text_in_attributes() -> None:
    value = Segment(
        UUID(int=10),
        UUID(int=11),
        0,
        2000,
        "<script>5 ou 15 mg</script>",
        uncertainty=(Uncertainty(8, 18, "uncertain"),),
    )
    transcript = TranscriptVersionDTO(
        1,
        "partial",
        (value,),
        (CoverageSlot(UUID(int=20), "microphone", 0, value.chunk_id),),
        frozenset(),
        recording_closed=True,
    )
    with override("pt-br"):
        html = render_to_string(
            "scribe/transcript.html",
            {
                "clinic_id": UUID(int=30),
                "encounter_id": UUID(int=31),
                "recording": SimpleNamespace(pk=UUID(int=32)),
                "revision": 1,
                "transcript": transcript,
                "can_finalize": True,
                "available": False,
                "segments": [{"item": value, "runs": _runs(value)}],
                "speaker_choices": (("unknown", gettext("Unknown speaker")),),
                "csrf_token": "synthetic-source-only-csrf",
            },
        )
    controls = Controls()
    controls.feed(html)
    assert controls.marks == 1
    assert all(form["method"] == "post" for form in controls.forms)
    assert all("script>" not in str(item) for item in controls.inputs)
    assert "<script>5 ou 15 mg</script>" not in html
    acknowledgments = [
        item for item in controls.inputs if item.get("name") == "acknowledge_incomplete"
    ]
    assert len(acknowledgments) == 1
    assert "checked" not in acknowledgments[0]
    assert reverse("scribe:transcript", args=(UUID(int=30),)).endswith(
        "/scribe/transcript/"
    )


@pytest.mark.parametrize("finalized", [False, True])
@override_settings(
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
        },
    }
)
def test_refresh_exposes_two_new_missing_slots_without_another_recognition(
    finalized: bool,
) -> None:
    snapshot = visit()
    if finalized:
        snapshot = finalize(snapshot, acknowledge_incomplete=False)
    assert snapshot.missing == ()
    pending = CoverageSlot(UUID(int=100), "microphone", 4, UUID(int=5))
    gap = CoverageSlot(UUID(int=100), "microphone", 5, None)
    live_coverage = (*snapshot.coverage, pending, gap)
    recording = SimpleNamespace(
        pk=UUID(int=32),
        clinic_id=UUID(int=30),
        encounter_id=UUID(int=31),
        state="stopped",
    )
    request = RequestFactory().post(
        "/",
        {
            "action": "refresh",
            "encounter_id": str(recording.encounter_id),
            "session_id": str(recording.pk),
        },
    )
    with (
        patch("apps.scribe.transcripts.authorized_recording", return_value=recording),
        patch("apps.scribe.transcripts.current_snapshot", return_value=snapshot),
        patch("apps.scribe.transcripts.coverage_for", return_value=live_coverage),
        patch("apps.scribe.transcript_views.coverage_for", return_value=live_coverage),
        patch("apps.scribe.transcript_views.browser_binding"),
        patch("apps.scribe.transcript_views.encounter_scope"),
        patch("apps.scribe.transcript_views._recording", return_value=recording),
        patch("apps.scribe.transcript_views._available", return_value=False),
        patch("apps.scribe.transcript_views.render", wraps=render) as rendered,
        override("en"),
    ):
        response = inspect.unwrap(transcript_views.transcript_review)(
            request, recording.clinic_id
        )
        current = transcripts.read_transcript(
            clinic_id=recording.clinic_id, session_id=recording.pk
        )
    assert response.status_code == 200
    assert rendered.call_args is not None
    context = rendered.call_args.args[2]
    assert context["missing"] == (pending, gap)
    assert '<h2 id="transcript-coverage-title">' in response.content.decode()
    assert snapshot.missing == ()
    assert current is not None
    assert current.revision == snapshot.revision
    assert current.segments == snapshot.segments
    if finalized:
        assert current is snapshot
    else:
        assert current.coverage == live_coverage
        assert current.missing == (pending, gap)

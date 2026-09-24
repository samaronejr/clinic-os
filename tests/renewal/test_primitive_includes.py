"""The shared field and notice includes wire labels, hints, errors and tones."""

from __future__ import annotations

from django import forms
from django.template.loader import render_to_string


class _RegistrationForm(forms.Form):
    full_name = forms.CharField(label="Patient name", max_length=120)
    birth_date = forms.CharField(label="Birth date", required=False)


def test_field_include_renders_label_control_hint_and_linked_errors() -> None:
    form = _RegistrationForm(data={"full_name": ""})
    assert not form.is_valid()

    html = render_to_string(
        "includes/field.html",
        {"field": form["full_name"], "hint": "As written on the identity document."},
    )

    assert '<div class="field field--error">' in html
    assert '<label for="id_full_name">Patient name:</label>' in html
    assert 'aria-invalid="true"' in html
    assert '<p class="hint" id="id_full_name_hint">' in html
    assert '<div class="field-errors" id="id_full_name_error">' in html
    assert '<p class="field-error">Este campo é obrigatório.</p>' in html
    assert "field-success" not in html


def test_field_include_renders_success_progress_and_custom_hint_id() -> None:
    form = _RegistrationForm(data={"full_name": "Marina Duarte Sampaio"})
    assert form.is_valid()

    html = render_to_string(
        "includes/field.html",
        {
            "field": form["birth_date"],
            "hint": "Use DD/MM/YYYY.",
            "hint_id": "birth-help",
            "success": "Format accepted.",
            "busy": True,
            "progress": "Checking the registry…",
        },
    )

    assert '<div class="field field--success" aria-busy="true">' in html
    assert '<p class="hint" id="birth-help">' in html
    assert (
        '<p class="field-progress" id="id_birth_date_progress" role="status">' in html
    )
    assert '<p class="field-success" id="id_birth_date_success">' in html
    assert "field-errors" not in html


def test_notice_include_maps_tone_to_role_class_and_focus_target() -> None:
    error = render_to_string(
        "includes/notice.html",
        {
            "tone": "error",
            "title": "We could not save the patient",
            "message": "Fix the fields below.",
            "id": "summary",
            "focus": True,
        },
    )
    assert 'class="feedback feedback--error"' in error
    assert 'role="alert"' in error
    assert 'tabindex="-1" data-focus-error' in error
    assert "<h2>We could not save the patient</h2>" in error

    loading = render_to_string(
        "includes/notice.html",
        {"tone": "loading", "title": "Refreshing…", "heading": "h3"},
    )
    assert 'class="feedback"' in loading
    assert 'role="status"' in loading
    assert 'aria-busy="true"' in loading
    assert "<h3>Refreshing…</h3>" in loading

    success = render_to_string(
        "includes/notice.html", {"tone": "success", "message": "Booked for 14:30."}
    )
    assert 'class="feedback feedback--success"' in success
    assert "tabindex" not in success
    assert "<p>Booked for 14:30.</p>" in success

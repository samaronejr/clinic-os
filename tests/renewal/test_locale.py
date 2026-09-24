"""Locale boundaries preserve identifiers, native values, UTC and strict parsing."""

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import cast
from uuid import UUID

import pytest
from apps.identity.forms import ClinicAuthenticationForm
from apps.identity.templatetags.clinic_locale import (
    appointment_label,
    brl,
    clinic_minute,
)
from apps.intake.forms import PatientCreateForm, PatientSearchForm
from apps.intake.models import Patient
from apps.intake.services import PatientSearchPage
from apps.intake.views import _results_status
from apps.scheduling.appointment_forms import AppointmentCancelForm, LocalMinuteField
from apps.scheduling.models import Appointment
from apps.scheduling.timezones import LocalTimeValueError, parse_local_minute
from django import forms
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db.models import Field
from django.http import QueryDict
from django.template.loader import get_template, render_to_string
from django.utils.translation import gettext, override

KEY = UUID(int=8801)


def test_default_locale_preserves_storage_and_numeric_settings() -> None:
    assert settings.LANGUAGE_CODE == "pt-br"
    assert settings.USE_I18N
    assert settings.USE_TZ
    assert settings.TIME_ZONE == "UTC"
    assert not settings.USE_THOUSAND_SEPARATOR
    assert dict(settings.LANGUAGES).keys() == {"pt-br"}


@pytest.mark.parametrize("value", ["1988-03-12", "12/03/1988"])
def test_patient_form_round_trips_diacritics_and_native_date(value: str) -> None:
    data = QueryDict(mutable=True)
    data.update(
        {
            "full_name": "João da Conceição",
            "birth_date": value,
            "idempotency_key": str(KEY),
        }
    )
    form = PatientCreateForm(data)
    assert form.is_valid(), form.errors
    assert form.cleaned_data["birth_date"] == date(1988, 3, 12)
    field = Patient._meta.get_field("full_name")
    assert isinstance(field, Field)
    assert (
        field.clean("Joa\u0303o da Conceic\u0327a\u0303o", None) == "João da Conceição"
    )
    assert form.cleaned_data["idempotency_key"] == KEY
    unbound = PatientCreateForm()
    unbound.initial["birth_date"] = form.cleaned_data["birth_date"]
    assert 'value="1988-03-12"' in str(unbound["birth_date"])


@pytest.mark.parametrize("value", ["31/02/2030", "2030-13-01", "12/31/2030"])
def test_invalid_dates_keep_error_code_and_localized_shipped_copy(value: str) -> None:
    field = PatientCreateForm().fields["birth_date"]
    with pytest.raises(ValidationError) as caught:
        field.clean(value)
    assert caught.value.code == "invalid"
    messages = caught.value.messages
    assert messages == [gettext("Enter a valid date.")]
    with override("en"):
        assert messages != [gettext("Enter a valid date.")]


def test_local_minute_display_never_reinterprets_the_clinic_zone() -> None:
    instant = parse_local_minute("2031-01-02T23:30", "America/Sao_Paulo")
    assert instant == datetime(2031, 1, 3, 2, 30, tzinfo=UTC)
    assert clinic_minute("2031-01-02T23:30") == "02/01/2031 23:30"
    assert LocalMinuteField().clean("2031-01-02T23:30") == "2031-01-02T23:30"
    with pytest.raises(ValidationError) as caught:
        LocalMinuteField().clean("02/01/2031 23:30")
    assert caught.value.code == "invalid"


@pytest.mark.parametrize("value", ["2031-03-09T02:30", "2031-11-02T01:30"])
def test_dst_gap_and_fold_are_still_rejected(value: str) -> None:
    with pytest.raises(LocalTimeValueError):
        parse_local_minute(value, "America/New_York")


def test_brl_examples_keep_decimal_precision_and_existing_input_contract() -> None:
    field = forms.DecimalField(localize=True, decimal_places=2)
    assert field.clean("1234,56") == Decimal("1234.56")
    assert field.clean("1234.56") == Decimal("1234.56")
    assert brl(field.clean("1234,56")) == "R$ 1.234,56"
    assert brl(Decimal("0.10")) == "R$ 0,10"
    assert field.widget.format_value(Decimal("1234.56")) == "1234,56"
    assert field.clean(field.widget.format_value(Decimal("1234.56"))) == Decimal(
        "1234.56"
    )


@pytest.mark.parametrize("value", ["1,234.56", "1.234,56", "1,23,4", "R$ 12,00"])
def test_mixed_or_decorated_numeric_input_is_not_silently_reinterpreted(
    value: str,
) -> None:
    with pytest.raises(ValidationError) as caught:
        forms.DecimalField(localize=True, decimal_places=2).clean(value)
    assert caught.value.code == "invalid"
    assert caught.value.messages == [gettext("Enter a number.")]


def test_cancellation_labels_change_but_persisted_values_do_not() -> None:
    form = AppointmentCancelForm()
    field = form.fields["reason"]
    assert isinstance(field, forms.ChoiceField)
    choices = dict(cast("list[tuple[str, str]]", field.choices))
    assert set(choices) == {
        "patient_request",
        "clinic_request",
        "practitioner_unavailable",
        "duplicate",
        "other",
    }
    for reason in Appointment.CancellationReason:
        assert field.clean(reason.value) == reason.value
        assert str(choices[reason.value]) == appointment_label(reason.value)
        assert str(choices[reason.value]) != reason.label
    assert Appointment.Status.values == ["scheduled", "cancelled"]


def test_all_delivered_templates_compile_and_auth_labels_use_the_catalog() -> None:
    for path in (settings.BASE_DIR / "templates").rglob("*.html"):
        get_template(str(path.relative_to(settings.BASE_DIR / "templates")))
    form = ClinicAuthenticationForm()
    html = render_to_string("identity/login.html", {"form": form})
    assert '<html lang="pt-br">' in html
    assert str(form.fields["password"].label) == gettext("Password")
    assert gettext("Sign in to Clinic OS") in html
    with override("en"):
        assert gettext("Sign in to Clinic OS") not in html


@pytest.mark.parametrize("total", [1, 2, 31])
def test_localized_result_counts_keep_machine_values(total: int) -> None:
    results = PatientSearchPage(items=(), page=1, total=total, page_count=1)
    status = _results_status(results, "Marina")
    html = render_to_string(
        "intake/partials/patient_results.html",
        {"results": results, "status": status, "form": PatientSearchForm()},
    )
    assert str(total) in status
    assert status in html
    assert "matching patient" not in html
    assert "Página 1 de 1." in html
    assert ("pacientes" in status) is (total > 1)


def test_empty_result_status_names_the_escaped_term() -> None:
    results = PatientSearchPage(items=(), page=1, total=0, page_count=0)
    status = _results_status(results, "Zé <b>")
    html = render_to_string(
        "intake/partials/patient_results.html",
        {"results": results, "status": status, "form": PatientSearchForm()},
    )
    assert "Zé <b>" in status
    assert "Zé &lt;b&gt;" in html
    assert "<b>" not in html
    assert "matching patient" not in html

"""Native, bounded metric forms; clinic timezone is never client-selected."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from django import forms
from django.core.validators import MaxValueValidator
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.analytics.catalog import METRICS
from apps.analytics.contracts import AnalyticsInputError, Query, parse_descriptor

if TYPE_CHECKING:
    from collections.abc import Mapping


class ReportForm(forms.Form):
    """The complete manual reporting path remains available when AI is disabled."""

    metric = forms.ChoiceField(
        label=_("Metric"),
        choices=[(key, _(metric.label)) for key, metric in METRICS.items()],
    )
    start = forms.DateField(
        label=_("From"),
        input_formats=["%d/%m/%Y"],
        widget=forms.DateInput(format="%d/%m/%Y"),
    )
    end = forms.DateField(
        label=_("Through"),
        input_formats=["%d/%m/%Y"],
        widget=forms.DateInput(format="%d/%m/%Y"),
        validators=[MaxValueValidator(date.max - timedelta(days=1))],
    )
    grouping = forms.ChoiceField(
        label=_("Grouping"),
        choices=[("", _("Total")), ("day", _("Day"))],
        required=False,
    )
    weekday = forms.MultipleChoiceField(
        label=_("Day filter"),
        required=False,
        help_text=_("Leave empty to include every day."),
        choices=[
            ("0", _("Monday")),
            ("1", _("Tuesday")),
            ("2", _("Wednesday")),
            ("3", _("Thursday")),
            ("4", _("Friday")),
            ("5", _("Saturday")),
            ("6", _("Sunday")),
        ],
    )
    question = forms.CharField(
        label=_("Ask the synthetic analyst"),
        max_length=500,
        required=False,
        help_text=_(
            "Use a metric name. Dates come from this form. "
            "Do not enter patient information."
        ),
    )

    def __init__(
        self, data: Mapping[str, str] | None = None, *, clinic_timezone: str
    ) -> None:
        """Use explicit initial values; the browser never supplies authority."""
        super().__init__(data=data)
        self.clinic_timezone = clinic_timezone
        today = timezone.localdate(timezone=ZoneInfo(clinic_timezone))
        self.initial.update(
            metric="no_show", start=today - timedelta(days=29), end=today
        )

    def clean(self) -> dict[str, object]:
        """Refuse unsupported dimensions and invalid windows before any write."""
        data = super().clean()
        if data is None:
            return {}
        if "metric" not in data or "start" not in data or "end" not in data:
            return data
        zone = ZoneInfo(self.clinic_timezone)
        query = Query(
            str(data["metric"]),
            datetime.combine(data["start"], time.min, zone),
            datetime.combine(data["end"] + timedelta(days=1), time.min, zone),
            self.clinic_timezone,
            ("day",) if data.get("grouping") else (),
            tuple(int(day) for day in data.get("weekday", ())),
        )
        try:
            self.query = parse_descriptor(query.encode())
        except AnalyticsInputError as error:
            raise forms.ValidationError(
                _("Check the dates and the metric's permitted dimensions.")
            ) from error
        return data

    def select_query(self, query: Query) -> None:
        """Restore the saved manual filters without rebinding a reference-only POST."""
        zone = ZoneInfo(self.clinic_timezone)
        self.initial.update(
            metric=query.metric_id,
            start=query.start.astimezone(zone).date(),
            end=(query.end - timedelta(microseconds=1)).astimezone(zone).date(),
            grouping="day" if query.dimensions else "",
            weekday=[str(day) for day in query.weekdays],
        )


class ReferenceForm(forms.Form):
    """Selectors travel in POST bodies only."""

    reference = forms.UUIDField(widget=forms.HiddenInput)
    query_digest = forms.RegexField(
        regex=r"^[0-9a-f]{64}$", required=False, widget=forms.HiddenInput
    )
    authorization_version = forms.RegexField(
        regex=r"^[0-9a-f]{64}$", required=False, widget=forms.HiddenInput
    )

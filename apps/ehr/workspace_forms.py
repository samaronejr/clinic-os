"""Native patient workspace forms; decimals remain strings until domain validation."""

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.ehr.longitudinal_models import MedicationStatement
from apps.ehr.longitudinal_values import CODES, ORIGINS, ProvenanceTag
from apps.ehr.medication_services import DOSE_UNITS

ORIGIN_CHOICES = [(value, ProvenanceTag(value).label) for value in ORIGINS]
ROUTE_CHOICES = [
    ("oral", _("Oral")),
    ("inhaled", _("Inhaled")),
    ("topical", _("Topical")),
    ("subcutaneous", _("Subcutaneous")),
    ("intramuscular", _("Intramuscular")),
    ("intravenous", _("Intravenous")),
    ("other", _("Other")),
]


class ObservationForm(forms.Form):
    """The server validates a submitted unit against its kind, not just this list."""

    kind = forms.ChoiceField(
        label=_("Observation"),
        choices=[(key, code.label) for key, code in CODES.items() if key != "bmi"],
        initial="temperature",
    )
    value = forms.CharField(label=_("Value"), max_length=30)
    unit = forms.ChoiceField(
        label=_("Unit"),
        choices=[
            (unit, unit) for unit in dict.fromkeys(code.unit for code in CODES.values())
        ],
        initial="Cel",
    )
    provenance = forms.ChoiceField(
        label=_("Origin"), choices=ORIGIN_CHOICES, initial=ProvenanceTag.OBSERVED
    )
    reference_low = forms.CharField(label=_("Reference minimum"), required=False)
    reference_high = forms.CharField(label=_("Reference maximum"), required=False)


class VitalsForm(forms.Form):
    """One simultaneous measurement set, with no fabricated normal defaults."""

    systolic_bp = forms.CharField(
        label=_("Systolic blood pressure (mmHg)"), required=False
    )
    diastolic_bp = forms.CharField(
        label=_("Diastolic blood pressure (mmHg)"), required=False
    )
    heart_rate = forms.CharField(label=_("Heart rate (/min)"), required=False)
    respiratory_rate = forms.CharField(
        label=_("Respiratory rate (/min)"), required=False
    )
    temperature = forms.CharField(label=_("Temperature (Cel)"), required=False)
    spo2 = forms.CharField(label=_("Oxygen saturation (%)"), required=False)
    weight = forms.CharField(label=_("Weight (kg)"), required=False)
    height = forms.CharField(label=_("Height (cm)"), required=False)
    provenance = forms.ChoiceField(
        label=_("Origin"), choices=ORIGIN_CHOICES, initial=ProvenanceTag.OBSERVED
    )


class MedicationForm(forms.Form):
    """Reported use remains unconfirmed after this form is saved."""

    text = forms.CharField(label=_("Medication"), max_length=240)
    dose_value = forms.CharField(label=_("Dose"), max_length=30)
    dose_unit = forms.ChoiceField(
        label=_("Dose unit"), choices=[(u, u) for u in DOSE_UNITS]
    )
    route = forms.ChoiceField(label=_("Route"), choices=ROUTE_CHOICES)
    times_per_day = forms.CharField(label=_("Times per day"), max_length=30)
    status = forms.ChoiceField(
        label=_("Status"),
        choices=MedicationStatement.Status,
        initial=MedicationStatement.Status.UNKNOWN,
    )
    provenance = forms.ChoiceField(
        label=_("Origin"), choices=ORIGIN_CHOICES, initial=ProvenanceTag.REPORTED
    )

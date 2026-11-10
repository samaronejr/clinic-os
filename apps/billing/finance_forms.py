"""Closed native Finance commands; selectors stay in POST bodies."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, cast
from uuid import uuid4

from django import forms
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from apps.billing.money_types import MAX_MINOR, JournalInputError, round_reais

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from django.http import QueryDict

ACTION_FIELDS: Final = {
    "create_book": (),
    "opening_balance": ("account", "amount", "evidence_reference", "attested"),
    "create_rule": ("practitioner_id", "kind", "rule_value", "command_id"),
    "prepare_terms": ("invoice_id", "payer", "discount", "adjustment", "rule_id"),
    "complete_service": ("invoice_id", "evidence_reference", "attested"),
    "observe": (
        "amount",
        "receipt_kind",
        "account",
        "evidence_reference",
    ),
    "verify": ("settlement_id", "attested"),
    "quarantine": ("settlement_id",),
    "post": ("settlement_id",),
    "allocate": ("settlement_id", "invoice_id", "amount", "command_id"),
    "allocate_proportionally": ("settlement_id", "invoice_ids", "command_id"),
    "request_refund": ("allocation_id", "amount", "refund_kind", "command_id"),
    "request_unallocated_refund": ("settlement_id", "amount", "command_id"),
    "request_payout": ("practitioner_id", "amount", "command_id"),
    "approve_refund": ("refund_id",),
    "approve_payout": ("payout_id",),
    "refund_state": ("refund_id", "outcome", "outcome_evidence", "attested"),
    "payout_state": ("payout_id", "outcome", "outcome_evidence", "attested"),
    "open_cash": ("counted", "command_id"),
    "close_cash": ("session_id", "counted", "expected_revision", "command_id"),
    "reopen_cash": ("session_id", "reason", "expected_revision", "command_id"),
    "psp_fee": ("amount", "account", "command_id"),
}
ACTION_LABELS: Final = {
    "create_book": _("Open the clinic book"),
    "opening_balance": _("Record opening balance"),
    "create_rule": _("Record practitioner rule"),
    "prepare_terms": _("Freeze invoice terms"),
    "complete_service": _("Attest completed service"),
    "observe": _("Record observed money"),
    "verify": _("Verify retained evidence"),
    "quarantine": _("Quarantine receipt"),
    "post": _("Post verified collection"),
    "allocate": _("Allocate collection"),
    "allocate_proportionally": _("Allocate proportionally"),
    "request_refund": _("Request refund or chargeback"),
    "request_unallocated_refund": _("Return unallocated money"),
    "request_payout": _("Request practitioner payout"),
    "approve_refund": _("Approve refund"),
    "approve_payout": _("Approve payout"),
    "refund_state": _("Record refund rehearsal outcome"),
    "payout_state": _("Record payout rehearsal outcome"),
    "open_cash": _("Open cash drawer"),
    "close_cash": _("Close cash drawer"),
    "reopen_cash": _("Reopen cash drawer"),
    "psp_fee": _("Record PSP fee"),
}


class FinanceForm(forms.Form):
    """Display only the fields of one closed, exact-clinic command."""

    command_id = forms.UUIDField(widget=forms.HiddenInput())
    invoice_id = forms.ChoiceField(label=_("Charge"))
    invoice_ids = forms.MultipleChoiceField(
        label=_("Charges for proportional allocation")
    )
    settlement_id = forms.ChoiceField(label=_("Collection"))
    allocation_id = forms.ChoiceField(label=_("Allocation"))
    refund_id = forms.ChoiceField(label=_("Refund"))
    payout_id = forms.ChoiceField(label=_("Payout"))
    session_id = forms.ChoiceField(label=_("Cash drawer"))
    practitioner_id = forms.ChoiceField(label=_("Practitioner"))
    rule_id = forms.ChoiceField(label=_("Practitioner rule"), required=False)
    amount = forms.CharField(label=_("Amount in reais"), max_length=64)
    counted = forms.CharField(label=_("Counted cash in reais"), max_length=64)
    discount = forms.CharField(label=_("Discount in reais"), max_length=64)
    adjustment = forms.CharField(label=_("Payer adjustment in reais"), max_length=64)
    expected_revision = forms.IntegerField(label=_("Reviewed revision"), min_value=1)
    rule_value = forms.IntegerField(
        label=_("Basis points or fixed centavos"), min_value=0, max_value=MAX_MINOR
    )
    kind = forms.ChoiceField(
        label=_("Rule basis"),
        choices=(("percent", _("Percentage")), ("fixed", _("Fixed per service"))),
    )
    payer = forms.ChoiceField(
        label=_("Payer"), choices=(("patient", _("Patient")), ("insurer", _("Insurer")))
    )
    receipt_kind = forms.ChoiceField(
        label=_("Receipt kind"),
        choices=(
            ("patient", _("Patient advance")),
            ("insurer", _("Insurer overpayment")),
            ("unidentified", _("Unidentified receipt")),
        ),
    )
    account = forms.ChoiceField(
        label=_("Money account"),
        choices=(
            ("bank", _("Bank")),
            ("petty_cash", _("Petty cash")),
            ("psp_clearing", _("PSP clearing")),
        ),
    )
    refund_kind = forms.ChoiceField(
        label=_("Return kind"),
        choices=(("refund", _("Refund")), ("chargeback", _("Chargeback"))),
    )
    outcome = forms.ChoiceField(
        label=_("Rehearsal state"),
        choices=(
            ("sending", _("Sending")),
            ("confirmed", _("Confirmed")),
            ("unknown", _("Unknown outcome")),
            ("rejected", _("Rejected")),
        ),
    )
    evidence_reference = forms.UUIDField(label=_("Retained evidence reference"))
    outcome_evidence = forms.UUIDField(
        label=_("Outcome evidence reference"), required=False
    )
    attested = forms.BooleanField(
        label=_("I independently checked the retained evidence."),
    )
    reason = forms.ChoiceField(
        label=_("Reopening reason"),
        choices=(
            ("correction", _("Correction")),
            ("reconciliation", _("Reconciliation")),
        ),
    )

    def __init__(
        self,
        data: QueryDict | None = None,
        *,
        action: str,
        choices: Mapping[str, Sequence[tuple[str, str]]],
    ) -> None:
        """Keep valid posted selectors restricted to currently visible scoped rows."""
        super().__init__(
            data=data,
            initial={"command_id": uuid4(), "discount": "0,00", "adjustment": "0,00"},
            auto_id=f"id_{action}_%s",
        )
        self.action = action
        self.action_label = ACTION_LABELS[action]
        for name in tuple(self.fields):
            if name not in ACTION_FIELDS[action]:
                del self.fields[name]
        for name, values in choices.items():
            if name in self.fields:
                field = self.fields[name]
                field_class = (
                    forms.MultipleChoiceField
                    if isinstance(field, forms.MultipleChoiceField)
                    else forms.ChoiceField
                )
                self.fields[name] = field_class(
                    label=field.label, choices=values, required=field.required
                )
        if action == "payout_state":
            self.fields["outcome"] = forms.ChoiceField(
                label=_("Rehearsal state"),
                choices=(
                    ("sending", _("Sending")),
                    ("confirmed", _("Confirmed")),
                    ("unknown", _("Unknown outcome")),
                ),
            )
        if action == "psp_fee":
            self.fields["account"] = forms.ChoiceField(
                label=_("Money account"),
                choices=(("bank", _("Bank")), ("psp_clearing", _("PSP clearing"))),
            )

    def clean_amount(self) -> int:
        """Parse exact text and round once at the documented input boundary."""
        return self._money("amount")

    @property
    def available(self) -> bool:
        """Empty required selectors explain a prerequisite instead of a dead submit."""
        return all(
            not field.required or bool(tuple(cast("Iterable[object]", field.choices)))
            for field in self.fields.values()
            if isinstance(field, forms.ChoiceField)
        )

    def clean_counted(self) -> int:
        """Allow a zero count, never a negative count."""
        return self._money("counted")

    def clean_discount(self) -> int:
        """Discounts reduce the repasse base before collection."""
        return self._money("discount")

    def clean_adjustment(self) -> int:
        """Payer adjustments reduce the repasse base, not the clinic's fee expense."""
        return self._money("adjustment")

    def _money(self, name: str) -> int:
        try:
            return round_reais(str(self.cleaned_data[name]).replace(",", "."))
        except JournalInputError as error:
            raise ValidationError(
                _("Enter an exact nonnegative amount in reais.")
            ) from error


class JournalExportForm(forms.Form):
    """Only a date window and documented output format can leave the clinic book."""

    start = forms.DateField(
        label=_("From"), widget=forms.DateInput(attrs={"type": "date"})
    )
    end = forms.DateField(
        label=_("Through"), widget=forms.DateInput(attrs={"type": "date"})
    )
    format = forms.ChoiceField(
        label=_("Export format"),
        choices=(("csv", "CSV"), ("journal", _("OFX-like journal XML"))),
    )

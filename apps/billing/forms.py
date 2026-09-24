"""Native billing forms: exact BRL input and an explicit settlement attestation."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Final

from django import forms
from django.core.exceptions import ValidationError

if TYPE_CHECKING:
    from collections.abc import Sequence
    from uuid import UUID

CENTAVOS: Final = 2
MAX_DIGITS: Final = 12
MIN_AMOUNT: Final = Decimal("0.01")


def amount_in_centavos(amount: Decimal) -> int:
    """Convert an exact decimal of reais into stored centavos, never rounding."""
    minor = amount.scaleb(CENTAVOS)
    if minor != minor.to_integral_value():
        message = "Informe no máximo dois decimais."
        raise ValidationError(message)
    return int(minor)


class ChargeForm(forms.Form):
    """Open one draft charge for an enrolled patient of this clinic.

    The submission carries no operation key: the server names the operation
    from this session and these exact terms, so a resubmitted page - refresh,
    browser Back, double click - asks for the same charge instead of a second
    one. ``repeat_of`` is the opposite request, and the only way to ask for a
    genuinely second charge with the same patient and the same value: it names
    the charge this one deliberately repeats.
    """

    patient_id = forms.ChoiceField(label="Paciente", choices=[])
    amount = forms.DecimalField(
        label="Valor em reais (R$)",
        max_digits=MAX_DIGITS,
        decimal_places=CENTAVOS,
        min_value=MIN_AMOUNT,
        localize=True,
        help_text="Use vírgula para os centavos, por exemplo 180,00.",
    )
    repeat_of = forms.UUIDField(widget=forms.HiddenInput(), required=False)

    def __init__(
        self,
        *args: object,
        patients: Sequence[tuple[UUID, str]] = (),
        **kwargs: object,
    ) -> None:
        """Bind the clinic's enrolled patients as the only allowed choices."""
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.fields["patient_id"].choices = [  # type: ignore[attr-defined]
            (str(patient_id), name) for patient_id, name in patients
        ]

    def clean_amount(self) -> int:
        """Return the exact integer centavos the service requires."""
        amount = self.cleaned_data["amount"]
        if not isinstance(amount, Decimal):  # pragma: no cover - field guarantees it
            message = "Informe um valor válido."
            raise ValidationError(message)
        return amount_in_centavos(amount)


class SettlementForm(forms.Form):
    """Attest one settlement that was verified outside this screen."""

    confirmation_reference = forms.UUIDField(
        label="Identificador da comprovação",
        help_text=(
            "Identificador da evidência guardada fora do sistema. "
            "Repetir o mesmo identificador devolve o mesmo recibo."
        ),
    )
    amount = forms.DecimalField(
        label="Valor confirmado em reais (R$)",
        max_digits=MAX_DIGITS,
        decimal_places=CENTAVOS,
        min_value=MIN_AMOUNT,
        localize=True,
        help_text="Precisa ser igual ao valor da cobrança.",
    )
    attested = forms.BooleanField(
        label="Confirmo que verifiquei o pagamento fora deste sistema.",
        required=True,
    )

    def clean_amount(self) -> int:
        """Return the exact integer centavos compared against the frozen terms."""
        amount = self.cleaned_data["amount"]
        if not isinstance(amount, Decimal):  # pragma: no cover - field guarantees it
            message = "Informe um valor válido."
            raise ValidationError(message)
        return amount_in_centavos(amount)

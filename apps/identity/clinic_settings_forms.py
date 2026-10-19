"""Explicit settings fields; never a generic model or JSON editor."""

from typing import Any

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.identity.clinic_configuration import REMINDER_HOURS
from apps.intake.branching import is_number


class ClinicSettingsForm(forms.Form):
    """Edit only approved contacts, a fixed branding token and future schedule."""

    expected_version = forms.IntegerField(min_value=0, widget=forms.HiddenInput)
    display_name = forms.CharField(label="Nome de apresentação", max_length=120)
    contact_email = forms.EmailField(label="E-mail de contato", required=False)
    contact_phone = forms.RegexField(
        label="Telefone de contato",
        regex=r"^\+[1-9][0-9]{7,14}$",
        required=False,
        help_text="Formato internacional, por exemplo +5511999990000.",
    )
    brand_token = forms.ChoiceField(
        label="Identidade visual", choices=(("navy", "Azul"), ("teal", "Verde"))
    )
    reminder_hours = forms.TypedChoiceField(
        label="Lembrete antes da consulta",
        coerce=int,
        choices=[(value, f"{value} horas") for value in REMINDER_HOURS],
    )
    logo = forms.FileField(
        label="Logo PNG ou JPEG",
        required=False,
        help_text="Até 256 KiB e 1024 x 1024 pixels. Verificação obrigatória.",
    )
    remove_logo = forms.BooleanField(label="Remover logo", required=False)


class SpecialtyOverlayForm(forms.Form):
    """Publish all four SOAP prompts as one immutable specialty version."""

    key = forms.RegexField(label="Código da especialidade", regex=r"^[a-z0-9_-]{1,64}$")
    title = forms.CharField(label="Título do modelo", max_length=160)
    subjective = forms.CharField(
        label="Orientação: subjetivo",
        max_length=1000,
        widget=forms.Textarea,
        required=False,
    )
    objective = forms.CharField(
        label="Orientação: objetivo",
        max_length=1000,
        widget=forms.Textarea,
        required=False,
    )
    assessment = forms.CharField(
        label="Orientação: avaliação",
        max_length=1000,
        widget=forms.Textarea,
        required=False,
    )
    plan = forms.CharField(
        label="Orientação: plano",
        max_length=1000,
        widget=forms.Textarea,
        required=False,
    )


QUESTION_ROWS = 5
QUESTION_TYPES = (
    ("text", "Texto livre"),
    ("selection", "Escolha entre opções"),
    ("boolean", "Sim ou não"),
    ("number", _("Number")),
)
DEFAULT_TEXT_LIMIT = 500
DEFAULT_OPTION_LIMIT = 200
BOOLEAN_LIMIT = 5


class QuestionnaireForm(forms.Form):
    """Publish one typed pre-consultation version from fixed, explicit rows.

    Each row is one question: label, answer type, required flag, character
    limit and, for choices only, one option per line. There is no free-form
    schema input, expression or condition; the service re-validates the result.
    """

    key = forms.RegexField(label="Código do questionário", regex=r"^[a-z0-9_-]{1,64}$")
    title = forms.CharField(label="Título do questionário", max_length=160)

    def __init__(self, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401 - Form signature
        """Add the fixed question rows; only the first is mandatory.

        The specialty form on the same page also has ``key`` and ``title``;
        a distinct id prefix keeps every label bound to one control.
        """
        kwargs.setdefault("auto_id", "questionnaire_%s")
        super().__init__(*args, **kwargs)
        for index in range(1, QUESTION_ROWS + 1):
            self.fields[f"q{index}_label"] = forms.CharField(
                label="Texto da pergunta",
                max_length=200,
                required=index == 1,
            )
            self.fields[f"q{index}_type"] = forms.ChoiceField(
                label="Tipo de resposta",
                choices=QUESTION_TYPES,
                initial="text",
                required=False,
            )
            self.fields[f"q{index}_required"] = forms.BooleanField(
                label="Resposta obrigatória", required=False
            )
            self.fields[f"q{index}_max_length"] = forms.IntegerField(
                label="Limite de caracteres",
                min_value=1,
                max_value=4000,
                required=False,
                help_text="Opcional. Padrão: 500 para texto e 200 para opções.",
            )
            self.fields[f"q{index}_options"] = forms.CharField(
                label="Opções, uma por linha",
                max_length=6200,
                required=False,
                widget=forms.Textarea(attrs={"rows": 3}),
                help_text="Somente para escolha entre opções.",
            )
            self.fields[f"q{index}_when_question"] = forms.ChoiceField(
                label=_("Show only after question"),
                choices=[("", _("Always visible"))]
                + [(str(prior), str(prior)) for prior in range(1, index)],
                required=False,
            )
            self.fields[f"q{index}_when_op"] = forms.ChoiceField(
                label=_("Comparison"),
                choices=[
                    ("eq", _("Equals")),
                    ("in", _("One of")),
                    ("gt", _("Greater than")),
                    ("lt", _("Less than")),
                ],
                required=False,
            )
            self.fields[f"q{index}_when_value"] = forms.CharField(
                label=_("Comparison value"),
                max_length=4000,
                required=False,
                widget=forms.Textarea(attrs={"rows": 2}),
                help_text=_(
                    "Use yes or no for booleans. For One of, use one value per line."
                ),
            )

    def question_rows(self) -> list[list[forms.BoundField]]:
        """Group the bound fields by question for the fieldset layout."""
        names = (
            "label",
            "type",
            "required",
            "max_length",
            "options",
            "when_question",
            "when_op",
            "when_value",
        )
        return [
            [self[f"q{index}_{name}"] for name in names]
            for index in range(1, QUESTION_ROWS + 1)
        ]

    def clean(self) -> dict[str, Any]:
        """Turn the filled rows into the closed typed question schema."""
        cleaned = self.cleaned_data
        questions: list[dict[str, object]] = []
        for index in range(1, QUESTION_ROWS + 1):
            label = str(cleaned.get(f"q{index}_label") or "").strip()
            options = [
                line.strip()
                for line in str(cleaned.get(f"q{index}_options") or "").splitlines()
                if line.strip()
            ]
            if not label:
                if options:
                    self.add_error(
                        f"q{index}_label", "Informe a pergunta destas opções."
                    )
                continue
            kind = str(cleaned.get(f"q{index}_type") or "text")
            if kind != "selection" and options:
                self.add_error(
                    f"q{index}_options",
                    "Somente perguntas de escolha aceitam opções.",
                )
                continue
            limit = cleaned.get(f"q{index}_max_length")
            if kind == "boolean":
                limit = BOOLEAN_LIMIT
            elif limit is None:
                limit = (
                    DEFAULT_OPTION_LIMIT if kind == "selection" else DEFAULT_TEXT_LIMIT
                )
            questions.append(
                {
                    "id": f"q_{index}",
                    "label": label,
                    "type": kind,
                    "required": bool(cleaned.get(f"q{index}_required")),
                    "max_length": limit,
                    "options": options,
                }
            )
        cleaned["questions"] = questions
        rules = self._branch_rules(questions)
        if rules:
            cleaned["questions"] = {
                "schema_version": 2,
                "state": "published",
                "questions": questions,
                "rules": rules,
            }
        return cleaned

    @staticmethod
    def _comparison_value(kind: str, value: str) -> str | bool | float:
        """Parse one literal, never an expression or a nested value."""
        if kind == "boolean":
            booleans = {"yes": True, "no": False, "sim": True, "não": False}
            if value.casefold() not in booleans:
                raise forms.ValidationError(_("Use yes or no for this comparison."))
            return booleans[value.casefold()]
        if kind == "number":
            try:
                number = float(value)
            except ValueError:
                raise forms.ValidationError(_("Enter a finite number.")) from None
            if not is_number(number):
                raise forms.ValidationError(_("Enter a finite number."))
            return number
        return value

    def _branch_rules(
        self, questions: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        """Bind each conditional row to one earlier filled question."""
        cleaned = self.cleaned_data
        by_id = {question["id"]: question for question in questions}
        rules: list[dict[str, object]] = []
        for index in range(1, QUESTION_ROWS + 1):
            source = cleaned.get(f"q{index}_when_question")
            if not source:
                continue
            source_id = f"q_{source}"
            if source_id not in by_id or f"q_{index}" not in by_id:
                self.add_error(
                    f"q{index}_when_question", _("Choose an earlier filled question.")
                )
                continue
            raw = str(cleaned.get(f"q{index}_when_value") or "")
            op = str(cleaned.get(f"q{index}_when_op") or "eq")
            try:
                values = [
                    self._comparison_value(str(by_id[source_id]["type"]), line.strip())
                    for line in (raw.splitlines() if op == "in" else [raw])
                ]
            except forms.ValidationError as error:
                self.add_error(f"q{index}_when_value", error)
                continue
            rules.append(
                {
                    "when": {
                        "question": source_id,
                        "op": op,
                        "value": values if op == "in" else values[0],
                    },
                    "show": [f"q_{index}"],
                }
            )
        return rules

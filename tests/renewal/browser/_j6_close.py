"""One reference-unit day close for J6 (todo 75 ``journey-financial-close``).

The close's browser facts are created in the same unit and day: a charge
issued and settled on the charge screen
(the confirmed settlement bridges into the journal), its frozen terms with a
practitioner rule and the attested service completion, a PSP fee, a refund
and a practitioner payout approved under fresh step-up with recorded rehearsal
outcomes, and an unknown refund outcome handled as an explicit exception that
blocks the close until its outcome is evidenced. The additional fiscal and
insurer service legs feed this same journal before closing and exporting it.
"""

from __future__ import annotations

import csv
import io
from datetime import date, timedelta
from typing import TYPE_CHECKING, Final
from uuid import uuid4

from django.utils.translation import gettext
from playwright.sync_api import expect

from renewal.browser._navigation import click_to_navigate, goto_settled

if TYPE_CHECKING:
    from playwright.sync_api import Page

OK: Final = 200
SUCCESS: Final = (
    "Financial command recorded. The displayed balances come from the journal."
)
STEP_UP_COPY: Final = "Verify your authenticator again before approving or reopening."
CHARGE: Final = 18000
FEE: Final = 250
REFUND: Final = 2000
PAYOUT: Final = 1000
RULE_BASIS_POINTS: Final = 3000


def reais(minor: int) -> str:
    """Format centavos the way a person types them into the Finance forms."""
    return f"{minor // 100},{minor % 100:02d}"


def finance(page: Page, base: str, clinic: str) -> None:
    goto_settled(page, f"{base}/billing/clinics/{clinic}/finance/")
    expect(page.locator("[data-finance-workspace]")).to_be_visible()


def command(page: Page, action: str, values: dict[str, str | int | bool]) -> None:
    """Fill one Finance command through its own form and submit it.

    Booleans tick a checkbox, integers pick a select option by index, and text
    goes into a select by value or into a text field, by the element's kind.
    """
    form = page.locator(f'form:has(input[name="action"][value="{action}"])')
    details = page.locator("details.panel").filter(has=form)
    if details.get_attribute("open") is None:
        details.locator("summary").click()
    expect(form).to_be_visible()
    for key, value in values.items():
        field = page.locator(f"#id_{action}_{key}")
        if isinstance(value, bool):
            field.check()
        elif isinstance(value, int):
            field.select_option(index=value)
        elif field.evaluate("element => element.tagName") == "SELECT":
            field.select_option(value)
        else:
            field.fill(value)
    click_to_navigate(form.locator("button[type=submit]"))


def recorded(page: Page) -> None:
    expect(page.get_by_text(gettext(SUCCESS))).to_be_visible()


def needs_step_up(page: Page) -> None:
    expect(page.get_by_text(gettext(STEP_UP_COPY))).to_be_visible()


def evidence() -> str:
    return str(uuid4())


def drawer(page: Page) -> tuple[str, int]:
    row = page.locator("tr[data-cash-state]").first
    state = row.get_attribute("data-cash-state")
    assert state is not None
    return state, int(row.locator("td").nth(1).inner_text().strip())


def journal_totals(page: Page, base: str, clinic: str) -> tuple[int, dict[str, int]]:
    """Download the accountant export; return its line count and net by account.

    Each sealed entry must balance on its own; the net of an account is its
    debits minus its credits in centavos.
    """
    tokens = [c for c in page.context.cookies() if "csrf" in c["name"].lower()]
    assert len(tokens) == 1
    today = date.today()  # noqa: DTZ011 - a generous window around the server day
    response = page.request.post(
        f"{base}/billing/clinics/{clinic}/journal-export/",
        form={
            "csrfmiddlewaretoken": str(tokens[0]["value"]),
            "start": (today - timedelta(days=3)).isoformat(),
            "end": (today + timedelta(days=3)).isoformat(),
            "format": "csv",
        },
        headers={"Referer": page.url},
    )
    assert response.status == OK, response.status
    rows = list(csv.DictReader(io.StringIO(response.text())))
    entries: dict[str, int] = {}
    accounts: dict[str, int] = {}
    for row in rows:
        assert row["side"] in {"debit", "credit"}, row["side"]
        assert row["currency"] == "BRL"
        signed = int(row["amount_minor"]) * (1 if row["side"] == "debit" else -1)
        entries[row["entry_id"]] = entries.get(row["entry_id"], 0) + signed
        accounts[row["account"]] = accounts.get(row["account"], 0) + signed
    assert not [entry for entry, total in entries.items() if total]
    return len(rows), accounts

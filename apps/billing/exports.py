"""Exact clinic-scoped accountant exports from sealed journal lines only."""

from __future__ import annotations

import csv
from datetime import datetime, time, timedelta
from io import StringIO
from typing import TYPE_CHECKING, Final, cast
from xml.etree.ElementTree import Element, SubElement, tostring
from zoneinfo import ZoneInfo

from apps.audit.services import record_phase1_event
from apps.billing.models import JournalLine
from apps.billing.money_types import JournalInputError
from apps.billing.security import scoped_book

if TYPE_CHECKING:
    from datetime import date
    from uuid import UUID

MAX_EXPORT_DAYS: Final = 366
COLUMNS: Final = (
    "entry_id",
    "posted_at",
    "account",
    "side",
    "amount_minor",
    "currency",
    "source_kind",
    "source_id",
    "reversal_of",
    "practitioner_id",
    "owner_id",
    "due_on",
)


def export_journal(
    *, clinic_id: UUID, start: date, end: date, output_format: str
) -> bytes:
    """Export one inclusive clinic-local date window, never SaaS or other clinics."""
    book = scoped_book(clinic_id=clinic_id, permission="settlement.post")
    if (
        output_format not in {"csv", "journal"}
        or not 0 <= (end - start).days < MAX_EXPORT_DAYS
    ):
        raise JournalInputError
    # The persisted clinic's required timezone is validated by its field.
    zone = ZoneInfo(cast("str", book.clinic.timezone))
    first = datetime.combine(start, time.min, tzinfo=zone)
    after = datetime.combine(end + timedelta(days=1), time.min, tzinfo=zone)
    rows = (
        JournalLine.objects.filter(
            entry__book=book,
            entry__state="posted",
            entry__posted_at__gte=first,
            entry__posted_at__lt=after,
        )
        .select_related("entry", "account")
        .order_by("entry__posted_at", "entry_id", "id")
    )
    stream = StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    document = Element("journal", version="clinic-journal-v1", currency="BRL")
    previous = None
    node = document
    writer.writerow(COLUMNS)
    for row in rows:
        values = (
            str(row.entry_id),
            # The sealing trigger sets posted_at for every posted entry.
            cast("datetime", row.entry.posted_at).isoformat(),
            row.account.code,
            row.side,
            str(row.amount_minor),
            "BRL",
            row.entry.source_kind,
            str(row.entry.source_id),
            str(row.entry.reversal_of_id or ""),
            str(row.practitioner_id or ""),
            str(row.owner_id or ""),
            row.due_on.isoformat() if row.due_on else "",
        )
        writer.writerow(values)
        if row.entry_id != previous:
            node = SubElement(document, "entry", id=str(row.entry_id))
            previous = row.entry_id
        line = SubElement(node, "line")
        for field, value in zip(COLUMNS, values, strict=True):
            SubElement(line, field).text = value
    record_phase1_event(
        "billing.journal.exported", clinic_id=clinic_id, affected_record_id=book.pk
    )
    if output_format == "csv":
        return stream.getvalue().encode("utf-8")
    return cast("bytes", tostring(document, encoding="utf-8", xml_declaration=True))

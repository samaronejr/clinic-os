"""Read the metric and window of the governed result a Reports page shows."""

from __future__ import annotations

from html.parser import HTMLParser
from typing import Final

# The no_show refresh of 02/06/2035 covers that clinic-local day, end exclusive.
REPORT_DAY_DESCRIPTOR: Final = {
    "metric": "no_show",
    "start": "2035-06-02 00:00",
    "end": "2035-06-03 00:00",
}


class _Report(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.found: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name: value or "" for name, value in attrs}
        if tag == "section" and "data-report-state" in values:
            self.found["state"] = values["data-report-state"]
        if values.get("id") == "metric-caption":
            for field in ("metric", "start", "end"):
                self.found[field] = values.get(f"data-report-{field}", "")


def governed_report(html: str) -> dict[str, str]:
    """Return state, metric, start and end as rendered with the governed result."""
    parser = _Report()
    parser.feed(html)
    return parser.found

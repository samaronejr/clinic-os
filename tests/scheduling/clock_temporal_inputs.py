"""Count literal values with server-derived clock prefixes, irrespective of type."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import cast

from django.db import connection

from .clock_datetime_tokens import special_datetime_tokens
from .clock_literals import literal_values


class LiteralInputs:
    def __init__(self) -> None:
        self.tokens = special_datetime_tokens()
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_setting('standard_conforming_strings')")
            row = cursor.fetchone()
        assert row is not None
        self.standard_strings = row[0] == "on"
        self.allowlist = cast(
            "dict[str, dict[str, str]]",
            json.loads(
                Path(__file__).with_name("clock_literal_allowlist.json").read_text()
            ),
        )
        assert all(
            entry.get("reason") and entry.get("scope") == "python-mapping-key"
            for entry in self.allowlist.values()
        ), "literal exceptions need exact scope and reason"

    def risky(self, value: str) -> bool:
        return value.strip().casefold().startswith(tuple(self.tokens))

    def python_risky(self, value: str, *, mapping_key: bool) -> bool:
        return self.risky(value) and not (mapping_key and value in self.allowlist)

    def counts(self, source: str) -> Counter[str]:
        count = sum(
            self.risky(value)
            for value in literal_values(source, standard_strings=self.standard_strings)
        )
        return Counter({"unresolved-temporal-input": count}) if count else Counter()

    def unresolved(self, source: str) -> bool:
        return bool(self.counts(source))

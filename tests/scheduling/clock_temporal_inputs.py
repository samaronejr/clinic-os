"""Count literals containing server-derived clock words, irrespective of type."""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import cast

from django.db import connection

from .clock_datetime_tokens import special_datetime_tokens
from .clock_literals import literal_values


class LiteralInputs:
    def __init__(self) -> None:
        self.tokens = special_datetime_tokens()
        self.words = re.compile(
            r"(?<![a-z])(?:"
            + "|".join(map(re.escape, sorted(self.tokens)))
            + r")(?![a-z])"
        )
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
            entry.get("reason")
            and entry.get("scope") in {"python-mapping-key", "python-docstring"}
            for entry in self.allowlist.values()
        ), "literal exceptions need exact scope and reason"

    def risky(self, value: str) -> bool:
        return self.words.search(value.casefold()) is not None

    def python_risky(
        self, value: str, *, mapping_key: bool, docstring: bool = False
    ) -> bool:
        scope = self.allowlist.get(value, {}).get("scope")
        exempt = (mapping_key and scope == "python-mapping-key") or (
            docstring and scope == "python-docstring"
        )
        return self.risky(value) and not exempt

    def counts(self, source: str) -> Counter[str]:
        count = sum(
            self.risky(value)
            for value in literal_values(source, standard_strings=self.standard_strings)
        )
        return Counter({"unresolved-temporal-input": count}) if count else Counter()

    def unresolved(self, source: str) -> bool:
        return bool(self.counts(source))

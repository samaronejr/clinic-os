"""Equivalent encodings used across pure and real-gateway boundary regressions."""

from __future__ import annotations

import json

SERIALIZATIONS = ("compact", "pretty", "reordered", "escaped")


def serialize(raw: str, form: str) -> str:
    data = json.loads(raw)
    if form == "pretty":
        return json.dumps(data, indent=2)
    if form == "reordered":
        data = json.loads(raw, object_pairs_hook=lambda pairs: dict(reversed(pairs)))
    encoded = json.dumps(data, separators=(",", ":"))
    if form == "escaped":
        for plain, escaped in (
            ('"schema"', '"\\u0073chema"'),
            ("decision-support", "\\u0064ecision-support"),
            ("chart-request", "\\u0063hart-request"),
            ('"diagnosis"', '"\\u0064iagnosis"'),
            ('"key"', '"\\u006bey"'),
        ):
            encoded = encoded.replace(plain, escaped)
    else:
        assert form in {"compact", "reordered"}
    return encoded

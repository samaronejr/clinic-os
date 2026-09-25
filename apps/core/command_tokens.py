"""Opaque, session-bound tokens that stand in for record selectors in the palette.

A palette row that acts on a record (a patient) carries only a random token;
the enrollment it names stays in the server-side session, bound to the clinic
it was issued for. Unknown, evicted, foreign-clinic and forged tokens all
redeem to ``None`` so callers answer with one identical denial.
"""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from uuid import UUID

    from django.contrib.sessions.backends.base import SessionBase

SESSION_KEY: Final = "workspace.command.tokens"
MAX_TOKENS: Final = 50
TOKEN_BYTES: Final = 18
_ENTRY_FIELDS: Final = 3


def _entries(session: SessionBase) -> dict[str, list[str]]:
    raw = session.get(SESSION_KEY)
    if not isinstance(raw, dict):
        return {}
    return {
        str(token): [str(part) for part in entry]
        for token, entry in raw.items()
        if isinstance(entry, list) and len(entry) == _ENTRY_FIELDS
    }


def issue(session: SessionBase, *, clinic_id: UUID, kind: str, subject: str) -> str:
    """Store one selector behind a fresh token; the oldest tokens are evicted."""
    entries = _entries(session)
    token = secrets.token_urlsafe(TOKEN_BYTES)
    entries[token] = [str(clinic_id), kind, subject]
    while len(entries) > MAX_TOKENS:
        entries.pop(next(iter(entries)))
    session[SESSION_KEY] = entries
    return token


def peek(
    session: SessionBase, *, clinic_id: UUID, token: str
) -> tuple[str, str] | None:
    """Return ``(kind, subject)`` for a token issued in this clinic, else ``None``."""
    entry = _entries(session).get(token) if isinstance(token, str) else None
    if entry is None or entry[0] != str(clinic_id):
        return None
    return entry[1], entry[2]


def consume(session: SessionBase, *, token: str) -> None:
    """Spend a token once its command succeeded."""
    entries = _entries(session)
    if entries.pop(token, None) is not None:
        session[SESSION_KEY] = entries

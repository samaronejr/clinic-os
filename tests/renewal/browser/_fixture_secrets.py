"""Credentials the browser fixtures hold, kept out of every failure report.

Two layers, so that no suite failure can print a DSN, a password, a TOTP
seed, an access code or the run's KEK into ``pytest.log`` or the JUnit file
the runner uploads (hosted retention@firefox run 36349167980 printed the
owner DSN, password included, from ``availability_staff``'s argument line):

* Construction. Fixtures build every such value as a ``FixtureSecret``: a
  ``str`` whose ``repr`` is ``<fixture secret>``. Clients (psycopg,
  Playwright ``fill``, ``make_password``, subprocess environments) receive
  the value unchanged, while pytest's argument lines, ``--showlocals`` and
  assertion introspection render the ``repr``. Each value is also recorded
  for the second layer.
* Output. The ``pytest_runtest_makereport``/``pytest_make_collect_report``
  wrappers rewrite every report before the terminal or the JUnit writer sees
  it: every recorded value, the secret runner inputs (``SECRET_ENVIRONMENT``
  and the password inside each DSN) and any ``scheme://user:password@``
  credential become ``<fixture secret>``. That covers what construction
  cannot: a plain ``str`` derived from a secret (f-strings, slicing,
  ``str.replace``), assertion diffs (``==``/``in`` compare text, not
  ``repr``), exception messages and captured output.

``conftest.py`` re-exports the two hooks; ``tests/renewal/test_browser_runner.py``
runs a failing suite with and without them.
"""

from __future__ import annotations

import base64
import dataclasses
import os
import re
import secrets
from typing import TYPE_CHECKING, Final, Self
from urllib.parse import quote, unquote, urlsplit

import pytest
from _pytest._code.code import TerminalRepr

if TYPE_CHECKING:
    from collections.abc import Generator

    from _pytest._code.code import ReprFileLocation
    from _pytest._io import TerminalWriter
    from _pytest.reports import CollectReport, TestReport

REDACTED: Final = "<fixture secret>"
# Runner inputs that carry a credential (ops/testing/renewal_runner.py).
OWNER_PASSWORD: Final = "CLINIC_RENEWAL_PASSWORD"  # noqa: S105 - a variable name
SECRET_ENVIRONMENT: Final = (
    "CLINIC_RENEWAL_FIXTURE_DATABASE_URL",
    "CLINIC_RENEWAL_WORKER_DATABASE_URL",
    OWNER_PASSWORD,
)
# Shorter values could collide with ordinary report text; no fixture secret
# is this short (token_urlsafe(24) is 32 characters, a TOTP seed 40).
MIN_SECRET_LENGTH: Final = 8
# The shortest piece of a secret that counts as disclosing it.
FRAGMENT: Final = 6
CREDENTIAL_URL: Final = re.compile(
    r"(?P<prefix>\b[a-zA-Z][a-zA-Z0-9+.-]*://[^\s:/@]*:)[^\s@/]+(?=@)"
)
_RECORDED: set[str] = set()


class FixtureSecret(str):
    """A credential value whose ``repr`` never reveals it."""

    __slots__ = ()

    def __new__(cls, value: str) -> Self:
        """Record ``value`` for the report backstop and wrap it."""
        if len(value) >= MIN_SECRET_LENGTH:
            _RECORDED.add(value)
        return super().__new__(cls, value)

    def __repr__(self) -> str:
        """Render the placeholder instead of the value."""
        return REDACTED


def environment_secret(name: str) -> FixtureSecret:
    """Return the runner input ``name`` (one of ``SECRET_ENVIRONMENT``)."""
    assert name in SECRET_ENVIRONMENT, name
    return FixtureSecret(os.environ[name])


def fixture_dsn() -> FixtureSecret:
    """The owner DSN the runner lends fixtures for seeding."""
    return environment_secret("CLINIC_RENEWAL_FIXTURE_DATABASE_URL")


def worker_dsn() -> FixtureSecret:
    """The runtime-role DSN the runner lends suites that spawn workers."""
    return environment_secret("CLINIC_RENEWAL_WORKER_DATABASE_URL")


def new_password() -> FixtureSecret:
    """A fresh staff password for a seeded account."""
    return FixtureSecret(secrets.token_urlsafe(24))


def new_totp_key() -> FixtureSecret:
    """A fresh hex TOTP seed for a seeded authenticator."""
    return FixtureSecret(secrets.token_hex(20))


def new_access_code() -> FixtureSecret:
    """A fresh patient access (invitation) code."""
    return FixtureSecret(secrets.token_urlsafe(32))


HEX_SECRET: Final = re.compile(r"(?:[0-9a-fA-F]{2}){8,}")


def _credentials(value: str) -> set[str]:
    """``value`` in every spelling a report can show.

    For a URL, the password inside it (raw, unquoted, quoted). For a hex
    secret (a TOTP seed), also the decoded bytes as ``repr`` shows them
    (``bytes.fromhex(seed)`` in an assertion or a local) and as base32 (the
    authenticator's own spelling).
    """
    found = {value}
    password = urlsplit(value).password if "://" in value else None
    if password:
        found.update({password, unquote(password), quote(password, safe="")})
    if HEX_SECRET.fullmatch(value):
        raw = bytes.fromhex(value)
        encoded = base64.b32encode(raw).decode("ascii")
        found.update({repr(raw)[2:-1], encoded, encoded.rstrip("=")})
    return found


def _known() -> tuple[list[str], set[str]]:
    """(values longest first, their secret-only fragments of FRAGMENT chars)."""
    values: set[str] = set()
    for value in (*_RECORDED, *(os.environ.get(n, "") for n in SECRET_ENVIRONMENT)):
        values.update(_credentials(value))
    values = {value for value in values if len(value) >= MIN_SECRET_LENGTH}
    # A URL's scheme, user and host are not secret; only its password is.
    atoms = [value for value in values if "://" not in value]
    fragments = {
        atom[start : start + FRAGMENT]
        for atom in atoms
        for start in range(len(atom) - FRAGMENT + 1)
    }
    # Longest first, so a DSN goes before the password inside it.
    return sorted(values, key=len, reverse=True), fragments


def _mask_fragments(text: str, fragments: set[str]) -> str:
    """Redact every run of ``text`` covered by FRAGMENT-long secret pieces.

    pytest truncates a long ``repr`` to ``head...tail``, so a plain string
    that embeds a secret can show part of it; exact matching misses that.
    """
    covered = [False] * len(text)
    for start in range(len(text) - FRAGMENT + 1):
        if text[start : start + FRAGMENT] in fragments:
            covered[start : start + FRAGMENT] = [True] * FRAGMENT
    if not any(covered):
        return text
    pieces: list[str] = []
    index = 0
    while index < len(text):
        if covered[index]:
            while index < len(text) and covered[index]:
                index += 1
            pieces.append(REDACTED)
        else:
            pieces.append(text[index])
            index += 1
    return "".join(pieces)


def scrub(text: str) -> str:
    """``text`` with every known secret, URL credential and piece redacted."""
    values, fragments = _known()
    for value in values:
        text = text.replace(value, REDACTED)
    text = CREDENTIAL_URL.sub(rf"\g<prefix>{REDACTED}", text)
    return _mask_fragments(text, fragments)


class ScrubbedRepr(TerminalRepr):
    """A failure representation already rendered and scrubbed.

    It keeps what pytest's consumers read: ``toterminal`` (terminal,
    ``longreprtext`` and ``str``, the JUnit body) and ``reprcrash`` (JUnit
    ``message``, the ``-r`` summary line).
    """

    def __init__(self, text: str, reprcrash: ReprFileLocation | None) -> None:
        """Hold the rendered ``text`` and the scrubbed crash location."""
        self.text = text
        self.reprcrash = reprcrash

    def toterminal(self, tw: TerminalWriter) -> None:
        """Write the scrubbed text line by line."""
        for line in self.text.split("\n"):
            tw.line(line)


def scrub_report(report: TestReport | CollectReport) -> None:
    """Rewrite ``report``'s failure text and captured sections in place."""
    longrepr = report.longrepr
    if isinstance(longrepr, tuple):
        path, lineno, reason = longrepr
        report.longrepr = (path, lineno, scrub(reason))
    elif longrepr is not None:
        crash = getattr(longrepr, "reprcrash", None)
        report.longrepr = ScrubbedRepr(
            scrub(report.longreprtext),
            None
            if crash is None
            else dataclasses.replace(crash, message=scrub(crash.message)),
        )
    report.sections = [(title, scrub(content)) for title, content in report.sections]


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport() -> Generator[None, TestReport, TestReport]:
    """Scrub each setup/call/teardown report before anything renders it."""
    report = yield
    scrub_report(report)
    return report


@pytest.hookimpl(wrapper=True)
def pytest_make_collect_report() -> Generator[None, CollectReport, CollectReport]:
    """Scrub collection errors (import-time failures) the same way."""
    report = yield
    scrub_report(report)
    return report

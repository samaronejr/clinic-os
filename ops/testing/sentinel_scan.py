"""Reusable PHI-sentinel sink scanner (todo 73; reused by todo 75 and F2).

A test or suite injects synthetic sentinel strings (``new_sentinel``) through a
real request, then hands every sink it can observe to one ``SentinelScan``:

* ``logs``: what the configured root handlers actually emit
  (``capture_emitted_logs`` mirrors each handler's filters and formatter);
* ``urls``: request paths, redirect headers and URLs found in HTML bodies;
* ``database``: every text, JSON, array and bytea column of every base table
  (audit rows, outbox rows and their ``last_error`` included), read through a
  caller-supplied superuser cursor so RLS hides nothing;
* ``sse`` / ``push``: event frames or payload bytes the caller captured;
* ``files``: a published evidence tree (report.json, access.log, axe JSON).

A sink matches a sentinel in raw, case-folded or percent-encoded form. The
result never echoes a matched value, only the sink, location and sentinel
label. The CLI scans artifact trees or a database for the shared prefix::

    python -m ops.testing.sentinel_scan artifacts --root DIR [--root ...] --output PATH
    python -m ops.testing.sentinel_scan database --database-url-env NAME --output PATH
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import secrets
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from html.parser import HTMLParser
from io import StringIO
from pathlib import Path
from typing import TYPE_CHECKING, Final, Never, Protocol
from urllib.parse import quote, quote_plus

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ops.testing.isolation_common import IsolationError, canonical_bytes

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Sequence

    from ops.testing.isolation_common import JsonObject, JsonValue

SENTINEL_PREFIX: Final = "SINTETICO-SENTINELA"
SINKS: Final = ("logs", "urls", "database", "audit", "outbox", "sse", "push", "files")
LABEL: Final = re.compile(r"[A-Z0-9]+(?:-[A-Z0-9]+)*")
SENTINEL: Final = re.compile(rf"{SENTINEL_PREFIX}[-A-Za-z0-9 ]*")
TEXT_SUFFIXES: Final = frozenset(
    {".json", ".jsonl", ".log", ".txt", ".xml", ".html", ".md", ".csv", ".yaml"}
)
MAX_FILE_BYTES: Final = 64 * 1024 * 1024
URL_ATTRIBUTES: Final = frozenset({"href", "src", "action", "formaction", "hx-get"})
URL_HEADERS: Final = ("Location", "HX-Redirect", "HX-Location", "Content-Location")
DATABASE_TYPES: Final = (
    "text",
    "character varying",
    "character",
    "json",
    "jsonb",
    "ARRAY",
    "bytea",
)
AUDIT_TABLE: Final = re.compile(r"audit_")
OUTBOX_TABLE: Final = re.compile(r"(operation|outbox|attempt|delivery|webhook)")


class Cursor(Protocol):
    """The DB-API cursor subset ``SentinelScan.database`` needs."""

    def execute(self, query: str, params: Sequence[object] = ..., /) -> object:
        """Run one statement."""
        ...

    def fetchall(self) -> list[tuple[object, ...]]:
        """Return every row of the last statement."""
        ...


def _fail(message: str) -> Never:
    raise IsolationError(message)


def new_sentinel(label: str) -> str:
    """Return one fresh synthetic sentinel, e.g. ``SINTETICO-SENTINELA-NAME-1a2b``."""
    if LABEL.fullmatch(label) is None:
        _fail("sentinel label must be upper-case words joined by hyphens")
    return f"{SENTINEL_PREFIX}-{label}-{secrets.token_hex(6).upper()}"


@dataclass(frozen=True)
class Finding:
    """One sentinel observed in one sink location (the value is never kept)."""

    sink: str
    location: str
    sentinel: str


@dataclass
class SentinelScan:
    """Accumulate every observed sink and the sentinels found in it."""

    sentinels: tuple[str, ...]
    findings: list[Finding] = field(default_factory=list)
    scanned: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Refuse an empty or non-sentinel needle set."""
        if not self.sentinels:
            _fail("a sentinel scan needs at least one sentinel")
        for sentinel in self.sentinels:
            if SENTINEL.fullmatch(sentinel) is None:
                _fail("every needle must start with the synthetic sentinel prefix")

    def text(self, sink: str, location: str, value: str | bytes) -> None:
        """Scan one value observed in ``sink`` at ``location``."""
        if sink not in SINKS:
            _fail(f"unknown sentinel sink {sink!r}")
        self.scanned[sink] = self.scanned.get(sink, 0) + 1
        haystack = (
            value.decode("utf-8", "replace") if isinstance(value, bytes) else value
        ).casefold()
        for sentinel in self.sentinels:
            if any(form in haystack for form in _forms(sentinel)):
                self.findings.append(Finding(sink, location, _label(sentinel)))

    def logs(self, lines: Iterable[str]) -> None:
        """Scan every emitted log line."""
        for number, line in enumerate(lines, start=1):
            self.text("logs", f"line {number}", line)
        self.scanned.setdefault("logs", 0)

    def urls(self, location: str, urls: Iterable[str]) -> None:
        """Scan request paths, redirect targets and page links."""
        for url in urls:
            self.text("urls", location, url)
        self.scanned.setdefault("urls", 0)

    def database(
        self, cursor: Cursor, schemas: Sequence[str] = ("clinic_app", "public")
    ) -> None:
        """Scan every text-like column of every base table in ``schemas``."""
        cursor.execute(
            "SELECT c.table_schema, c.table_name, c.column_name, c.data_type "
            "FROM information_schema.columns c JOIN information_schema.tables t "
            "USING (table_schema, table_name) "
            "WHERE t.table_type = 'BASE TABLE' AND c.table_schema = ANY(%s) "
            "AND c.data_type = ANY(%s) "
            "ORDER BY c.table_schema, c.table_name, c.ordinal_position",
            [list(schemas), list(DATABASE_TYPES)],
        )
        tables: dict[tuple[str, str], list[tuple[str, str]]] = {}
        for schema, table, column, data_type in cursor.fetchall():
            tables.setdefault((str(schema), str(table)), []).append(
                (str(column), str(data_type))
            )
        patterns = [f"%{_escape(form)}%" for s in self.sentinels for form in _forms(s)]
        for (schema, table), columns in tables.items():
            self._table(cursor, schema, table, columns, patterns)
        self.scanned.setdefault("database", 0)

    def _table(
        self,
        cursor: Cursor,
        schema: str,
        table: str,
        columns: list[tuple[str, str]],
        patterns: list[str],
    ) -> None:
        checks = ", ".join(
            f"coalesce(bool_or({_cell(column, data_type)} ILIKE ANY(%s)), false)"
            for column, data_type in columns
        )
        cursor.execute(
            f'SELECT {checks} FROM "{schema}"."{table}"',  # noqa: S608 - catalog names
            [patterns] * len(columns),
        )
        (flags,) = cursor.fetchall()
        sink = (
            "audit"
            if AUDIT_TABLE.match(table)
            else "outbox"
            if OUTBOX_TABLE.search(table)
            else "database"
        )
        for (column, _type), flag in zip(columns, flags, strict=True):
            self.scanned[sink] = self.scanned.get(sink, 0) + 1
            if flag is True:
                self.findings.append(
                    Finding(sink, f"{schema}.{table}.{column}", "sentinel")
                )

    def files(self, root: Path) -> None:
        """Scan every text artifact below ``root``; binaries are only counted."""
        if root.is_symlink() or not root.is_dir():
            _fail(f"{root} is not a real artifact directory")
        for path in sorted(root.rglob("*")):
            if path.is_symlink() or not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            if path.suffix not in TEXT_SUFFIXES:
                self.scanned["binary"] = self.scanned.get("binary", 0) + 1
                continue
            if path.stat().st_size > MAX_FILE_BYTES:
                _fail(f"{relative} exceeds the artifact scan bound")
            self.text("files", relative, path.read_bytes())
        self.scanned.setdefault("files", 0)

    @property
    def clean(self) -> bool:
        """Return whether no sink held any sentinel."""
        return not self.findings

    def result(self) -> JsonObject:
        """Return the evidence record (labels only, never matched values)."""
        findings: list[JsonValue] = [
            {"sink": item.sink, "location": item.location, "sentinel": item.sentinel}
            for item in self.findings
        ]
        scanned: JsonObject = dict(sorted(self.scanned.items()))
        return {
            "schema": "clinic-sentinel-scan-v1",
            "sentinels": len(self.sentinels),
            "scanned": scanned,
            "findings": findings,
            "clean": self.clean,
        }


def _forms(sentinel: str) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            form.casefold()
            for form in (sentinel, quote(sentinel), quote_plus(sentinel))
        )
    )


def _label(sentinel: str) -> str:
    if sentinel == SENTINEL_PREFIX:
        return "prefix"
    parts = sentinel.removeprefix(f"{SENTINEL_PREFIX}-").split("-")
    return "-".join(parts[:-1]) if len(parts) > 1 else parts[0]


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _cell(column: str, data_type: str) -> str:
    quoted = '"' + column.replace('"', '""') + '"'
    if data_type == "bytea":
        return f"encode({quoted}, 'escape')"
    return f"{quoted}::text"


class _UrlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.found: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del tag
        self.found.extend(
            value for name, value in attrs if name in URL_ATTRIBUTES and value
        )


def html_urls(body: bytes | str) -> list[str]:
    """Return every link, form target and resource URL in an HTML body."""
    parser = _UrlParser()
    parser.feed(body.decode("utf-8", "replace") if isinstance(body, bytes) else body)
    parser.close()
    return parser.found


def header_urls(headers: Callable[[str], str | None]) -> list[str]:
    """Return redirect-style header values from a header lookup."""
    return [value for name in URL_HEADERS if (value := headers(name))]


@contextmanager
def capture_emitted_logs() -> Iterator[Callable[[], list[str]]]:
    """Mirror every root handler (its filters and formatter) into memory.

    The mirror sees exactly what the deployment would write, so redaction done
    by handler filters is honoured and anything that escapes it is caught.
    Handlers a test runner installs (pytest's capture handlers) are not part
    of the deployment and are not mirrored.
    """
    root = logging.getLogger()
    stream = StringIO()
    mirrors: list[logging.Handler] = []
    for handler in root.handlers:
        if type(handler).__module__.startswith("_pytest"):
            continue
        mirror = logging.StreamHandler(stream)
        mirror.setLevel(handler.level)
        mirror.setFormatter(handler.formatter)
        for item in handler.filters:
            mirror.addFilter(item)
        mirrors.append(mirror)
    if not mirrors:
        _fail("the root logger has no emitting handler to mirror")
    for installed in mirrors:
        root.addHandler(installed)
    try:
        yield lambda: stream.getvalue().splitlines()
    finally:
        for installed in mirrors:
            root.removeHandler(installed)


def run_cli(argv: list[str] | None = None) -> int:
    """Scan artifact trees or a database for the shared sentinel prefix."""
    parser = argparse.ArgumentParser(prog="sentinel_scan")
    commands = parser.add_subparsers(dest="command", required=True)
    artifacts = commands.add_parser("artifacts")
    artifacts.add_argument("--root", action="append", required=True, type=Path)
    artifacts.add_argument("--output", required=True, type=Path)
    database = commands.add_parser("database")
    database.add_argument("--database-url-env", required=True)
    database.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args(argv)
    scan = SentinelScan((SENTINEL_PREFIX,))
    if arguments.command == "artifacts":
        for root in arguments.root:
            scan.files(root)
    else:
        url = os.environ.get(arguments.database_url_env)
        if not url:
            _fail(f"{arguments.database_url_env} is not set")
        import psycopg  # noqa: PLC0415 - only the database mode needs a driver

        with psycopg.connect(url) as connection, connection.cursor() as cursor:
            scan.database(cursor)
    output: Path = arguments.output
    output.write_bytes(canonical_bytes(scan.result()))
    sys.stdout.write(
        f"sentinel scan: {len(scan.findings)} finding(s) over "
        f"{sum(scan.scanned.values())} item(s)\n"
    )
    return 0 if scan.clean else 1


if __name__ == "__main__":
    raise SystemExit(run_cli())

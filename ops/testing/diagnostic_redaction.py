"""One publication boundary and one failure carrier for browser diagnostics."""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Final
from urllib.parse import quote, quote_plus, unquote, urlsplit

from ops.testing.isolation_common import MODE_PRIVATE, canonical_bytes, write_no_replace
from ops.testing.runtime_paths import runtime_directory

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping
    from contextlib import AbstractContextManager
    from pathlib import Path
    from typing import BinaryIO, TextIO

    from ops.testing.isolation_common import JsonObject, JsonValue

REDACTED: Final = "[REDACTED]"
_CREDENTIAL_FIELD: Final = (
    r"[\w-]*(?:password|passwd|secret|token|api[_-]?key|readiness[_-]?key"
    r"|session[_-]?key|otp[_-]?(?:seed|key))[\w-]*"
)
_VALUE: Final = r"""(?:"[^"\r\n]*"|'[^'\r\n]*'|[^\s'"]+)"""
_ASSIGNMENT: Final = re.compile(
    rf"""(?ix)(?P<label>["']?\b(?:{_CREDENTIAL_FIELD})["']?\s*[:=]\s*){_VALUE}"""
)
_OPTION: Final = re.compile(rf"""(?ix)(?P<label>--{_CREDENTIAL_FIELD}\s+){_VALUE}""")
_URL_AUTH: Final = re.compile(r"""(?i)([a-z][a-z0-9+.-]*://)[^\s/'"?#]+@""")
_AUTHORIZATION: Final = re.compile(r"(?i)(\b(?:Bearer|Basic)\s+)[A-Za-z0-9._~+/-]+=*")
_SECRET_ENV: Final = re.compile(
    r"""(?ix)(?:PGPASSWORD|REDISCLI_AUTH|[\w-]*(?:PASSWORD|PASSWD|SECRET(?:_KEY)?
      |TOKEN|API_KEY|SESSION_KEY|READINESS_KEY|OTP_(?:SEED|KEY|SECRET)))"""
)
_ESCAPE: Final = re.compile(r"%[0-9A-F]{2}")
_CARRIER_KEY: Final = "_browser_harness_diagnostics"


class DiagnosticSink:
    """Remember the run's actual secrets and redact at the output boundary."""

    def __init__(self) -> None:
        """Allocate storage scoped to one invocation, not the interpreter."""
        self._values: set[str] = set()
        self._literal: re.Pattern[str] | None = None
        self._replacement = REDACTED

    def remember(self, *values: str) -> None:
        """Register nonempty literal and URL-encoded forms, without length cuts."""
        variants = {
            variant
            for value in values
            if value
            for raw in (
                value,
                quote(value, safe=""),
                quote_plus(value, safe=""),
                json.dumps(value, ensure_ascii=False)[1:-1],
                repr(str.__str__(value))[1:-1],
            )
            for variant in (raw, _ESCAPE.sub(lambda part: part[0].lower(), raw))
        }
        if not variants.issubset(self._values):
            self._values.update(variants)
            self._literal = re.compile(
                "|".join(map(re.escape, sorted(self._values, key=len, reverse=True)))
            )
            self._replacement = next(
                (
                    marker
                    for marker in (REDACTED, "█")
                    if not self._literal.search(marker)
                ),
                "",
            )

    def remember_environment(self, environment: Mapping[str, str]) -> None:
        """Register secret fields and decoded userinfo from actual child inputs."""
        for name, value in environment.items():
            if _SECRET_ENV.fullmatch(name):
                self.remember(value)
            if name.upper().endswith(("_URL", "_DSN")):
                try:
                    password = urlsplit(value).password
                except ValueError:
                    # Invalid URL: leave its shape to the pattern layer.
                    continue
                if password is not None:
                    self.remember(password, unquote(password))

    def text(self, text: str) -> str:
        """Redact known values first, then unknown credential-shaped fields."""
        if self._literal is not None:
            text = self._literal.sub(self._replacement, text)
        mark = self._replacement
        text = _URL_AUTH.sub(rf"\g<1>{mark}@", text)
        text = _AUTHORIZATION.sub(rf"\g<1>{mark}", text)
        text = _ASSIGNMENT.sub(rf"\g<label>{mark}", text)
        return _OPTION.sub(rf"\g<label>{mark}", text)

    def screen(self, text: str, *, error: bool = False) -> None:
        """Emit a redacted diagnostic on the selected CLI stream."""
        self.line(sys.stderr if error else sys.stdout, text)

    def line(self, output: TextIO, text: str) -> None:
        """Emit redacted text to a caller-owned live log."""
        output.write(self.text(text))

    @contextmanager
    def live_log(self, path: Path) -> Iterator[TextIO]:
        """Open an exclusive sink for a readiness observer's redacted lines."""
        with preserving_context(
            path.open("x", encoding="utf-8"), "diagnostic log close failed"
        ) as output:
            yield output

    def _json(self, value: JsonValue) -> JsonValue:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self._json(item) for item in value]
        if isinstance(value, dict):
            return {self.text(key): self._json(item) for key, item in value.items()}
        return value

    def report(
        self,
        path: Path,
        report: JsonObject,
        *,
        echo: bool = False,
    ) -> None:
        """Publish a structured diagnostic once, redacting before serialization."""
        safe = canonical_bytes(self._json(report))
        write_no_replace(path, safe, mode=MODE_PRIVATE)
        if echo:
            sys.stdout.write(safe.decode())

    def artifact_bytes(self, path: Path, content: bytes) -> bytes:
        """Sanitize JSON artifacts while preserving opaque screenshot bytes."""
        if path.suffix != ".json":
            return content
        document: JsonValue = json.loads(content)
        safe = self._json(document)
        return content if safe == document else canonical_bytes(safe)

    def artifact(self, path: Path, content: bytes) -> None:
        """Exclusively publish a suite artifact at the diagnostic boundary."""
        write_no_replace(path, self.artifact_bytes(path, content), mode=MODE_PRIVATE)

    def _xml(self, text: str) -> bytes:
        # Redact the decoded diagnostic fields, not the surrounding XML grammar.
        root = ET.fromstring(text)  # noqa: S314 - private output from our pytest child.
        for element in root.iter():
            if element.text is not None:
                element.text = self.text(element.text)
            if element.tail is not None:
                element.tail = self.text(element.tail)
            for key, value in element.attrib.items():
                element.set(key, self.text(value))
        output = io.BytesIO()
        ET.ElementTree(root).write(output, encoding="utf-8", xml_declaration=True)
        return output.getvalue()

    @contextmanager
    def capture(
        self, path: Path, *, junit: bool = False, server_log: bool = False
    ) -> Iterator[BinaryIO]:
        """Give a subprocess an anonymous output FD; only publish scrubbed bytes."""
        with preserving_context(
            runtime_directory(path.parent.resolve(), purpose="browser-diagnostic"),
            "diagnostic staging cleanup failed",
        ) as staging:
            raw = staging / "output"
            with preserving_context(raw.open("x+b"), "diagnostic close failed") as fd:
                raw.unlink()

                def publish() -> None:
                    fd.seek(0)
                    text = fd.read().decode("utf-8", errors="replace")
                    if junit and not text:
                        return  # No JUnit was emitted; the verdict checks this.
                    safe = self.text(text) if not junit else ""
                    try:
                        write_no_replace(
                            path,
                            self._xml(text) if junit else safe.encode(),
                            mode=MODE_PRIVATE,
                        )
                    except BaseException as error:
                        if server_log:
                            Failures(error).diagnostic(
                                f"server log tail: {safe[-2000:]}"
                            )
                        raise

                with always_cleanup(publish, "diagnostic publication failed"):
                    yield fd


_ACTIVE_SINK: ContextVar[DiagnosticSink | None] = ContextVar(
    "browser_diagnostic_sink", default=None
)


def diagnostics(environment: Mapping[str, str] | None = None) -> DiagnosticSink:
    """Get the current sink, registering inputs before their first use."""
    sink = _ACTIVE_SINK.get()
    if sink is None:
        sink = DiagnosticSink()
        sink.remember_environment(os.environ)
    if environment is not None:
        sink.remember_environment(environment)
    return sink


@contextmanager
def diagnostic_scope() -> Iterator[DiagnosticSink]:
    """Retain secrets across child teardown, receipts and final CLI rendering."""
    sink = diagnostics()
    token = _ACTIVE_SINK.set(sink)
    try:
        yield sink
    finally:
        _ACTIVE_SINK.reset(token)


def redact_diagnostic(text: str, environment: Mapping[str, str] | None = None) -> str:
    """Sanitize text which must be used before publication (e.g., a log tail)."""
    return diagnostics(environment).text(text)


def _exception_dict(error: BaseException) -> dict[str, object]:
    return vars(error)


def _recorded(error: BaseException) -> tuple[str, ...]:
    records = _exception_dict(error).get(_CARRIER_KEY)
    if isinstance(records, tuple):
        return tuple(note for note in records if isinstance(note, str))
    return ()


def error_details(error: BaseException, *, omit: BaseException | None = None) -> str:
    """Render actual failures and diagnostics independently of exception notes."""
    details: list[str] = []
    seen = {id(omit)}
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, subprocess.TimeoutExpired):
            message = "subprocess timed out"
        elif isinstance(current, subprocess.CalledProcessError):
            message = f"subprocess failed (exit {current.returncode})"
        else:
            try:
                message = str(current)
            except Exception as formatting_error:  # noqa: BLE001 - exception rendering boundary.
                kind = type(formatting_error).__name__
                message = f"<exception message unavailable: {kind}>"
        details.append(f"{type(current).__name__}: {message}" if details else message)
        recorded = _recorded(current)
        notes = _exception_dict(current).get("__notes__")
        if isinstance(notes, (list, tuple)) and type(notes) in (list, tuple):
            mirrored = {id(note) for note in recorded}
            details.extend(
                note
                for note in notes
                if isinstance(note, str) and id(note) not in mirrored
            )
        details.extend(recorded)
        current = current.__cause__ or (
            None if current.__suppress_context__ else current.__context__
        )
    return redact_diagnostic("\n".join(details))


class Failures:
    """Carry the first exception and record every subsequent diagnostic on it."""

    def __init__(self, primary: BaseException | None = None) -> None:
        """Start with the body exception, if any."""
        self.primary = primary

    def diagnostic(self, text: str) -> None:
        """Record a diagnostic without depending on a writable __notes__."""
        if self.primary is None:
            msg = "diagnostic requires a primary exception"
            raise RuntimeError(msg)
        text = redact_diagnostic(text)
        state = _exception_dict(self.primary)
        state[_CARRIER_KEY] = (*_recorded(self.primary), text)
        # Preserve PEP 678 display for ordinary exceptions, but do not invoke
        # a possibly malformed notes descriptor or use this list as storage.
        notes = state.setdefault("__notes__", [])
        if type(notes) is list:
            notes.append(text)

    def record(self, step: str, error: BaseException) -> None:
        """Keep the first error object, attaching later failures in order."""
        if self.primary is None:
            self.primary = error
        elif self.primary is not error:
            details = error_details(error, omit=self.primary)
            self.diagnostic(f"{step}: {type(error).__name__}: {details}")

    def raise_if_failed(self) -> None:
        """Raise the original exception after all dependent steps have run."""
        if self.primary is not None:
            raise self.primary


def has_diagnostics(error: BaseException) -> bool:
    """Check the carrier rather than interpreting PEP 678 metadata."""
    return bool(_recorded(error))


@contextmanager
def preserve_cleanup_error(primary: BaseException | None, step: str) -> Iterator[None]:
    """Raise a cleanup failure unless an earlier error must stay primary."""
    try:
        yield
    except BaseException as error:
        if primary is None:
            raise
        Failures(primary).record(step, error)


@contextmanager
def always_cleanup(
    cleanup: Callable[[], object], step: str, primary: BaseException | None = None
) -> Iterator[None]:
    """Run one cleanup on either exit path without replacing the primary."""
    try:
        yield
    except BaseException as error:
        with preserve_cleanup_error(error, step):
            cleanup()
        raise
    else:
        with preserve_cleanup_error(primary, step):
            cleanup()


@contextmanager
def preserving_context[T](
    resource: AbstractContextManager[T], step: str
) -> Iterator[T]:
    """Release a resource without replacing a failure of its body."""
    value = resource.__enter__()
    try:
        yield value
    except BaseException as error:
        with preserve_cleanup_error(error, step):
            resource.__exit__(type(error), error, error.__traceback__)
        raise
    else:
        resource.__exit__(None, None, None)

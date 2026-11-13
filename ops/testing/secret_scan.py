"""Refuse committed credentials (pre-commit hook ``secret-scan``, todo 73).

Scans the files pre-commit passes (or ``--all`` tracked files) for private
keys, cloud/provider tokens, credentialed connection URLs, OTP provisioning
URIs, JWTs and forbidden files (``.env``, Terraform state/variables). A
connection URL counts as credentialed unless its password is a placeholder
(an interpolation such as ``${VAR}``) or a plain lower-case word such as
``pw`` or ``clinic_app_password``; generated secrets carry digits or capitals,
so they always fail. A finding
prints only the path, line number and rule, never the matched text. Synthetic
fixtures that must look like a credential are listed, one ``path<TAB>rule<TAB>
reason`` per line, in ``ops/testing/secret-scan-allowlist.txt``; an allowlist
entry that no longer matches anything fails too, so the list cannot rot::

    python -m ops.testing.secret_scan [--all] [PATH ...]
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, Never
from urllib.parse import unquote

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ops.testing.isolation_common import IsolationError

if TYPE_CHECKING:
    from collections.abc import Iterable

PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]
ALLOWLIST: Final = "ops/testing/secret-scan-allowlist.txt"
GIT: Final = "/usr/bin/git"
MAX_FILE_BYTES: Final = 8 * 1024 * 1024
PLACEHOLDER: Final = re.compile(
    r"\$\{[^}]*\}|\$[A-Z_][A-Z0-9_]*|<[^>@]*>|\{[^}@]*\}|%s"
)
PLAIN_WORD: Final = re.compile(r"[a-z@]+(?:[_-][a-z@]+)*")
MAX_PLAIN_WORD: Final = 32
CONNECTION_URL: Final = re.compile(
    r"\b(?:postgres(?:ql)?|redis|rediss|amqp|amqps|mysql|mongodb)://"
    r"[^\s:/@'\"]+:([^\s@'\"/]+)@"
)
RULES: Final = {
    "private-key": re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----"),
    "aws-access-key": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "github-token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_\w{22,})"),
    "slack-token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    "stripe-live-key": re.compile(r"\b[rs]k_live_[0-9A-Za-z]{16,}"),
    "google-api-key": re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    "otp-provisioning-uri": re.compile(
        r"otpauth://[^\s'\"]*[?&]secret=[A-Z2-7]{16,}", re.IGNORECASE
    ),
    "jwt": re.compile(
        r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
    ),
}
FORBIDDEN_NAMES: Final = re.compile(
    r"(?:^|/)(?:\.env(?:\.[^/]+)?|[^/]+\.tfstate(?:\.backup)?|[^/]+\.tfvars(?:\.json)?)$"
)
ALLOWED_NAMES: Final = frozenset({".env.example"})


@dataclass(frozen=True)
class Finding:
    """One rule hit; the matched text is never retained."""

    path: str
    line: int
    rule: str


def _fail(message: str) -> Never:
    raise IsolationError(message)


def scan_text(path: str, text: str) -> list[Finding]:
    """Return every rule hit in one file's text."""
    findings: list[Finding] = []
    for number, line in enumerate(text.splitlines(), start=1):
        findings.extend(
            Finding(path, number, rule)
            for rule, pattern in RULES.items()
            if pattern.search(line)
        )
        if any(_credential(match[1]) for match in CONNECTION_URL.finditer(line)):
            findings.append(Finding(path, number, "credentialed-url"))
    return findings


def _credential(password: str) -> bool:
    if PLACEHOLDER.fullmatch(password):
        return False
    plain = unquote(password)
    return len(plain) > MAX_PLAIN_WORD or PLAIN_WORD.fullmatch(plain) is None


def scan_paths(root: Path, paths: Iterable[str]) -> list[Finding]:
    """Scan repository-relative paths; binary files are checked by name only."""
    findings: list[Finding] = []
    for relative in sorted(set(paths)):
        name = Path(relative).name
        if FORBIDDEN_NAMES.search(relative) and name not in ALLOWED_NAMES:
            findings.append(Finding(relative, 0, "forbidden-file"))
        path = root / relative
        if path.is_symlink() or not path.is_file():
            continue
        raw = path.read_bytes()
        if len(raw) > MAX_FILE_BYTES or b"\0" in raw[:8192]:
            continue
        findings.extend(scan_text(relative, raw.decode("utf-8", "replace")))
    return findings


def load_allowlist(root: Path) -> dict[tuple[str, str], str]:
    """Return ``(path, rule) -> reason`` from the committed allowlist."""
    entries: dict[tuple[str, str], str] = {}
    path = root / ALLOWLIST
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line or line.startswith("#"):
            continue
        cells = line.split("\t")
        if len(cells) != 3 or not all(cells):  # noqa: PLR2004 - path, rule, reason
            _fail(f"{ALLOWLIST}:{number} must be path<TAB>rule<TAB>reason")
        relative, rule, reason = cells
        if rule not in {*RULES, "credentialed-url", "forbidden-file"}:
            _fail(f"{ALLOWLIST}:{number} names unknown rule {rule!r}")
        if (relative, rule) in entries:
            _fail(f"{ALLOWLIST}:{number} repeats {relative} {rule}")
        entries[(relative, rule)] = reason
    return entries


def tracked_files(root: Path) -> list[str]:
    """Return every tracked path (the ``--all`` population)."""
    listed = subprocess.run(  # noqa: S603 - fixed git argv
        [GIT, "-C", str(root), "ls-files", "-z"],
        check=True,
        capture_output=True,
    ).stdout
    return [item for item in listed.decode("utf-8").split("\0") if item]


def verdict(
    findings: list[Finding],
    allowlist: dict[tuple[str, str], str],
    *,
    scanned: set[str],
    complete: bool,
) -> list[str]:
    """Return failure lines: unallowed hits, and stale entries on full scans."""
    used = {(item.path, item.rule) for item in findings}
    failures = [
        f"{item.path}:{item.line}: {item.rule}"
        for item in findings
        if (item.path, item.rule) not in allowlist
    ]
    failures.extend(
        f"{ALLOWLIST}: stale entry {path} {rule}"
        for path, rule in sorted(allowlist)
        if (path, rule) not in used and (complete or path in scanned)
    )
    return failures


def run_cli(argv: list[str] | None = None, root: Path = PROJECT_ROOT) -> int:
    """Scan the given or all tracked files; return 1 on any failure."""
    parser = argparse.ArgumentParser(prog="secret_scan")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("paths", nargs="*")
    arguments = parser.parse_args(argv)
    paths = tracked_files(root) if arguments.all else list(arguments.paths)
    findings = scan_paths(root, paths)
    failures = verdict(
        findings, load_allowlist(root), scanned=set(paths), complete=arguments.all
    )
    for failure in failures:
        sys.stderr.write(f"secret-scan: {failure}\n")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(run_cli())

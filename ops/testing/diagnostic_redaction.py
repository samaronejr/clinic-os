"""Keep credential-shaped values out of harness diagnostics."""

import re
from typing import Final

REDACTED: Final = "[REDACTED]"
MIN_LITERAL_LENGTH: Final = 4
_CREDENTIAL_FIELD: Final = (
    r"[\w-]*(?:password|passwd|secret|token|api[_-]?key|readiness[_-]?key)[\w-]*"
)
_VALUE: Final = r"""(?:"[^"\r\n]*"|'[^'\r\n]*'|[^\s,;'"\]\}\\]+)"""
_ASSIGNMENT: Final = re.compile(
    rf"""(?ix)(?P<label>["']?\b(?:{_CREDENTIAL_FIELD})["']?\s*[:=]\s*){_VALUE}"""
)
_OPTION: Final = re.compile(rf"""(?ix)(?P<label>--{_CREDENTIAL_FIELD}\s+){_VALUE}""")
_URL_AUTH: Final = re.compile(r"(?i)([a-z][a-z0-9+.-]*://)[^\s/?#]+@")
_AUTHORIZATION: Final = re.compile(r"(?i)(\b(?:Bearer|Basic)\s+)[A-Za-z0-9._~+/-]+=*")
_ENV_VALUE: Final = re.compile(
    rf"(?i)(?:{_CREDENTIAL_FIELD}|.*(?:_DSN|_DATABASE_URL))$"
)


def redact_diagnostic(text: str, environment: dict[str, str] | None = None) -> str:
    """Scrub credential assignments, CLI options and URL userinfo."""
    credentials = {
        value
        for name, value in (environment or {}).items()
        if len(value) >= MIN_LITERAL_LENGTH and _ENV_VALUE.fullmatch(name)
    }
    for value in sorted(credentials, key=len, reverse=True):
        text = text.replace(value, REDACTED)
    text = _URL_AUTH.sub(rf"\g<1>{REDACTED}@", text)
    text = _AUTHORIZATION.sub(rf"\g<1>{REDACTED}", text)
    text = _ASSIGNMENT.sub(rf"\g<label>{REDACTED}", text)
    return _OPTION.sub(rf"\g<label>{REDACTED}", text)

"""Private Gunicorn entrypoint; extend only the synthetic readiness response."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from config.wsgi import application as django_application

from ops.testing.browser_readiness import (
    PROOF_HEADER,
    READINESS_KEY,
    readiness_proof,
    valid_challenge,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from types import TracebackType
    from wsgiref.types import StartResponse, WSGIEnvironment

_key = os.environ.pop(READINESS_KEY)


def application(
    environ: WSGIEnvironment, start_response: StartResponse
) -> Iterable[bytes]:
    """Keep Django readiness semantics and authenticate its successful response."""
    challenge = environ.get("HTTP_X_CLINIC_BROWSER_CHALLENGE")
    if (
        environ.get("PATH_INFO") != "/readyz"
        or not isinstance(challenge, str)
        or not valid_challenge(challenge)
    ):
        return django_application(environ, start_response)

    def respond(
        status: str,
        headers: list[tuple[str, str]],
        exc_info: tuple[type[BaseException], BaseException, TracebackType]
        | tuple[None, None, None]
        | None = None,
    ) -> Callable[[bytes], object]:
        if status.startswith("200 "):
            headers = [*headers, (PROOF_HEADER, readiness_proof(_key, challenge))]
        return start_response(status, headers, exc_info)

    return django_application(environ, respond)

"""Retain exact client exceptions without recording credential material."""

from __future__ import annotations

import json
from contextlib import contextmanager
from functools import partial
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from renewal.browser.engines import watch_page_errors

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from playwright.sync_api import ConsoleMessage, Page, Response

CLIENT_OBSERVER = """
document.addEventListener("clinic:passkey-failure", function (event) {
  console.debug("PASSKEY_FAILURE:" + JSON.stringify(event.detail));
});
if (navigator.credentials) {
  ["create", "get"].forEach(function (method) {
    var original = navigator.credentials[method];
    if (typeof original !== "function") return;
    navigator.credentials[method] = async function () {
      var pending = original.apply(this, arguments);
      console.debug("PASSKEY_PENDING:" + method);
      var credential = await pending;
      if (method === "create") {
        console.debug("PASSKEY_ALGORITHM:" +
          credential.response.getPublicKeyAlgorithm());
      }
      return credential;
    };
  });
}
"""


def _record_console(
    message: ConsoleMessage,
    *,
    console: list[dict[str, str]],
    failures: list[dict[str, object]],
) -> None:
    if message.text.startswith("PASSKEY_FAILURE:"):
        failures.append(json.loads(message.text.removeprefix("PASSKEY_FAILURE:")))
    if message.type == "error" or message.text.startswith("PASSKEY_"):
        console.append(
            {
                "type": message.type,
                "text": message.text,
                "url": message.location.get("url", ""),
            }
        )


@contextmanager
def client_evidence(
    page: Page,
    root: Path,
    *,
    expected_denial: bool,
    expected_abort: bool,
) -> Iterator[None]:
    """Fail with the caught native exception, not just a missing finish timeout."""
    console: list[dict[str, str]] = []
    failures: list[dict[str, object]] = []
    page_errors: list[str] = []
    denied_urls: set[str] = set()
    policies: list[dict[str, str]] = []
    page.add_init_script(CLIENT_OBSERVER)
    watch_page_errors(page, page_errors)

    def record_response(response: Response) -> None:
        if (
            expected_denial
            and response.status == 403
            and urlsplit(response.url).path == "/auth/passkey/assert/finish/"
        ):
            denied_urls.add(response.url)
        if response.request.resource_type == "document":
            policies.append(
                {
                    "path": urlsplit(response.url).path,
                    "csp": response.headers.get("content-security-policy", ""),
                    "permissions_policy": response.headers.get(
                        "permissions-policy", ""
                    ),
                }
            )

    page.on("console", partial(_record_console, console=console, failures=failures))
    page.on("response", record_response)
    try:
        yield
    finally:
        unexpected = list(page_errors)
        for failure in failures:
            if (
                expected_abort
                and failure["name"] == "AbortError"
                and failure["aborted"] is True
            ):
                continue
            if (
                denied_urls
                and failure["name"] == "Error"
                and failure["message"] == "passkey_denied"
            ):
                continue
            unexpected.append(f"{failure['name']}: {failure['message']}")
        for message in console:
            if message["type"] != "error":
                continue
            if (
                message["url"] in denied_urls
                and "the server responded with a status of 403" in message["text"]
            ):
                continue
            unexpected.append(message["text"])
        (root / "client-events.json").write_text(
            json.dumps(
                {
                    "console": console,
                    "page_errors": page_errors,
                    "credential_failures": failures,
                    "response_policies": policies,
                    "unexpected": unexpected,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        assert not unexpected, unexpected

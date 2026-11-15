r"""Locust staff scenarios for a locally running synthetic server.

    PERF_USERNAME=... PERF_PASSWORD=... PERF_TOTP_SECRET_HEX=... \\
    PERF_CLINIC_ID=... uv run locust -f ops/perf/locustfile.py \\
        --headless -u 1 -r 1 -t 60s --host http://localhost:8000

Credentials come from the environment only and must belong to a synthetic
tenant on a throwaway database. The in-process ``ops.perf.run`` harness is the
source of the reported numbers; this file is the HTTP-level companion.
One configured account/authenticator supports one virtual user: accepted TOTP
codes cannot be replayed by concurrent users.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import struct
import time
from http import HTTPStatus
from typing import TYPE_CHECKING, Never, cast
from urllib.parse import urlsplit

from locust import HttpUser, between, task
from locust.exception import StopUser

if TYPE_CHECKING:
    from requests import Response

TOTP_STEP_SECONDS = 30
TOTP_DIGITS = 6
AGENDA_DAY = os.environ.get("PERF_AGENDA_DAY", "2035-06-04")
_DEVICE = re.compile(r'name="otp_device"[^>]*value="([^"]+)"')


def totp(secret_hex: str, now: float) -> str:
    """Return the RFC 6238 SHA-1 code for ``now`` (what an authenticator shows)."""
    counter = struct.pack(">Q", int(now // TOTP_STEP_SECONDS))
    digest = hmac.new(bytes.fromhex(secret_hex), counter, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{value % 10**TOTP_DIGITS:0{TOTP_DIGITS}d}"


class StaffUser(HttpUser):
    """One signed-in staff member browsing the workspace."""

    wait_time = between(1, 3)
    clinic: str

    def _abort(self, message: str) -> Never:
        self.environment.process_exit_code = 1
        logging.getLogger(__name__).error(message)
        if self.environment.runner is not None:
            self.environment.runner.quit()
        raise StopUser(message)

    def _request(
        self,
        method: str,
        path: str,
        expected: HTTPStatus | tuple[HTTPStatus, ...],
        *,
        data: dict[str, str] | None = None,
        referer: str = "",
    ) -> Response:
        accepted = (expected,) if isinstance(expected, HTTPStatus) else expected
        with self.client.request(
            method,
            path,
            data=data,
            headers={"Referer": referer} if referer else {},
            allow_redirects=False,
            catch_response=True,
        ) as response:
            refused = response.status_code not in accepted
            if refused:
                response.failure("performance authentication request refused")
        if refused:
            self._abort("performance authentication request refused")
        return cast("Response", response)

    def _csrf(self) -> str:
        token = self.client.cookies.get("csrftoken")
        if not token:
            self._abort("performance authentication CSRF token missing")
        return str(token)

    def on_start(self) -> None:
        """Sign in with password then TOTP, as the real flow requires."""
        required = (
            "PERF_CLINIC_ID",
            "PERF_USERNAME",
            "PERF_PASSWORD",
        )
        if not all(os.environ.get(name) for name in required):
            self._abort("performance authentication configuration missing")
        self.clinic = os.environ["PERF_CLINIC_ID"]
        page = self._request("GET", "/auth/login/", HTTPStatus.OK)
        self._request(
            "POST",
            "/auth/login/",
            HTTPStatus.FOUND,
            data={
                "username": os.environ["PERF_USERNAME"],
                "password": os.environ["PERF_PASSWORD"],
                "csrfmiddlewaretoken": self._csrf(),
            },
            referer=page.url,
        )
        page = self._request("GET", "/auth/verify/", (HTTPStatus.OK, HTTPStatus.FOUND))
        # The application skips OTP for non-privileged staff. A redirect alone
        # proves nothing: the same protected-route check still has to succeed.
        if page.status_code == HTTPStatus.FOUND:
            self._request("GET", "/auth/protected/", HTTPStatus.OK)
            return
        if not os.environ.get("PERF_TOTP_SECRET_HEX"):
            self._abort("performance authenticator configuration missing")
        device = os.environ.get("PERF_OTP_DEVICE", "")
        if not device:
            found = _DEVICE.search(page.text)
            device = found.group(1) if found else ""
        if not device:
            self._abort("performance authenticator device missing")
        try:
            token = totp(os.environ["PERF_TOTP_SECRET_HEX"], time.time())
        except ValueError:
            self._abort("performance authenticator configuration invalid")
        self._request(
            "POST",
            "/auth/verify/",
            HTTPStatus.FOUND,
            data={
                "otp_device": device,
                "otp_token": token,
                "csrfmiddlewaretoken": self._csrf(),
            },
            referer=page.url,
        )
        self._request("GET", "/auth/protected/", HTTPStatus.OK)

    def _visit(self, path: str, name: str) -> None:
        with self.client.get(path, name=name, catch_response=True) as response:
            refused = response.status_code != HTTPStatus.OK or urlsplit(
                response.url
            ).path.startswith("/auth/")
            if refused:
                response.failure("performance staff session refused")
        if refused:
            self._abort("performance staff session refused")

    @task(3)
    def workspace(self) -> None:
        """Open the workspace entry."""
        self._visit("/workspace/", "workspace-home")

    @task(3)
    def agenda_board(self) -> None:
        """Open the day agenda board."""
        self._visit(
            f"/scheduling/clinics/{self.clinic}/agenda/board/day/{AGENDA_DAY}/",
            name="agenda-board",
        )

    @task(2)
    def patient_list(self) -> None:
        """Open the patient registry."""
        self._visit(f"/intake/clinics/{self.clinic}/patients/", "patient-list")

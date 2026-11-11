r"""Locust staff scenarios for a locally running synthetic server.

    PERF_USERNAME=... PERF_PASSWORD=... PERF_TOTP_SECRET_HEX=... \\
    PERF_CLINIC_ID=... uv run locust -f ops/perf/locustfile.py \\
        --headless -u 10 -r 2 -t 60s --host http://localhost:8000

Credentials come from the environment only and must belong to a synthetic
tenant on a throwaway database. The in-process ``ops.perf.run`` harness is the
source of the reported numbers; this file is the HTTP-level companion.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import struct
import time

from locust import HttpUser, between, task

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
    """One signed-in receptionist browsing the workspace."""

    wait_time = between(1, 3)

    def on_start(self) -> None:
        """Sign in with password then TOTP, as the real flow requires."""
        self.clinic = os.environ["PERF_CLINIC_ID"]
        login = self.client.post(
            "/auth/login/",
            {
                "username": os.environ["PERF_USERNAME"],
                "password": os.environ["PERF_PASSWORD"],
            },
        )
        device = os.environ.get("PERF_OTP_DEVICE", "")
        if not device:
            page = self.client.get("/auth/verify/")
            found = _DEVICE.search(page.text)
            device = found.group(1) if found else ""
        self.client.post(
            "/auth/verify/",
            {
                "otp_device": device,
                "otp_token": totp(os.environ["PERF_TOTP_SECRET_HEX"], time.time()),
            },
        )
        login.close()

    @task(3)
    def workspace(self) -> None:
        """Open the workspace entry."""
        self.client.get("/workspace/", name="workspace-home")

    @task(3)
    def agenda_board(self) -> None:
        """Open the day agenda board."""
        self.client.get(
            f"/scheduling/clinics/{self.clinic}/agenda/board/day/{AGENDA_DAY}/",
            name="agenda-board",
        )

    @task(2)
    def patient_list(self) -> None:
        """Open the patient registry."""
        self.client.get(f"/intake/clinics/{self.clinic}/patients/", name="patient-list")

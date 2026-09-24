"""Positive and negative probes for the production HTTPS acceptance stack."""

from __future__ import annotations

import itertools
import json
import sys
from typing import TYPE_CHECKING, Never
from urllib.parse import urlsplit

from ops.testing.https_role_bootstrap import configure_roles
from ops.testing.https_runtime_probes import inspect_https_runtime
from ops.testing.isolation_common import IsolationError, JsonValue
from ops.testing.isolation_docker_metadata import run_docker_command
from ops.testing.process_helpers import ProcessResult, run_process
from ops.testing.tls_contract import WEB_HOST

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from ops.testing.https_stack_specs import HttpsStackPlan


def run_https_probes(
    repository: Path,
    plan: HttpsStackPlan,
    containers: dict[str, str],
    ca_path: Path,
    browser_probe: Callable[[Path], None] | None = None,
) -> None:
    """Run role setup, release, TLS/HBA assertions, and browser fixture checks."""
    inspect_https_runtime(plan, containers)
    configure_roles(containers["database"], plan)
    run_docker_command(("exec", containers["release"], "/app/ops/container/release.sh"))
    _probe_database_tls(containers["release"], plan)
    _probe_https(plan, ca_path)
    if browser_probe is None:
        _probe_browser_fixture(repository)
    else:
        browser_probe(repository)


def _probe_database_tls(container_id: str, plan: HttpsStackPlan) -> None:
    environment = (
        "--env",
        f"QA_DATABASE={plan.database_name}",
        "--env",
        f"QA_OWNER_PASSWORD={plan.credentials.owner}",
        "--env",
        f"QA_APP_PASSWORD={plan.credentials.app}",
    )
    positive = """
import os
import psycopg

for role, password_key in (
    ("clinic_app", "QA_APP_PASSWORD"),
    ("clinic_owner", "QA_OWNER_PASSWORD"),
    ("postgres", "QA_POSTGRES_PASSWORD"),
):
    connection = psycopg.connect(
        host=os.environ["QA_DATABASE_HOST"],
        dbname=os.environ["QA_DATABASE"],
        user=role,
        password=os.environ[password_key],
        sslmode="verify-full",
        sslrootcert="/run/clinic-test-db-trust/db-ca.pem",
        connect_timeout=3,
    )
    row = connection.execute(
        "SELECT current_setting('ssl'), current_setting('password_encryption'), "
        "(SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid())"
    ).fetchone()
    assert row == ("on", "scram-sha-256", True)
    if role == "postgres":
        hba = connection.execute(
            "SELECT current_setting('hba_file'), count(*), "
            "count(*) FILTER (WHERE error IS NOT NULL) FROM pg_hba_file_rules"
        ).fetchone()
        assert hba == ("/run/clinic-test-db-tls/tls/pg_hba.conf", 6, 0)
    connection.close()
""".strip()
    run_docker_command(
        (
            "exec",
            "--env",
            f"QA_DATABASE_HOST={plan.database_host}",
            "--env",
            f"QA_POSTGRES_PASSWORD={plan.credentials.postgres}",
            *environment,
            container_id,
            "python",
            "-c",
            positive,
        )
    )
    negative = """
import os
import sys
import psycopg

try:
    psycopg.connect(
        host=os.environ["QA_DATABASE_HOST"],
        dbname=os.environ["QA_DATABASE"],
        user="clinic_app",
        password=os.environ["QA_APP_PASSWORD"],
        sslmode="disable",
        connect_timeout=2,
    )
except psycopg.OperationalError:
    sys.exit(0)
sys.exit(1)
""".strip()
    run_docker_command(
        (
            "exec",
            "--env",
            f"QA_DATABASE_HOST={plan.database_host}",
            *environment,
            container_id,
            "python",
            "-c",
            negative,
        )
    )


def _probe_https(plan: HttpsStackPlan, ca_path: Path) -> None:
    trusted = _curl(
        "--fail",
        "--cacert",
        str(ca_path),
        f"https://{WEB_HOST}:{plan.https_port}/readyz",
    )
    if trusted.returncode != 0 or json.loads(trusted.stdout) != {"status": "ok"}:
        _fail("trusted HTTPS curl failed")
    secure_headers = _curl(
        "--cacert",
        str(ca_path),
        "--dump-header",
        "-",
        "--output",
        "/dev/null",
        f"https://{WEB_HOST}:{plan.https_port}/auth/login/",
    )
    lowered_headers = secure_headers.stdout.lower()
    if (
        secure_headers.returncode != 0
        or "strict-transport-security:" not in lowered_headers
        or "set-cookie: csrftoken=" not in lowered_headers
        or "; secure" not in lowered_headers
    ):
        _fail("HTTPS security headers or cookie contract failed")
    plaintext = _curl(f"http://{WEB_HOST}:{plan.https_port}/healthz")
    wrong_ca = _curl(
        "--cacert",
        "/etc/ssl/certs/ca-certificates.crt",
        f"https://{WEB_HOST}:{plan.https_port}/healthz",
    )
    wrong_host = _curl(
        "--cacert",
        str(ca_path),
        f"https://wrong.qa.clinic-os.dev:{plan.https_port}/healthz",
        resolve_host="wrong.qa.clinic-os.dev",
    )
    if any(item.returncode == 0 for item in (plaintext, wrong_ca, wrong_host)):
        _fail("HTTPS negative curl unexpectedly succeeded")
    header_names = (
        "X-Forwarded-Proto:https",
        "X-Forwarded-Protocol:ssl",
        "X-Forwarded-Ssl:on",
    )
    for count in range(1, len(header_names) + 1):
        for headers in itertools.combinations(header_names, count):
            arguments = ["--head"]
            for header in headers:
                arguments.extend(("--header", header))
            redirect = _curl(
                *arguments,
                f"http://{WEB_HOST}:{plan.cleartext_port}/healthz",
            )
            expected = f"location: https://{WEB_HOST}:8443/healthz"
            if redirect.returncode != 0 or expected not in redirect.stdout.lower():
                _fail("cleartext proxy-spoof redirect contract failed")
    sys.stdout.write(
        "curl-negative-returncodes="
        f"{plaintext.returncode},{wrong_ca.returncode},{wrong_host.returncode}\n"
    )


def _curl(
    *arguments: str,
    resolve_host: str = WEB_HOST,
) -> ProcessResult:
    port = urlsplit(arguments[-1]).port
    if port is None:
        _fail("curl URL lacks a port")
    return run_process(
        (
            "/usr/bin/curl",
            "--silent",
            "--show-error",
            "--max-time",
            "5",
            "--noproxy",
            "*",
            "--resolve",
            f"{resolve_host}:{port}:127.0.0.1",
            *arguments,
        ),
        timeout_seconds=7,
    )


def _probe_browser_fixture(repository: Path) -> None:
    result = run_process(
        (
            str(repository / "ops/testing/browser_runner.sh"),
            "session",
            "--profile",
            "container-https",
            "--origin",
            f"https://{WEB_HOST}:8443",
            "--ca",
            "/run/clinic-test-db-trust/db-ca.pem",
        )
    )
    if result.returncode != 0:
        _fail("container HTTPS browser fixture failed")
    value: JsonValue = json.loads(result.stdout)
    if not isinstance(value, dict) or value.get("profile") != "container-https":
        _fail("container HTTPS browser fixture drifted")


def _fail(message: str) -> Never:
    raise IsolationError(message)

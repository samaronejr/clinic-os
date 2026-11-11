"""PHI sentinels injected in real requests never reach a technical sink (todo 73).

The adversarial test drives intake, patient search and the UI API through the
full middleware stack with synthetic sentinels in their request bodies, then
scans what each sink holds: the configured log handlers' output, every URL the
responses expose, a realtime SSE frame for a sentinel-bearing topic, and every
text, JSON and bytea column of every table (audit and outbox rows included) as
the superuser, so row-level security hides nothing. The positive controls
prove each sink check can fail.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING
from uuid import uuid4

import psycopg
import pytest
from apps.core.telemetry import LOG_MESSAGE_ALLOWLIST
from apps.realtime.transport import event_bytes
from ops.testing.isolation_common import IsolationError
from ops.testing.sentinel_scan import (
    SENTINEL_PREFIX,
    SentinelScan,
    capture_emitted_logs,
    header_urls,
    html_urls,
    new_sentinel,
    run_cli,
)

from patient_http_support import (
    patient_create_url,
    patient_list_url,
    receptionist_client,
)
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from django.test.client import _MonkeyPatchedWSGIResponse

    from rbac_fixtures import RbacGraph

SEARCH = "/api/ui/v1/command/search/"
OK = 200
SEE_OTHER = 303


def _urls(response: _MonkeyPatchedWSGIResponse) -> list[str]:
    return [
        response.wsgi_request.get_full_path(),
        *header_urls(response.headers.get),
        *html_urls(response.content),
    ]


@pytest.mark.django_db(transaction=True)
def test_request_sentinels_never_reach_any_sink(
    rbac_graph: RbacGraph, superuser_database_url: str
) -> None:
    # Given synthetic sentinels for a patient name, a search term and a query.
    name = new_sentinel("PATIENT-NAME")
    term = new_sentinel("SEARCH-TERM")
    query = new_sentinel("PALETTE-QUERY")
    client, _receptionist = receptionist_client(rbac_graph)
    clinic = rbac_graph.clinic_a
    # When reception registers, searches and queries through real HTTP.
    with capture_emitted_logs() as emitted, runtime_role():
        responses = {
            "create": client.post(
                patient_create_url(clinic),
                {
                    "full_name": f"{name} Synthetic Testpatient",
                    "birth_date": "1990-01-01",
                    "idempotency_key": str(uuid4()),
                },
            ),
            "invalid-create": client.post(
                patient_create_url(clinic),
                {
                    "full_name": f"{name} Synthetic Testpatient",
                    "birth_date": "3999-01-01",
                    "idempotency_key": str(uuid4()),
                },
                headers={"hx-request": "true"},
            ),
            "search": client.post(
                patient_list_url(clinic),
                {"q": term, "birth_date": "1990-01-01", "page": "1"},
            ),
            "palette": client.post(
                SEARCH,
                data=json.dumps({"clinic_id": str(clinic), "q": query}),
                content_type="application/json",
            ),
        }
        lines = emitted()
    # Then every request ran, the deployment logged each one, and the
    # registration really committed the sentinel-named patient ...
    assert {label: item.status_code for label, item in responses.items()} == {
        "create": SEE_OTHER,
        "invalid-create": OK,
        "search": OK,
        "palette": OK,
    }
    assert len(lines) == len(responses)
    scan = SentinelScan((name, term, query))
    for label, response in responses.items():
        scan.urls(label, _urls(response))
    scan.logs(lines)
    scan.text("sse", "agenda", event_bytes(f"clinic:{clinic}:agenda", "agenda", 1))
    with (
        psycopg.connect(superuser_database_url) as database,
        database.cursor() as cursor,
    ):
        cursor.execute("SELECT count(*) FROM clinic_app.intake_patient")
        assert cursor.fetchone() == (1,)
        scan.database(cursor)
    # ... and no URL, emitted log, SSE frame, audit, outbox or other row holds it.
    result = scan.result()
    assert result["findings"] == []
    assert result["clean"] is True
    assert {"urls", "logs", "sse", "audit", "outbox", "database"} <= set(scan.scanned)


@pytest.mark.django_db(transaction=True)
def test_every_sink_check_detects_a_planted_sentinel(
    rbac_graph: RbacGraph, superuser_database_url: str
) -> None:
    # Given one sentinel planted in a plaintext column and in each other sink.
    planted = new_sentinel("PLANTED")
    with (
        psycopg.connect(superuser_database_url) as database,
        database.cursor() as cursor,
    ):
        cursor.execute(
            "UPDATE clinic_app.identity_organization SET name = %s WHERE id = %s",
            [planted, rbac_graph.organization_a],
        )
        database.commit()
        scan = SentinelScan((planted,))
        # When every sink is scanned.
        scan.database(cursor)
    scan.urls("redirect", [f"/next/?q={planted.lower()}"])
    scan.logs(["safe line", json.dumps({"message": planted})])
    scan.text("sse", "frame", planted.encode())
    scan.text("push", "payload", f"%7B{planted}%7D")
    # Then each sink reports it without echoing the planted value.
    sinks = {(item.sink, item.location) for item in scan.findings}
    assert ("database", "clinic_app.identity_organization.name") in sinks
    assert {"urls", "logs", "sse", "push"} <= {sink for sink, _location in sinks}
    assert planted not in json.dumps(scan.result())
    assert scan.result()["clean"] is False


@pytest.mark.parametrize(
    ("value", "matches"),
    [
        ("{sentinel}", 1),
        ("prefix {sentinel} suffix", 1),
        ("{lower}", 1),
        # The spaced needle is found only in its encoded form.
        ("/path?q={quoted}", 2),
        ("name={plus}", 2),
    ],
)
def test_text_matches_raw_case_folded_and_percent_encoded_forms(
    value: str, matches: int
) -> None:
    sentinel = new_sentinel("FORM")
    spaced = f"{sentinel} x"
    scan = SentinelScan((sentinel, spaced))
    text = value.format(
        sentinel=sentinel,
        lower=sentinel.lower(),
        quoted=spaced.replace(" ", "%20"),
        plus=spaced.replace(" ", "+"),
    )
    scan.text("urls", "value", text)
    assert len(scan.findings) == matches
    assert {item.sentinel for item in scan.findings} == {"FORM"}


def test_unrelated_text_is_clean_and_bad_needles_are_refused() -> None:
    scan = SentinelScan((new_sentinel("CLEAN"),))
    scan.text("logs", "line", "SINTETICO-SENTINELA-OTHER-ABCDEF")
    assert scan.clean
    with pytest.raises(IsolationError):
        SentinelScan(())
    with pytest.raises(IsolationError):
        SentinelScan(("Maria da Silva",))
    with pytest.raises(IsolationError):
        new_sentinel("lower case")
    with pytest.raises(IsolationError):
        scan.text("mail", "subject", "x")


def test_html_and_header_urls_cover_links_forms_and_redirects() -> None:
    body = (
        b'<a href="/a/">x</a><form action="/f/" method="post">'
        b'<button formaction="/b/"></button></form><img src="/i.png">'
        b'<div hx-get="/h/"></div><p data-x="/not-a-url/"></p>'
    )
    assert html_urls(body) == ["/a/", "/f/", "/b/", "/i.png", "/h/"]
    headers = {"Location": "/l/", "HX-Redirect": "/r/"}
    assert header_urls(headers.get) == ["/l/", "/r/"]


def test_emitted_log_capture_honours_the_configured_handler_pipeline() -> None:
    # Given a registered template and an unregistered message carrying a sentinel.
    sentinel = new_sentinel("LOG")
    template = sorted(LOG_MESSAGE_ALLOWLIST)[0]
    logger = logging.getLogger("apps.core")
    # When both are logged while the deployment handlers are mirrored.
    with capture_emitted_logs() as emitted:
        logger.warning(template)
        logger.warning("patient %s", sentinel)
        lines = emitted()
    # Then both lines are emitted, redacted exactly as the real handler writes.
    assert len(lines) == 2
    scan = SentinelScan((sentinel,))
    scan.logs(lines)
    assert scan.clean
    assert template in lines[0]


def test_artifact_files_are_scanned_and_binaries_only_counted(tmp_path: Path) -> None:
    # The autouse secret backend owns tmp_path itself, so the run is a subtree.
    sentinel = new_sentinel("ARTIFACT")
    run = tmp_path / "run"
    (run / "browser").mkdir(parents=True)
    (run / "report.json").write_text('{"ok": true}', encoding="utf-8")
    (run / "access.log").write_text(f"GET /x?{sentinel}\n", encoding="utf-8")
    (run / "browser" / "page.png").write_bytes(sentinel.encode())
    scan = SentinelScan((sentinel,))
    scan.files(run)
    assert [(item.sink, item.location) for item in scan.findings] == [
        ("files", "access.log")
    ]
    assert scan.scanned == {"files": 2, "binary": 1}


def test_artifact_cli_exit_follows_the_prefix_scan(tmp_path: Path) -> None:
    clean = tmp_path / "clean"
    dirty = tmp_path / "dirty"
    for root in (clean, dirty):
        root.mkdir()
        (root / "report.json").write_text("{}", encoding="utf-8")
    (dirty / "access.log").write_text(f"{SENTINEL_PREFIX}-X-1\n", encoding="utf-8")
    output = tmp_path / "scan.json"
    assert run_cli(["artifacts", "--root", str(clean), "--output", str(output)]) == 0
    assert json.loads(output.read_text(encoding="utf-8"))["clean"] is True
    arguments: Sequence[str] = ("artifacts", "--root", str(clean), "--root", str(dirty))
    assert run_cli([*arguments, "--output", str(output)]) == 1
    assert json.loads(output.read_text(encoding="utf-8"))["findings"] == [
        {"location": "access.log", "sentinel": "prefix", "sink": "files"}
    ]


class _Cursor:
    def __init__(self, catalog: list[tuple[object, ...]], flags: list[bool]) -> None:
        self.catalog = catalog
        self.flags = flags
        self.statements: list[str] = []

    def execute(self, query: str, params: Sequence[object] = (), /) -> object:
        del params
        self.statements.append(query)
        return None

    def fetchall(self) -> list[tuple[object, ...]]:
        if len(self.statements) == 1:
            return self.catalog
        return [(self.flags.pop(0),)]


def test_database_rows_are_classified_as_audit_outbox_or_database() -> None:
    cursor = _Cursor(
        [
            ("clinic_app", "audit_event", "payload", "jsonb"),
            ("clinic_app", "comms_integrationoperation", "last_error", "text"),
            ("clinic_app", "intake_patient", "full_name", "bytea"),
        ],
        [True, True, False],
    )
    scan = SentinelScan((new_sentinel("DB"),))
    scan.database(cursor)
    assert [(item.sink, item.location) for item in scan.findings] == [
        ("audit", "clinic_app.audit_event.payload"),
        ("outbox", "clinic_app.comms_integrationoperation.last_error"),
    ]
    assert "encode(\"full_name\", 'escape')" in cursor.statements[3]

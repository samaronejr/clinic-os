"""SM-I1 without a browser: no clinical content reaches browser storage or the SW cache.

The shipped bytes are the boundary: every first-party script the pages load,
the scribe capture buffer and the service worker the server actually serves.
The browser suites (scribe-privacy, scribe-capture) inspect live storage after
capture; these checks fail first whenever a script gains a storage write.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from patient_http_support import receptionist_client
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

ROOT = Path(__file__).resolve().parents[2]
WORKER = ROOT / "templates/clinic-os-sw.js"
CAPTURE = ROOT / "static/js/scribe-capture.js"
STORAGE_API = re.compile(
    r"\b(?:localStorage|sessionStorage|indexedDB|openDatabase|caches\s*\.|"
    r"CacheStorage|navigator\s*\.\s*storage)\b|document\s*\.\s*cookie\s*=(?!=)"
)
OK = 200


def _first_party_scripts() -> list[Path]:
    scripts = [
        path
        for path in sorted((ROOT / "static/js").rglob("*.js"))
        if "vendor" not in path.relative_to(ROOT).parts
    ]
    scripts.extend(sorted((ROOT / "templates").rglob("*.js")))
    return [path for path in scripts if path != WORKER]


def test_first_party_scripts_never_write_browser_storage() -> None:
    scripts = _first_party_scripts()
    assert CAPTURE in scripts
    offending = {
        str(path.relative_to(ROOT)): match.group(0)
        for path in scripts
        for match in [STORAGE_API.search(path.read_text(encoding="utf-8"))]
        if match is not None
    }
    assert offending == {}


def test_scribe_capture_buffer_is_memory_only_and_capped_from_capture_time() -> None:
    # A source-level tripwire, not behavior: the running cap is proved by the
    # scribe-capture browser suite, so SM-I1 records that clause **Unproved:**.
    source = CAPTURE.read_text(encoding="utf-8")
    # The cap is sixty seconds, enforced on the in-memory pending list ...
    assert re.search(r"\bvar BUFFER_MS = 60000;", source)
    assert "total + duration > BUFFER_MS" in source
    # ... and each slice expires from its capture instant, so a reconnect
    # cannot extend it.
    assert "expires: capturedAt + BUFFER_MS" in source
    assert STORAGE_API.search(source) is None


@pytest.mark.django_db(transaction=True)
def test_service_worker_stores_only_versioned_static_assets(
    rbac_graph: RbacGraph,
) -> None:
    client, _receptionist = receptionist_client(rbac_graph)
    with runtime_role():
        worker = client.get("/sw.js")
        page = client.get(f"/scheduling/clinics/{rbac_graph.clinic_a}/agenda/")
    assert worker.status_code == page.status_code == OK
    source = worker.content.decode()
    precache = re.search(r"const PRECACHE = (\[.*?\]);", source)
    assert precache is not None
    urls = json.loads(precache.group(1))
    assert urls
    assert all(url.startswith("/static/") and not url.endswith(".html") for url in urls)
    # The only runtime write is guarded by the static-request and storable checks,
    # and the fetch handler never intercepts anything else.
    assert source.count("cache.put(") == 1
    assert re.search(
        r"if \(isStorableResponse\(response\)\) \{\s*await cache\.put\(", source
    )
    assert re.search(
        r'addEventListener\("fetch", \(event\) => \{\s*'
        r"if \(isCacheableStaticRequest\(event\.request\)\) \{",
        source,
    )
    assert "!url.pathname.startsWith(STATIC_PREFIX)" in source
    assert 'request.mode === "navigate"' in source
    assert 'contentType.startsWith("text/html")' in source
    # A clinical page itself is never storable: it is HTML and no-store.
    assert page["Content-Type"].startswith("text/html")
    assert "no-store" in page["Cache-Control"]

"""Observe actual browser persistence and technical sinks after real capture."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING
from uuid import uuid4

from playwright.sync_api import expect

from renewal.browser.test_scribe_capture import (
    capture_page,
    fake_microphone,
    fixture_command,
    open_capture,
    supported,
)

if TYPE_CHECKING:
    from pathlib import Path

    from playwright.sync_api import Page, Request

__all__ = ("capture_page",)


def test_capture_and_manual_note_stay_out_of_browser_and_technical_sinks(
    capture_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
) -> None:
    # Given: observers registered before capture or the synthetic note is sent.
    page = capture_page
    fake_microphone(page)
    urls: list[str] = []
    console: list[str] = []
    audio_prefixes: list[str] = []

    def request_sent(request: Request) -> None:
        urls.append(request.url)
        if request.url.endswith("/scribe/chunk/") and request.method == "POST":
            payload = request.post_data_json
            assert isinstance(payload, dict)
            audio = payload["bytes"]
            assert isinstance(audio, str)
            audio_prefixes.append(audio[:128])

    page.on("request", request_sent)
    page.on("console", lambda message: console.append(message.text))
    open_capture(page, renewal_base_url, availability_staff, "2036-05-01")
    token = page.locator('[data-capture-form] [name="encounter_token"]').input_value()
    sentinel = "SINTETICO-40-SINK-" + uuid4().hex

    # When: the native encoder uploads and a manual clinical save is acknowledged.
    page.locator('[data-capture-action="start"]').click()
    native = supported(page)
    if native:
        expect(page.locator("[data-scribe-capture]")).to_have_attribute(
            "data-received", re.compile(r"^[1-9][0-9]*$"), timeout=15000
        )
        page.locator('[data-capture-action="stop"]').click()
        expect(page.locator("[data-scribe-capture]")).to_have_attribute(
            "data-phase", "done"
        )
        assert audio_prefixes
    with page.expect_response(
        lambda response: (
            "autosave" in response.url and response.request.method == "POST"
        )
    ) as saved:
        page.locator("#id_objective").fill(sentinel)
    assert saved.value.status == 200
    terms = [sentinel, token, *audio_prefixes]
    browser = page.evaluate("""async () => {
      const binary = value => value instanceof Blob || value instanceof ArrayBuffer ||
        ArrayBuffer.isView(value) || (value && typeof value === 'object' &&
          Object.values(value).some(binary));
      const persisted = [
        ...Object.values(localStorage), ...Object.values(sessionStorage)
      ];
      let binaryStored = false;
      let databaseCount = 0;
      for (const entry of await indexedDB.databases()) {
        if (!entry.name) continue;
        const db = await new Promise((resolve,reject) => {
          const request = indexedDB.open(entry.name);
          request.onsuccess = () => resolve(request.result);
          request.onerror = () => reject(request.error);
        });
        try {
          const names = [...db.objectStoreNames];
          if (!names.length) continue;
          const transaction = db.transaction(names, 'readonly');
          const records = await Promise.all(names.map(name => new Promise(
            (resolve,reject) => {
              const request = transaction.objectStore(name).getAll();
              request.onsuccess = () => resolve(request.result);
              request.onerror = () => reject(request.error);
            })));
          binaryStored ||= records.some(binary);
          persisted.push(JSON.stringify(records));
          databaseCount += 1;
        } finally { db.close(); }
      }
      let cacheEntries = 0;
      for (const name of await caches.keys()) {
        const cache = await caches.open(name);
        for (const request of await cache.keys()) {
          const response = await cache.match(request);
          if (!response) continue;
          binaryStored ||= /^audio\\//.test(response.headers.get('Content-Type') || '');
          persisted.push(request.url, await response.text());
          cacheEntries += 1;
        }
      }
      return {persisted, binaryStored, databaseCount, cacheEntries};
    }""")
    sinks = json.loads(
        fixture_command(
            availability_staff,
            {
                "SCRIBE_FIXTURE_ACTION": "sinks",
                "SCRIBE_FIXTURE_SENSITIVE": json.dumps(terms),
            },
        )
    )
    logs = [
        *renewal_artifact_root.glob("*.log"),
        *renewal_artifact_root.parent.glob("*.log"),
    ]
    assert any(path.name == "server.log" for path in logs)
    observed = [
        json.dumps(browser["persisted"]),
        *urls,
        *console,
        *(path.read_text(errors="replace") for path in logs),
    ]

    # Then: no clinical sentinel, token, or encoded audio reaches these sinks.
    assert browser["binaryStored"] is False
    assert all(term not in value for term in terms for value in observed)
    assert sinks["clean"] == {"audit": True, "outbox": True}
    assert sinks["rows"]["audit"] > 0
    if native:
        assert sinks["rows"]["outbox"] > 0
    (renewal_artifact_root / "privacy-sinks.json").write_text(
        json.dumps(
            {
                "level": "L1",
                "native_capture": native,
                "observed_chunk_posts": len(audio_prefixes),
                "indexed_databases_read": browser["databaseCount"],
                "cache_entries_read": browser["cacheEntries"],
                "binary_browser_storage": False,
                "storage_url_console_log_sentinels_absent": True,
                "audit_outbox": sinks,
                "log_files_scanned": sorted({path.name for path in logs}),
                "sensitive_values_published": False,
            },
            sort_keys=True,
        )
        + "\n"
    )

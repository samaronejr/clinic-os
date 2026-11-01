"""Observe original-context storage without native-page script callbacks."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from playwright.sync_api import expect

from renewal.browser._page_wait import _answer

if TYPE_CHECKING:
    from playwright.sync_api import Browser, Page

ENGINE = "evaluationstorage"
ATTRIBUTE = "data-evaluation-storage-observation"
SCRIPT = """(() => {
  const started = new WeakSet();
  return {
  query(root) {
    const element = root.documentElement;
    const attribute = 'data-evaluation-storage-observation';
    if (!started.has(element)) {
      started.add(element);
      element.setAttribute(attribute, 'pending');
      Promise.all([
        indexedDB.databases(),
        caches.keys().then(keys => Promise.all(keys.map(async key =>
          (await (await caches.open(key)).keys()).every(request =>
            new URL(request.url).pathname.startsWith('/static/')))))
      ]).then(([databases, caches]) => {
        element.setAttribute(attribute, JSON.stringify({
          indexedDB: databases.length, staticCacheOnly: caches.every(Boolean)
        }));
      }).catch(() => element.setAttribute(attribute, 'failed'));
    }
    return element;
  },
  queryAll(root) { return [this.query(root)]; }
  };
})()"""


def register_storage_observer(page: Page) -> Browser:
    """Reuse only the exact isolated-world engine across shared fixtures."""
    browser = page.context.browser
    assert browser is not None
    browser_type = browser._impl_obj._browser_type
    assert browser_type is not None
    selectors = browser_type._playwright.selectors
    for registered in selectors._selector_engines:
        if registered["name"] == ENGINE:
            assert registered == {
                "name": ENGINE,
                "source": SCRIPT,
                "contentScript": True,
            }
            return browser
    _answer(
        page,
        selectors.register(ENGINE, script=SCRIPT, contentScript=True),
        None,
    )
    return browser


def observe_storage(page: Page) -> dict[str, object]:
    """Wait on the original page's actual storage observation, never a timer."""
    element = page.locator(f"{ENGINE}=observe")
    expect(element).not_to_have_attribute(
        ATTRIBUTE, "pending", timeout=page._impl_obj._timeout_settings.timeout()
    )
    encoded = element.get_attribute(ATTRIBUTE)
    assert encoded is not None
    observed = json.loads(encoded)
    assert observed["indexedDB"] == 0
    assert observed["staticCacheOnly"] is True
    return {"indexedDB": observed["indexedDB"], "staticCacheOnly": True}

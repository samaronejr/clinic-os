"""Offline event-protocol checks for evaluation response and driver deadlines."""

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest
from ops.testing.renewal_runner import _run_bounded
from playwright._impl._driver import compute_driver_executable

from renewal.browser import a11y_support
from renewal.browser import evaluation_journeys as journey
from renewal.browser import evaluation_storage as storage
from renewal.browser._page_wait import EvaluateTimeoutError

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Iterator
    from pathlib import Path

    from playwright.sync_api import Locator, Page, Response


class ResponseSignal:
    def __init__(self, page: PageStub) -> None:
        self.page = page

    @property
    def value(self) -> Response:
        assert self.page.submitted
        self.page.events.append("response_read")
        return cast("Response", self.page.response)


class PageStub:
    url = "https://synthetic.invalid/evaluation/"

    def __init__(self, *, enhanced: bool = False, status: int = 200) -> None:
        self.events: list[str] = []
        self.timeouts: list[float] = []
        self.submitted = False
        self.press_pending = False
        self.response = SimpleNamespace(
            status=status,
            url=self.url,
            request=SimpleNamespace(
                method="POST", headers={"hx-request": "true"} if enhanced else {}
            ),
            headers={
                "cache-control": "private, no-store, no-cache, must-revalidate",
                "content-security-policy": "script-src 'self'",
            },
        )
        self.keyboard = SimpleNamespace(_impl_obj=SimpleNamespace(press=self.press))
        self.context = SimpleNamespace()
        self._impl_obj = SimpleNamespace(
            _timeout_settings=SimpleNamespace(timeout=self.timeout),
            add_script_tag=self.add_script_tag,
        )

    def timeout(self, milliseconds: float | None = None) -> float:
        return 10 if milliseconds is None else milliseconds

    @contextmanager
    def expect_response(
        self,
        predicate: str | Callable[[Response], bool],
        *,
        timeout: float | None = None,
    ) -> Iterator[ResponseSignal]:
        if callable(predicate):
            assert predicate(cast("Response", self.response))
        self.events.append("response_subscribed")
        self.timeouts.append(self.timeout(timeout))
        yield ResponseSignal(self)

    def focus(self, *, timeout: float) -> None:
        self.timeouts.append(timeout)

    def click(self, *, timeout: float) -> None:
        self.timeouts.append(timeout)
        self.events.append("click")
        self.submitted = True

    async def press(self, key: str) -> None:
        assert key == "Enter"
        self.events.append("enter")
        self.submitted = True
        if self.press_pending:
            await asyncio.Event().wait()

    async def add_script_tag(self, *, url: str) -> None:
        assert url.endswith(a11y_support.AXE_URL)
        await asyncio.Event().wait()

    def _sync(self, work: Coroutine[object, object, object]) -> object:
        async def bounded() -> object:
            return await asyncio.wait_for(work, timeout=1)

        return asyncio.run(bounded())


@contextmanager
def navigation(page: Page, *, wait_until: str, timeout: float) -> Iterator[None]:
    stub = cast("PageStub", page)
    assert wait_until == "domcontentloaded"
    stub.timeouts.append(timeout)
    stub.events.append("navigation_subscribed")
    cancelled = True
    try:
        yield
        cancelled = False
    finally:
        stub.events.append("navigation_cancelled" if cancelled else "navigation_wait")


def evaluated(page: Page, _expression: str, *, timeout: float) -> None:
    cast("PageStub", page).timeouts.append(timeout)


def settled(page: Page, _expression: str, *, timeout: float) -> None:
    stub = cast("PageStub", page)
    stub.timeouts.append(timeout)
    stub.events.append("hx_settled")


@pytest.fixture
def bounded_journey(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(journey, "SUBMIT_TIMEOUT_MS", 10)
    monkeypatch.setattr(journey, "expect_document", navigation)
    monkeypatch.setattr(journey, "evaluate_js", evaluated)
    monkeypatch.setattr(journey, "wait_for_js", settled)


@pytest.mark.parametrize("enhanced", [False, True])
def test_unexpected_status_fails_before_navigation_or_hx_wait(
    bounded_journey: None, enhanced: bool
) -> None:
    stub = PageStub(enhanced=enhanced, status=403)
    with pytest.raises(AssertionError) as error:
        journey.submit(cast("Page", stub), cast("Locator", stub), enhanced=enhanced)
    assert error.value.args == ({"expected_status": 200, "observed_status": 403},)
    assert "navigation_wait" not in stub.events
    assert "hx_settled" not in stub.events
    assert set(stub.timeouts) == {10}


@pytest.mark.parametrize("enhanced", [False, True])
def test_expected_validation_400_is_not_treated_as_an_authority_failure(
    bounded_journey: None, enhanced: bool
) -> None:
    stub = PageStub(enhanced=enhanced, status=400)
    journey.submit(
        cast("Page", stub), cast("Locator", stub), enhanced=enhanced, invalid=True
    )
    action = "click" if enhanced else "enter"
    assert stub.events.index("response_subscribed") < stub.events.index(action)
    followup = "hx_settled" if enhanced else "navigation_wait"
    assert stub.events.index("response_read") < stub.events.index(followup)
    assert set(stub.timeouts) == {10}


def test_native_key_delivery_has_a_driver_deadline(bounded_journey: None) -> None:
    stub = PageStub()
    stub.press_pending = True
    with pytest.raises(EvaluateTimeoutError):
        journey.submit(cast("Page", stub), cast("Locator", stub), enhanced=False)
    assert "navigation_cancelled" in stub.events


def test_hx_key_delivery_has_a_driver_deadline(bounded_journey: None) -> None:
    stub = PageStub(enhanced=True)
    stub.press_pending = True
    with pytest.raises(EvaluateTimeoutError):
        journey.submit(
            cast("Page", stub),
            cast("Locator", stub),
            enhanced=True,
            keyboard=True,
        )
    assert stub.events.index("response_subscribed") < stub.events.index("enter")
    assert "hx_settled" not in stub.events


def test_axe_injection_has_a_driver_deadline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stub = PageStub()
    monkeypatch.setattr(a11y_support, "evaluate_js", lambda *args, **kwargs: False)
    with pytest.raises(EvaluateTimeoutError):
        a11y_support.check_page(
            cast("Page", stub),
            "https://synthetic.invalid",
            tmp_path,
            "offline",
        )


@pytest.mark.parametrize("pending", [False, True])
def test_storage_observer_registration_uses_isolated_world_and_deadline(
    pending: bool,
) -> None:
    stub = PageStub()
    registrations = []
    registered = []

    async def register(name: str, **options: object) -> None:
        registrations.append((name, options))
        if pending:
            await asyncio.Event().wait()
        registered.append(
            {"name": name, "source": options["script"], "contentScript": True}
        )

    stub.context.browser = SimpleNamespace(
        _impl_obj=SimpleNamespace(
            _browser_type=SimpleNamespace(
                _playwright=SimpleNamespace(
                    selectors=SimpleNamespace(
                        register=register, _selector_engines=registered
                    )
                )
            )
        )
    )
    if pending:
        with pytest.raises(EvaluateTimeoutError):
            storage.register_storage_observer(cast("Page", stub))
    else:
        assert (
            storage.register_storage_observer(cast("Page", stub))
            is stub.context.browser
        )
        assert (
            storage.register_storage_observer(cast("Page", stub))
            is stub.context.browser
        )
    assert registrations == [
        (storage.ENGINE, {"script": storage.SCRIPT, "contentScript": True})
    ]


@pytest.mark.parametrize(
    ("field", "value"),
    [("source", "different-engine"), ("contentScript", False)],
)
def test_storage_observer_refuses_a_conflicting_shared_engine(
    field: str, value: object
) -> None:
    stub = PageStub()
    calls = []

    async def register(*_args: object, **_kwargs: object) -> None:
        calls.append(True)

    entry = {
        "name": storage.ENGINE,
        "source": storage.SCRIPT,
        "contentScript": True,
        field: value,
    }
    stub.context.browser = SimpleNamespace(
        _impl_obj=SimpleNamespace(
            _browser_type=SimpleNamespace(
                _playwright=SimpleNamespace(
                    selectors=SimpleNamespace(
                        register=register, _selector_engines=[entry]
                    )
                )
            )
        )
    )
    with pytest.raises(AssertionError):
        storage.register_storage_observer(cast("Page", stub))
    assert calls == []


@pytest.mark.parametrize(
    ("databases", "static_cache_only", "accepted"),
    [(0, True, True), (1, True, False), (0, False, False)],
)
def test_original_context_storage_observations_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    databases: int,
    static_cache_only: bool,
    accepted: bool,
) -> None:
    subscribed = []

    def settled(attribute: str, value: str, *, timeout: float) -> None:
        assert (attribute, value, timeout) == (storage.ATTRIBUTE, "pending", 10)
        subscribed.append(True)

    def observed(attribute: str) -> str:
        assert subscribed
        assert attribute == storage.ATTRIBUTE
        return json.dumps(
            {"indexedDB": databases, "staticCacheOnly": static_cache_only}
        )

    element = SimpleNamespace(get_attribute=observed)

    def locate(selector: str) -> SimpleNamespace:
        assert selector == f"{storage.ENGINE}=observe"
        return element

    def expected(target: object) -> SimpleNamespace:
        assert target is element
        return SimpleNamespace(not_to_have_attribute=settled)

    page = SimpleNamespace(
        locator=locate,
        _impl_obj=SimpleNamespace(
            _timeout_settings=SimpleNamespace(timeout=lambda: 10)
        ),
    )
    monkeypatch.setattr(storage, "expect", expected)
    if accepted:
        assert storage.observe_storage(cast("Page", page)) == {
            "indexedDB": 0,
            "staticCacheOnly": True,
        }
    else:
        with pytest.raises(AssertionError):
            storage.observe_storage(cast("Page", page))


@pytest.mark.parametrize(
    ("databases", "cached_path", "static_cache_only"),
    [
        (0, "/static/app.css", True),
        (1, "/static/app.css", True),
        (0, "/clinical-content/", False),
    ],
)
def test_storage_script_reads_actual_sinks_and_ignores_dom_spoof(
    tmp_path: Path,
    databases: int,
    cached_path: str,
    static_cache_only: bool,
) -> None:
    node, _ = compute_driver_executable()
    script = f"""
      let calls = 0;
      globalThis.indexedDB = {{
        databases: async () => {{ calls++; return Array({databases}).fill({{}}); }}
      }};
      globalThis.caches = {{
        keys: async () => ['existing-cache'],
        open: async () => ({{
          keys: async () => [{{url: 'https://synthetic.invalid{cached_path}'}}]
        }})
      }};
      let completed;
      const observation = new Promise(resolve => {{ completed = resolve; }});
      const attributes = new Map([
        ['{storage.ATTRIBUTE}', '{{"indexedDB":0,"staticCacheOnly":true}}']
      ]);
      const element = {{
        setAttribute(name, value) {{
          attributes.set(name, value);
          if (value !== 'pending') completed(value);
        }}
      }};
      const root = {{documentElement: element}};
      const engine = {storage.SCRIPT};
      if (engine.query(root) !== element) throw Error('wrong observation root');
      const observed = JSON.parse(await observation);
      if (engine.queryAll(root)[0] !== element) throw Error('wrong retry root');
      console.log(JSON.stringify({{...observed, calls}}));
    """
    log = tmp_path / "storage-observation.json"
    assert (
        _run_bounded(
            [node, "--input-type=module", "--eval", script],
            {},
            log,
            timeout=10,
        )
        == 0
    )
    assert json.loads(log.read_text()) == {
        "indexedDB": databases,
        "staticCacheOnly": static_cache_only,
        "calls": 1,
    }

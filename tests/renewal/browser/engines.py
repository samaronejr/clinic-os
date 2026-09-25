"""Browser engine selection and mobile emulation profiles for runner suites.

The renewal runner exports ``CLINIC_BROWSER_ENGINE`` (``chromium`` by default,
keeping CI parity) and the resolved executable for that engine. An unknown
engine fails the suite; it never skips and never falls back to Chromium.

Mobile profiles mirror Playwright 1.61's ``iPhone 15`` and ``Pixel 8``
device descriptors (viewport, device scale factor, user agent) with touch
enabled, so a suite can assert touch-first layouts on every engine.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import pytest

if TYPE_CHECKING:
    from playwright.sync_api import Browser, BrowserContext, Playwright

ENGINES: Final = ("chromium", "firefox", "webkit")
DEFAULT_ENGINE: Final = "chromium"
CHROMIUM_ARGS: Final = ("--no-sandbox", "--disable-dev-shm-usage")


@dataclass(frozen=True, slots=True)
class MobileProfile:
    """One emulated phone: CSS viewport, pixel density and user agent."""

    label: str
    width: int
    height: int
    device_scale_factor: float
    user_agent: str


MOBILE_PROFILES: Final = {
    "iphone-15": MobileProfile(
        label="iPhone 15",
        width=393,
        height=659,
        device_scale_factor=3,
        user_agent=(
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X)"
            " AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.5"
            " Mobile/15E148 Safari/604.1"
        ),
    ),
    "pixel-8": MobileProfile(
        label="Pixel 8",
        width=412,
        height=839,
        device_scale_factor=2.625,
        user_agent=(
            "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36"
            " (KHTML, like Gecko) Chrome/149.0.7827.55 Mobile Safari/537.36"
        ),
    ),
}


def selected_engine() -> str:
    """Return the runner-selected engine; an unknown name fails the suite."""
    engine = os.environ.get("CLINIC_BROWSER_ENGINE", "") or DEFAULT_ENGINE
    if engine not in ENGINES:
        pytest.fail(f"CLINIC_BROWSER_ENGINE names an unsupported engine: {engine}")
    return engine


def launch(driver: Playwright, engine: str, executable: str) -> Browser:
    """Launch ``engine`` from the runner-resolved executable, never another."""
    if engine == "chromium":
        return driver.chromium.launch(
            executable_path=executable, args=list(CHROMIUM_ARGS)
        )
    browser_type = {"firefox": driver.firefox, "webkit": driver.webkit}[engine]
    return browser_type.launch(executable_path=executable)


def mobile_context(
    browser: Browser, profile: str, *, locale: str = "pt-BR"
) -> BrowserContext:
    """Open a touch-enabled context emulating one of ``MOBILE_PROFILES``."""
    device = MOBILE_PROFILES[profile]
    return browser.new_context(
        viewport={"width": device.width, "height": device.height},
        device_scale_factor=device.device_scale_factor,
        user_agent=device.user_agent,
        is_mobile=True,
        has_touch=True,
        locale=locale,
    )

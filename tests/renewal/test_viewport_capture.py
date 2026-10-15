"""Task 18: invalid viewport evidence cannot produce a green Manual-QA."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from unittest.mock import create_autospec

import pytest
from PIL import Image, ImageDraw
from playwright.sync_api import Page

from renewal.browser import _viewport_capture as capture

FOOTER: capture.FooterRect = {
    "left": 16.0,
    "top": 820.0,
    "right": 374.0,
    "bottom": 884.0,
}


def _png(*, upper: bool, footer: bool, width: int = 390) -> bytes:
    with Image.new("RGB", (width, 900), (18, 48, 61)) as image:
        draw = ImageDraw.Draw(image)
        if upper:
            draw.rectangle((20, 20, 200, 40), fill=(230, 240, 242))
        if footer:
            draw.rectangle((20, 840, 300, 860), fill=(169, 192, 200))
        output = BytesIO()
        image.save(output, format="PNG")
        return output.getvalue()


@pytest.mark.parametrize(
    ("upper", "footer", "reason"),
    [(False, False, "blank"), (True, False, "footer")],
)
def test_uniform_or_partial_frame_is_rejected(
    upper: bool, footer: bool, reason: str
) -> None:
    # Given a flat frame, or content whose expected footer was never painted.
    png = _png(upper=upper, footer=footer)

    # When the real pixel validator examines it.
    with pytest.raises(capture.CaptureEvidenceError) as failure:
        capture.validate_viewport_png(png, width=390, height=900, footer=FOOTER)

    # Then neither can be accepted as evidence.
    assert failure.value.reason == reason


def test_populated_frame_and_footer_have_real_pixel_variance() -> None:
    # Given a composed frame containing both content and footer pixels.
    png = _png(upper=True, footer=True)

    # When its complete viewport and visible footer are checked.
    proof = capture.validate_viewport_png(png, width=390, height=900, footer=FOOTER)

    # Then both regions contain more than one colour.
    assert max(proof.variance) > 0
    assert proof.footer_variance is not None
    assert max(proof.footer_variance) > 0


def test_wrong_viewport_dimensions_are_not_accepted() -> None:
    # Given pixels from a viewport other than the requested width.
    png = _png(upper=True, footer=True, width=391)

    # When they are validated against the requested viewport.
    with pytest.raises(capture.CaptureEvidenceError) as failure:
        capture.validate_viewport_png(png, width=390, height=900)

    # Then the mismatch fails instead of being relabelled.
    assert failure.value.reason == "dimensions"


def test_offscreen_footer_is_not_silently_clipped() -> None:
    # Given stale geometry outside the captured viewport.
    footer: capture.FooterRect = {**FOOTER, "bottom": 920.0}

    # When a valid image is checked against that geometry.
    with pytest.raises(capture.CaptureEvidenceError) as failure:
        capture.validate_viewport_png(
            _png(upper=True, footer=True), width=390, height=900, footer=footer
        )

    # Then it cannot pass by cropping away the missing region.
    assert failure.value.reason == "footer"


def test_capture_waits_then_keeps_one_failed_raster(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Given the real decoder and a Page adapter returning a uniform raster.
    page = create_autospec(Page, instance=True)
    page.viewport_size = {"width": 1280, "height": 720}
    png = _png(upper=False, footer=False)
    stages: list[str | int] = []

    def settle(
        _page: Page, _expression: str, arg: dict[str, str | int]
    ) -> capture.CaptureState:
        stages.append(arg["stage"])
        return {
            "epoch": 1.0,
            "scroll_y": 2000.0,
            "document_height": 2900,
            "footer": FOOTER,
        }

    def screenshot(*, path: str, animations: str, scale: str) -> bytes:
        assert stages == ["theme", "scroll"]
        assert animations == "disabled"
        assert scale == "css"
        Path(path).write_bytes(png)
        return png

    monkeypatch.setattr(capture, "evaluate_js", settle)
    page.screenshot.side_effect = screenshot
    destination = tmp_path / "frame.png"

    # When the capture fails its actual pixel validation.
    with pytest.raises(capture.CaptureEvidenceError) as failure:
        capture.capture_viewport(
            page,
            destination,
            theme="dark",
            size={"width": 390, "height": 900},
            position="end",
        )

    # Then readiness precedes the only raster, and the red pixels remain.
    assert failure.value.reason == "blank"
    assert page.screenshot.call_count == 1
    assert destination.read_bytes() == png
    assert destination.stat().st_mode & 0o777 == 0o600

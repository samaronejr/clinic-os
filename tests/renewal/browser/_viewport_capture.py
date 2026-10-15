"""Settled, pixel-validated viewport evidence for real-server Manual-QA."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from io import BytesIO
from typing import TYPE_CHECKING, Final, Literal, TypedDict

from PIL import Image, ImageStat

from renewal.browser._page_wait import evaluate_js

if TYPE_CHECKING:
    from pathlib import Path

    from playwright.sync_api import Page, ViewportSize

type Theme = Literal["light", "dark"]
type Position = Literal["top", "end"]


class CaptureFailure(StrEnum):
    """Machine-readable reasons a frame cannot be used as evidence."""

    FORMAT = "format"
    DIMENSIONS = "dimensions"
    BLANK = "blank"
    FOOTER = "footer"
    NAVIGATION = "navigation"


class FooterRect(TypedDict):
    """The rendered footer paragraph in CSS viewport coordinates."""

    left: float
    top: float
    right: float
    bottom: float


class CaptureState(TypedDict):
    """Content-free geometry returned by the settled browser observer."""

    epoch: float
    scroll_y: float
    document_height: int
    footer: FooterRect


@dataclass(frozen=True, slots=True)
class PixelEvidence:
    """Raster variation, not a claim based only on file existence."""

    variance: tuple[float, ...]
    footer_variance: tuple[float, ...] | None


@dataclass(frozen=True, slots=True)
class CaptureProof:
    """Geometry and raster facts retained without clinical page content."""

    scroll_y: float
    document_height: int
    footer_bounds: tuple[float, float, float, float]
    pixels: PixelEvidence


class CaptureEvidenceError(RuntimeError):
    """A captured raster cannot prove the intended viewport."""

    def __init__(self, reason: CaptureFailure, *, artifact: str | None = None) -> None:
        self.reason = reason
        self.artifact = artifact
        prefix = f"{artifact}: " if artifact is not None else ""
        super().__init__(f"{prefix}invalid viewport capture ({reason})")


# Subscribe before changing state. Resolve only after the requested viewport,
# loaded fonts/images, completed transitions and geometry agree on consecutive
# rendering updates. The scroll stage additionally requires the actual offset
# and the whole footer paragraph in view. evaluate_js supplies the driver-side
# deadline even if the renderer or animation-frame delivery stops.
_SETTLED_CAPTURE_JS: Final = """({stage, theme, width, height, position}) =>
  new Promise((resolve, reject) => {
    const root = document.documentElement;
    let previous = null;
    const inspect = () => {
      const footer = document.querySelector('.site-footer p');
      if (!footer) {
        reject(new Error('capture footer is missing'));
        return;
      }
      const rect = footer.getBoundingClientRect();
      const scrolling = document.scrollingElement;
      const target = position === 'end'
        ? Math.max(0, scrolling.scrollHeight - innerHeight) : 0;
      const state = {
        epoch: performance.timeOrigin,
        scroll_y: scrollY,
        document_height: scrolling.scrollHeight,
        footer: {left: rect.left, top: rect.top,
                 right: rect.right, bottom: rect.bottom}
      };
      const ready = document.readyState === 'complete'
        && document.fonts.status === 'loaded'
        && Array.from(document.images).every(image =>
          image.complete && image.naturalWidth > 0)
        && root.dataset.theme === theme
        && innerWidth === width && innerHeight === height
        && !document.getAnimations().some(animation =>
          animation.pending || animation.playState === 'running')
        && (stage === 'theme' || (
          scrollX === 0 && Math.abs(scrollY - target) <= 0.5
          && (position !== 'end' || (
            rect.top >= 0 && rect.bottom <= innerHeight
            && rect.left >= 0 && rect.right <= innerWidth))));
      const signature = JSON.stringify([
        state, scrolling.scrollWidth,
        getComputedStyle(document.body).backgroundColor
      ]);
      if (ready && signature === previous) {
        resolve(state);
        return;
      }
      previous = ready ? signature : null;
      requestAnimationFrame(inspect);
    };
    requestAnimationFrame(inspect);
    if (stage === 'theme') {
      root.dataset.theme = theme;
    } else {
      window.scrollTo({
        left: 0,
        top: position === 'end'
          ? Math.max(0, document.scrollingElement.scrollHeight - innerHeight) : 0,
        behavior: 'instant'
      });
    }
  })"""


def validate_viewport_png(
    png: bytes, *, width: int, height: int, footer: FooterRect | None = None
) -> PixelEvidence:
    """Reject flat frames and end frames whose expected footer is unpainted."""
    with Image.open(BytesIO(png)) as source, source.convert("RGB") as image:
        if source.format != "PNG":
            raise CaptureEvidenceError(CaptureFailure.FORMAT)
        if image.size != (width, height):
            raise CaptureEvidenceError(CaptureFailure.DIMENSIONS)
        variance = tuple(ImageStat.Stat(image).var)
        if max(variance) == 0:
            raise CaptureEvidenceError(CaptureFailure.BLANK)
        footer_variance = None
        if footer is not None:
            box = (
                math.floor(footer["left"]),
                math.floor(footer["top"]),
                math.ceil(footer["right"]),
                math.ceil(footer["bottom"]),
            )
            left, top, right, bottom = box
            if not (0 <= left < right <= width and 0 <= top < bottom <= height):
                raise CaptureEvidenceError(CaptureFailure.FOOTER)
            with image.crop(box) as visible_footer:
                footer_variance = tuple(ImageStat.Stat(visible_footer).var)
            if max(footer_variance) == 0:
                raise CaptureEvidenceError(CaptureFailure.FOOTER)
        return PixelEvidence(variance, footer_variance)


def capture_viewport(
    page: Page,
    destination: Path,
    *,
    theme: Theme,
    size: ViewportSize,
    position: Position,
) -> CaptureProof:
    """Settle the live viewport and take exactly one validated screenshot."""
    if page.viewport_size != size:
        page.set_viewport_size(size)
    arguments = {
        "theme": theme,
        "width": size["width"],
        "height": size["height"],
        "position": position,
    }
    evaluate_js(page, _SETTLED_CAPTURE_JS, {**arguments, "stage": "theme"})
    state: CaptureState = evaluate_js(
        page, _SETTLED_CAPTURE_JS, {**arguments, "stage": "scroll"}
    )
    png = page.screenshot(path=str(destination), animations="disabled", scale="css")
    destination.chmod(0o600)
    try:
        pixels = validate_viewport_png(
            png,
            width=size["width"],
            height=size["height"],
            footer=state["footer"] if position == "end" else None,
        )
    except CaptureEvidenceError as failure:
        error = CaptureEvidenceError(failure.reason, artifact=destination.name)
        raise error from failure
    if evaluate_js(page, "() => performance.timeOrigin") != state["epoch"]:
        raise CaptureEvidenceError(CaptureFailure.NAVIGATION, artifact=destination.name)
    footer = state["footer"]
    bounds = (footer["left"], footer["top"], footer["right"], footer["bottom"])
    return CaptureProof(state["scroll_y"], state["document_height"], bounds, pixels)

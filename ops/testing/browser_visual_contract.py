"""Shared executable DOM, responsive, and accessibility assertions.

The audit runs inside the real rendered page and evaluates the WCAG 2.1 A/AA
rules whose axe-core equivalents are classified serious or critical. It uses
computed styles and live layout boxes, so it cannot be satisfied by markup that
only looks correct in the template source.
"""

from __future__ import annotations

from typing import Final, Never

VIEWPORTS: Final = (
    {"height": 812, "label": "mobile-375", "width": 375, "zoom": 1.0},
    {"height": 1024, "label": "tablet-768", "width": 768, "zoom": 1.0},
    {"height": 900, "label": "desktop-1280", "width": 1280, "zoom": 1.0},
    {"height": 900, "label": "desktop-1280-zoom-200", "width": 1280, "zoom": 2.0},
)
SERIOUS_RULES: Final = frozenset(
    {
        "aria-reference-resolves",
        "button-name",
        "color-contrast",
        "document-title",
        "duplicate-id",
        "form-label",
        "heading-order",
        "html-has-lang",
        "image-alt",
        "landmark-one-main",
        "link-name",
        "no-horizontal-overflow",
        "page-has-heading-one",
        "target-size",
        "th-has-scope",
    }
)
AUDIT_SCRIPT: Final = r"""
() => {
  const violations = [];
  const add = (rule, impact, target) =>
    violations.push({ rule, impact, target: target.slice(0, 120) });
  const describe = (node) => {
    const id = node.id ? `#${node.id}` : "";
    const cls = node.className && typeof node.className === "string"
      ? `.${node.className.trim().split(/\s+/).join(".")}` : "";
    return `${node.tagName.toLowerCase()}${id}${cls}`;
  };
  const visible = (node) => {
    const style = getComputedStyle(node);
    if (style.display === "none" || style.visibility === "hidden") return false;
    if (style.opacity === "0") return false;
    const box = node.getBoundingClientRect();
    return box.width > 0 && box.height > 0;
  };
  const parseColor = (value) => {
    const match = value.match(/rgba?\(([^)]+)\)/);
    if (!match) return null;
    const parts = match[1].split(",").map((item) => parseFloat(item.trim()));
    const alpha = parts.length > 3 ? parts[3] : 1;
    return { r: parts[0], g: parts[1], b: parts[2], a: alpha };
  };
  const channel = (value) => {
    const scaled = value / 255;
    return scaled <= 0.03928
      ? scaled / 12.92
      : Math.pow((scaled + 0.055) / 1.055, 2.4);
  };
  const luminance = (color) =>
    0.2126 * channel(color.r) + 0.7152 * channel(color.g) + 0.0722 * channel(color.b);
  const backgroundOf = (node) => {
    let current = node;
    while (current && current !== document.documentElement) {
      const color = parseColor(getComputedStyle(current).backgroundColor);
      if (color && color.a > 0) return color;
      current = current.parentElement;
    }
    return { r: 255, g: 255, b: 255, a: 1 };
  };
  const contrast = (front, back) => {
    const first = luminance(front) + 0.05;
    const second = luminance(back) + 0.05;
    return first > second ? first / second : second / first;
  };

  if (!document.documentElement.getAttribute("lang")) {
    add("html-has-lang", "serious", "html");
  }
  if (!document.title.trim()) add("document-title", "serious", "head>title");
  if (document.querySelectorAll("main").length !== 1) {
    add("landmark-one-main", "serious", "main");
  }
  if (document.querySelectorAll("h1").length !== 1) {
    add("page-has-heading-one", "serious", "h1");
  }

  const seen = new Set();
  document.querySelectorAll("[id]").forEach((node) => {
    if (seen.has(node.id)) add("duplicate-id", "serious", describe(node));
    seen.add(node.id);
  });

  ["aria-describedby", "aria-labelledby", "aria-controls"].forEach((attribute) => {
    document.querySelectorAll(`[${attribute}]`).forEach((node) => {
      node.getAttribute(attribute).split(/\s+/).filter(Boolean).forEach((id) => {
        if (!document.getElementById(id)) {
          add("aria-reference-resolves", "critical", `${describe(node)}[${id}]`);
        }
      });
    });
  });

  document.querySelectorAll("img").forEach((node) => {
    const decorative = node.getAttribute("role") === "presentation";
    if (node.getAttribute("alt") === null && !decorative) {
      add("image-alt", "critical", describe(node));
    }
  });

  const skipTypes = new Set(["hidden", "submit", "button", "reset", "image"]);
  document.querySelectorAll("input, select, textarea").forEach((node) => {
    if (skipTypes.has(node.getAttribute("type"))) return;
    const labelled =
      (node.id && document.querySelector(`label[for="${CSS.escape(node.id)}"]`)) ||
      node.closest("label") ||
      node.getAttribute("aria-label") ||
      node.getAttribute("aria-labelledby");
    if (!labelled) add("form-label", "critical", describe(node));
  });

  document.querySelectorAll("button").forEach((node) => {
    const name = (node.textContent || "").trim() || node.getAttribute("aria-label");
    if (!name) add("button-name", "critical", describe(node));
  });
  document.querySelectorAll("a[href]").forEach((node) => {
    const name = (node.textContent || "").trim() || node.getAttribute("aria-label");
    if (!name) add("link-name", "serious", describe(node));
  });

  document.querySelectorAll("table th").forEach((node) => {
    if (!node.getAttribute("scope")) add("th-has-scope", "serious", describe(node));
  });

  let previous = 0;
  document.querySelectorAll("h1,h2,h3,h4,h5,h6").forEach((node) => {
    const level = Number(node.tagName.substring(1));
    if (previous && level > previous + 1) {
      add("heading-order", "serious", describe(node));
    }
    previous = level;
  });

  const textual = "button, a[href], input, select, textarea, p, h1, h2, li,"
    + " td, th, caption, label, span";
  document.querySelectorAll(textual)
    .forEach((node) => {
      if (!visible(node)) return;
      const direct = Array.from(node.childNodes).some(
        (child) => child.nodeType === 3 && child.textContent.trim().length > 0,
      );
      if (!direct) return;
      const style = getComputedStyle(node);
      const front = parseColor(style.color);
      if (!front || front.a === 0) return;
      const ratio = contrast(front, backgroundOf(node));
      const size = parseFloat(style.fontSize);
      const bold = Number(style.fontWeight) >= 700;
      const large = size >= 24 || (size >= 18.66 && bold);
      if (ratio < (large ? 3 : 4.5)) {
        add("color-contrast", "serious", `${describe(node)}@${ratio.toFixed(2)}`);
      }
    });

  document.querySelectorAll("button, a[href], input[type=submit]").forEach((node) => {
    if (!visible(node)) return;
    const box = node.getBoundingClientRect();
    if (box.height < 24 || box.width < 24) {
      const size = `${Math.round(box.width)}x${Math.round(box.height)}`;
      add("target-size", "serious", `${describe(node)}@${size}`);
    }
  });

  const root = document.documentElement;
  const overflow = root.scrollWidth - root.clientWidth;
  if (overflow > 1) {
    add("no-horizontal-overflow", "serious", `document@${overflow}`);
  }

  return violations;
}
"""


class VisualContractError(AssertionError):
    """Report one failed shared browser contract without leaking patient data."""


def _fail(message: str) -> Never:
    raise VisualContractError(message)


def blocking_violations(violations: list[dict[str, str]]) -> list[dict[str, str]]:
    """Return only the serious or critical violations this contract blocks on."""
    return [
        violation
        for violation in violations
        if violation.get("impact") in {"critical", "serious"}
        and violation.get("rule") in SERIOUS_RULES
    ]


def require_no_blocking_violations(
    label: str,
    violations: list[dict[str, str]],
) -> None:
    """Fail the contract when any serious or critical rule is violated."""
    blocking = blocking_violations(violations)
    if blocking:
        summary = "; ".join(
            f"{item['rule']}({item['impact']}) at {item['target']}" for item in blocking
        )
        _fail(f"{label}: {len(blocking)} blocking accessibility violations: {summary}")


def require_clean_console(label: str, messages: list[str]) -> None:
    """Fail the contract when the live page reported an error or warning."""
    if messages:
        _fail(f"{label}: browser console reported {len(messages)}: {messages[:5]}")


def require_no_state_in_url(label: str, url: str, sentinels: tuple[str, ...]) -> None:
    """Fail the contract when any demographic or query value reached the URL."""
    if "?" in url:
        _fail(f"{label}: URL carries a query string")
    for sentinel in sentinels:
        if sentinel and sentinel in url:
            _fail(f"{label}: URL carries forbidden state")


def require_post_only_forms(label: str, methods: list[str], actions: list[str]) -> None:
    """Fail the contract when a rendered form is not a body-only POST."""
    if not methods:
        _fail(f"{label}: no form was rendered")
    for method in methods:
        if method.lower() != "post":
            _fail(f"{label}: rendered a non-POST form")
    for action in actions:
        if "?" in action:
            _fail(f"{label}: form action carries a query string")

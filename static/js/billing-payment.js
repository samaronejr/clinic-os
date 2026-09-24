"use strict";

// Payment screens: an accessible clipboard copy for the PIX text, and an
// honest report when a bounded status refresh cannot reach the server. No
// state is ever decided here: the server owns the payment state, and a failed
// refresh only says the screen may be out of date.

const COPY_OK = "Código copiado.";
const COPY_FAILED = "Não foi possível copiar automaticamente. Selecione o código acima e copie.";
const OFFLINE_SELECTOR = "[data-offline]";

function statusRegion() {
  return document.getElementById("payment-status");
}

function revealCopyButtons(root) {
  const canCopy =
    typeof navigator !== "undefined" &&
    navigator.clipboard !== undefined &&
    typeof navigator.clipboard.writeText === "function";
  if (!canCopy) {
    return;
  }
  root.querySelectorAll("[data-copy]").forEach((button) => {
    if (button instanceof HTMLElement) {
      button.hidden = false;
    }
  });
}

function reportCopy(button, message) {
  const status = document.getElementById(
    button.getAttribute("aria-describedby") || "",
  );
  if (status instanceof HTMLElement) {
    status.textContent = message;
  }
}

function selectCode(code) {
  const selection = window.getSelection();
  if (!selection) {
    return;
  }
  const range = document.createRange();
  range.selectNodeContents(code);
  selection.removeAllRanges();
  selection.addRange(range);
}

function copyCode(button) {
  const code = document.getElementById(button.dataset.copy || "");
  if (!(code instanceof HTMLElement)) {
    return;
  }
  const text = code.textContent || "";
  navigator.clipboard.writeText(text).then(
    () => reportCopy(button, COPY_OK),
    () => {
      selectCode(code);
      reportCopy(button, COPY_FAILED);
    },
  );
}

function showOffline(isOffline) {
  const region = statusRegion();
  if (!region) {
    return;
  }
  const notice = region.querySelector(OFFLINE_SELECTOR);
  if (notice instanceof HTMLElement) {
    notice.hidden = !isOffline;
  }
}

function isStatusRequest(event) {
  const element = event.detail ? event.detail.elt : null;
  const region = statusRegion();
  return (
    element instanceof Element && region !== null && region.contains(element)
  );
}

document.addEventListener("click", (event) => {
  const target = event.target;
  const button =
    target instanceof Element ? target.closest("[data-copy]") : null;
  if (button instanceof HTMLElement) {
    copyCode(button);
  }
});

document.addEventListener("DOMContentLoaded", () => revealCopyButtons(document));

document.addEventListener("htmx:load", (event) => {
  const root = event.detail.elt;
  if (root instanceof Element) {
    revealCopyButtons(root);
  }
});

// A refresh that never reached the server, or that the server refused, stops
// the bounded chain: say so instead of leaving a stale screen looking live.
for (const eventName of ["htmx:sendError", "htmx:responseError", "htmx:timeout"]) {
  document.addEventListener(eventName, (event) => {
    if (isStatusRequest(event)) {
      showOffline(true);
    }
  });
}

// After a successful swap the requesting element is gone; the swap target is
// the status region itself, so a completed refresh clears the stale warning.
document.addEventListener("htmx:afterSwap", (event) => {
  const target = event.detail ? event.detail.target : null;
  const region = statusRegion();
  if (
    target instanceof Element &&
    region !== null &&
    (target === region || region.contains(target))
  ) {
    showOffline(false);
  }
});

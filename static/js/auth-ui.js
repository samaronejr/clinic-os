"use strict";

function authFormFor(element) {
  return element instanceof Element ? element.closest("[data-auth-form]") : null;
}

function setAuthBusy(form, isBusy) {
  if (isBusy) {
    form.setAttribute("aria-busy", "true");
  } else {
    form.removeAttribute("aria-busy");
  }
  form.querySelectorAll('button[type="submit"]').forEach((button) => {
    if (button instanceof HTMLButtonElement) {
      button.disabled = isBusy;
    }
  });
}

function focusAuthError(root) {
  const errorSummary = root.querySelector("[data-focus-error]");
  if (errorSummary instanceof HTMLElement) {
    errorSummary.focus();
  }
}

function authNetworkNotice(form) {
  const notice = form.querySelector("[data-network-error]");
  return notice instanceof HTMLElement ? notice : null;
}

function showAuthNetworkNotice(form) {
  const notice = authNetworkNotice(form);
  if (notice) {
    notice.hidden = false;
    notice.focus();
  }
}

function hideAuthNetworkNotice(form) {
  const notice = authNetworkNotice(form);
  if (notice) {
    notice.hidden = true;
  }
}

function revealFocusInScrollRegion(event) {
  const target = event.target;
  if (!(target instanceof Element) || !target.closest(".table-scroll")) {
    return;
  }
  // Chromium leaves a partially visible control where it is; keep the whole
  // focused control and its ring inside the scroll region.
  target.scrollIntoView({ block: "nearest", inline: "nearest" });
}

document.addEventListener("DOMContentLoaded", () => focusAuthError(document));
document.addEventListener("focusin", revealFocusInScrollRegion);

document.addEventListener("htmx:beforeRequest", (event) => {
  const form = authFormFor(event.detail.elt);
  if (form instanceof HTMLFormElement) {
    hideAuthNetworkNotice(form);
    setAuthBusy(form, true);
  }
});

document.addEventListener("htmx:afterRequest", (event) => {
  const form = authFormFor(event.detail.elt);
  if (form instanceof HTMLFormElement) {
    setAuthBusy(form, false);
  }
});

// A request that never completed — refused, timed out or unreachable —
// leaves the form disabled and the screen unchanged: say so and re-enable
// the submit so the receptionist can retry deliberately.
for (const eventName of ["htmx:sendError", "htmx:responseError", "htmx:timeout"]) {
  document.addEventListener(eventName, (event) => {
    const form = authFormFor(event.detail.elt);
    if (form instanceof HTMLFormElement) {
      setAuthBusy(form, false);
      showAuthNetworkNotice(form);
    }
  });
}

document.addEventListener("htmx:afterSwap", (event) => {
  const root = event.detail.target;
  focusAuthError(root instanceof Element ? root : document);
});

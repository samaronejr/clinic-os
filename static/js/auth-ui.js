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

document.addEventListener("DOMContentLoaded", () => focusAuthError(document));

document.addEventListener("htmx:beforeRequest", (event) => {
  const form = authFormFor(event.detail.elt);
  if (form instanceof HTMLFormElement) {
    setAuthBusy(form, true);
  }
});

for (const eventName of ["htmx:afterRequest", "htmx:responseError"]) {
  document.addEventListener(eventName, (event) => {
    const form = authFormFor(event.detail.elt);
    if (form instanceof HTMLFormElement) {
      setAuthBusy(form, false);
    }
  });
}

document.addEventListener("htmx:afterSwap", (event) => {
  const root = event.detail.target;
  focusAuthError(root instanceof Element ? root : document);
});

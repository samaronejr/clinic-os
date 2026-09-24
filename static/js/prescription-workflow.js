"use strict";

// Submit feedback only. The server serializes repeated submits per document
// (advisory lock plus stored-state checks), so a second click can never start
// a second operation; this script just shows that the first click landed.
function onceForm(target) {
  return target instanceof Element ? target.closest("form[data-once]") : null;
}

function setOnceBusy(form, isBusy) {
  if (isBusy) {
    form.setAttribute("aria-busy", "true");
  } else {
    form.removeAttribute("aria-busy");
  }
  form.querySelectorAll("[data-once-status]").forEach((status) => {
    status.hidden = !isBusy;
  });
  form.querySelectorAll('button[type="submit"]').forEach((button) => {
    if (button instanceof HTMLButtonElement) {
      button.disabled = isBusy;
    }
  });
}

document.addEventListener("submit", (event) => {
  const form = onceForm(event.target);
  if (!(form instanceof HTMLFormElement) || event.defaultPrevented) {
    return;
  }
  form.setAttribute("aria-busy", "true");
  form.querySelectorAll("[data-once-status]").forEach((status) => {
    status.hidden = false;
  });
  // The pressed button's name/value enter the request when the browser
  // builds the entry list, synchronously after this event; disable after.
  setTimeout(() => setOnceBusy(form, true), 0);
});

window.addEventListener("pageshow", (event) => {
  if (!event.persisted) {
    return;
  }
  // A back-forward cache restore shows the page as it was mid-submit; the
  // controls come back so the physician can read the true state and retry.
  document.querySelectorAll('form[data-once][aria-busy="true"]').forEach((form) => {
    if (form instanceof HTMLFormElement) {
      setOnceBusy(form, false);
    }
  });
});

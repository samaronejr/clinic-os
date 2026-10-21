(function () {
  "use strict";

  function panelFor(event) {
    var detail = event.detail || {};
    var element = detail.requestConfig ? detail.requestConfig.elt : detail.elt;
    return element instanceof Element
      ? element.closest("#results-workspace")
      : null;
  }

  function networkState() {
    var panel = document.getElementById("results-workspace");
    if (!panel) return;
    var notice = panel.querySelector("[data-results-network]");
    if (!notice) return;
    var offline = navigator.onLine === false;
    if (offline) notice.hidden = false;
    panel.querySelectorAll("button[type=submit]").forEach(function (button) {
      if (offline && !button.disabled) {
        button.setAttribute("data-orders-offline-disabled", "");
        button.disabled = true;
      } else if (!offline && button.hasAttribute("data-orders-offline-disabled")) {
        button.removeAttribute("data-orders-offline-disabled");
        button.disabled = Boolean(button.closest('form[aria-busy="true"]'));
      }
    });
  }

  document.addEventListener("htmx:beforeRequest", function (event) {
    var panel = panelFor(event);
    if (!panel) return;
    panel.querySelector("[data-results-network]").hidden = true;
    panel.querySelector("[data-results-progress]").hidden = false;
  });

  document.addEventListener("htmx:beforeSwap", function (event) {
    var panel = panelFor(event);
    if (!panel) return;
    var status = event.detail.xhr.status;
    var denied = panel.querySelector("[data-results-denied]");
    if (status === 400 || status === 409) {
      event.detail.shouldSwap = true;
      event.detail.isError = false;
    } else if (status === 403) {
      denied.hidden = false;
      panel.replaceChildren(denied);
      denied.focus();
    }
  });

  document.addEventListener("htmx:afterRequest", function (event) {
    var panel = panelFor(event);
    var progress = panel && panel.querySelector("[data-results-progress]");
    if (progress) progress.hidden = true;
    networkState();
  });

  document.addEventListener("htmx:sendError", function (event) {
    var panel = panelFor(event);
    if (!panel) return;
    var notice = panel.querySelector("[data-results-network]");
    notice.hidden = false;
    notice.focus();
    networkState();
  });

  document.addEventListener("htmx:afterSwap", function () {
    var panel = document.getElementById("results-workspace");
    if (!panel) return;
    var focus = panel.querySelector("[data-focus-error], [data-results-outcome]");
    if (focus instanceof HTMLElement) focus.focus();
    networkState();
  });
  document.addEventListener("DOMContentLoaded", networkState);
  window.addEventListener("offline", networkState);
  window.addEventListener("online", networkState);
}());

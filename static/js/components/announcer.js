/* Live announcer and toasts for includes/components/announcer.html.
   window.ClinicAnnouncer.announce(message, {tone, toast}) speaks through the
   polite region (assertive for errors) and optionally shows a toast that
   stays until dismissed; at most three toasts are kept. The "clinic:announce"
   event (for example from an HTMX HX-Trigger header) calls the same API.
   Only short operational messages belong here: never clinical content,
   patient names or identifiers. Nothing is stored. */
(function () {
  "use strict";

  var MAX_TOASTS = 3;
  var TONES = ["success", "error", "warning", "info", "neutral"];

  function region(kind) {
    return document.querySelector('[data-announcer="' + kind + '"]');
  }

  function speak(message, assertive) {
    var target = region(assertive ? "assertive" : "polite");
    if (!target) {
      return;
    }
    target.textContent = "";
    window.requestAnimationFrame(function () {
      target.textContent = message;
    });
  }

  function toast(message, tone) {
    var host = document.querySelector("[data-toast-region]");
    if (!host) {
      return;
    }
    var template = document.querySelector("template[data-toast-template]");
    var item;
    if (template) {
      item = template.content.firstElementChild.cloneNode(true);
    } else {
      item = document.createElement("div");
      item.className = "toast";
      item.innerHTML = '<p class="toast-message"></p>';
    }
    if (tone !== "neutral") {
      item.classList.add("toast--" + tone);
    }
    item.setAttribute("role", "presentation");
    item.querySelector(".toast-message").textContent = message;
    item.setAttribute("data-toast-state", "entering");
    host.appendChild(item);
    window.requestAnimationFrame(function () {
      item.removeAttribute("data-toast-state");
    });
    var items = host.querySelectorAll("[data-toast], .toast");
    for (var index = 0; index < items.length - MAX_TOASTS; index += 1) {
      items[index].remove();
    }
  }

  function announce(message, options) {
    var settings = options || {};
    var tone = TONES.indexOf(settings.tone) === -1 ? "neutral" : settings.tone;
    var text = String(message || "").trim();
    if (!text) {
      return;
    }
    speak(text, tone === "error");
    if (settings.toast) {
      toast(text, tone);
    }
  }

  document.addEventListener("click", function (event) {
    var trigger = event.target.closest("[data-announce]");
    if (trigger) {
      announce(trigger.getAttribute("data-announce"), {
        tone: trigger.getAttribute("data-announce-tone"),
        toast: true
      });
      return;
    }
    var dismiss = event.target.closest("[data-toast-dismiss]");
    if (dismiss) {
      var item = dismiss.closest(".toast");
      var next = item && (item.nextElementSibling || item.previousElementSibling);
      if (item) {
        item.remove();
      }
      var focusTarget = next && next.querySelector("[data-toast-dismiss]");
      if (focusTarget) {
        focusTarget.focus();
      } else {
        var main = document.getElementById("main-content");
        if (main) {
          main.focus();
        }
      }
    }
  });

  document.addEventListener("clinic:announce", function (event) {
    var detail = event.detail || {};
    announce(detail.message, { tone: detail.tone, toast: detail.toast !== false });
  });

  window.ClinicAnnouncer = { announce: announce };
})();

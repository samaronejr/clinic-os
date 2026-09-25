/* Drawer behavior for includes/components/drawer.html.
   Non-modal: focus is never trapped. [data-drawer-toggle="<id>"] buttons
   flip aria-expanded and the drawer's hidden state; Escape inside an open
   drawer, or its close button, closes it and returns focus to the toggle. */
(function () {
  "use strict";

  function toggles(id) {
    return Array.prototype.slice.call(
      document.querySelectorAll('[data-drawer-toggle="' + id + '"]')
    );
  }

  function setOpen(drawer, isOpen, returnFocus) {
    drawer.hidden = !isOpen;
    toggles(drawer.id).forEach(function (button) {
      button.setAttribute("aria-expanded", isOpen ? "true" : "false");
    });
    if (isOpen) {
      var heading = drawer.querySelector(".drawer-title");
      if (heading) {
        heading.setAttribute("tabindex", "-1");
        heading.focus();
      }
    } else if (returnFocus) {
      var first = toggles(drawer.id)[0];
      if (first) {
        first.focus();
      }
    }
  }

  function enhance(root) {
    root.querySelectorAll("[data-drawer-collapsible]:not([data-enhanced])").forEach(
      function (drawer) {
        drawer.setAttribute("data-enhanced", "");
        if (toggles(drawer.id).length) {
          setOpen(drawer, false, false);
        }
      }
    );
  }

  document.addEventListener("click", function (event) {
    var toggle = event.target.closest("[data-drawer-toggle]");
    if (toggle) {
      var target = document.getElementById(toggle.getAttribute("data-drawer-toggle"));
      if (target) {
        setOpen(target, target.hidden, true);
      }
      return;
    }
    var close = event.target.closest("[data-drawer-close]");
    if (close) {
      var drawer = document.getElementById(close.getAttribute("data-drawer-close"));
      if (drawer && toggles(drawer.id).length) {
        setOpen(drawer, false, true);
      }
    }
  });

  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape") {
      return;
    }
    var drawer = event.target.closest && event.target.closest("[data-drawer]");
    if (drawer && !drawer.hidden && toggles(drawer.id).length) {
      setOpen(drawer, false, true);
    }
  });

  document.addEventListener("DOMContentLoaded", function () { enhance(document); });
  document.addEventListener("htmx:load", function (event) { enhance(event.target); });
})();

/* Tabs enhancement for includes/components/tabs.html (mode="panels").
   Upgrades the list of anchor links and the page's [data-tab-panel]
   sections into the ARIA tabs pattern: roving tabindex, Left/Right (and
   Home/End) move and activate, inactive panels are hidden. Links mode is
   plain navigation and is left alone. */
(function () {
  "use strict";

  function enhance(root) {
    root.querySelectorAll("[data-tabs]:not([data-enhanced])").forEach(function (container) {
      container.setAttribute("data-enhanced", "");
      var id = container.getAttribute("data-tabs");
      var list = container.querySelector(".tabs-list");
      var tabs = Array.prototype.slice.call(container.querySelectorAll("a.tab[data-tab]"));
      var panels = tabs.map(function (tab) {
        return document.getElementById(id + "-" + tab.getAttribute("data-tab"));
      });
      if (!tabs.length || panels.indexOf(null) !== -1) {
        return;
      }
      list.setAttribute("role", "tablist");
      list.querySelectorAll("li").forEach(function (item) { item.setAttribute("role", "presentation"); });
      list.querySelectorAll('a.tab[aria-disabled="true"]').forEach(function (tab) {
        tab.setAttribute("role", "tab");
        tab.removeAttribute("href");
      });

      function select(index, moveFocus) {
        tabs.forEach(function (tab, position) {
          var current = position === index;
          tab.setAttribute("aria-selected", current ? "true" : "false");
          tab.tabIndex = current ? 0 : -1;
          panels[position].hidden = !current;
        });
        if (moveFocus) {
          tabs[index].focus();
        }
      }

      tabs.forEach(function (tab, position) {
        tab.setAttribute("role", "tab");
        tab.setAttribute("aria-controls", panels[position].id);
        panels[position].setAttribute("role", "tabpanel");
        panels[position].setAttribute("aria-labelledby", tab.id);
        panels[position].tabIndex = 0;
        tab.addEventListener("click", function (event) {
          event.preventDefault();
          select(position, true);
        });
      });
      list.addEventListener("keydown", function (event) {
        var index = tabs.indexOf(document.activeElement);
        if (index === -1) {
          return;
        }
        var next = { ArrowRight: index + 1, ArrowLeft: index - 1, Home: 0, End: tabs.length - 1 }[event.key];
        if (next === undefined) {
          return;
        }
        event.preventDefault();
        select((next + tabs.length) % tabs.length, true);
      });
      var initial = tabs.findIndex(function (tab) { return tab.hasAttribute("data-tab-current"); });
      select(initial === -1 ? 0 : initial, false);
    });
  }

  document.addEventListener("DOMContentLoaded", function () { enhance(document); });
  document.addEventListener("htmx:load", function (event) { enhance(event.target); });
})();

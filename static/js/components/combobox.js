/* Combobox behavior for includes/components/combobox.html (ARIA 1.2
   editable combobox with list autocomplete). Down/Up move through options,
   Alt+Down opens, Enter chooses, Escape closes (a second Escape clears),
   Tab leaves. Static options are filtered here; with
   data-combobox-endpoint the typed text is POSTed (never put in a URL) and
   the server returns <li role="option"> markup. Nothing is stored. */
(function () {
  "use strict";

  var DEBOUNCE_MS = 200;

  function normalize(text) {
    return (text || "").normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase().trim();
  }

  function csrfToken(root) {
    var form = root.closest("form");
    var field = (form || document).querySelector("input[name=csrfmiddlewaretoken]");
    if (field) {
      return field.value;
    }
    var match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    return match ? decodeURIComponent(match[1]) : "";
  }

  function Combobox(root) {
    this.root = root;
    this.input = root.querySelector("[role=combobox]");
    this.listbox = root.querySelector("[role=listbox]");
    this.status = root.querySelector("[data-combobox-status]");
    this.endpoint = root.getAttribute("data-combobox-endpoint");
    this.choice = root.querySelector("[data-combobox-value]");
    this.selection = root.querySelector("[data-combobox-selection]");
    this.timer = null;
    this.request = null;
    this.bind();
  }

  Combobox.prototype.text = function (name, count) {
    var template = this.root.getAttribute("data-text-" + name) || "";
    return template.replace("__N__", String(count));
  };

  Combobox.prototype.options = function () {
    return Array.prototype.slice.call(this.listbox.querySelectorAll("[role=option]"));
  };

  Combobox.prototype.visible = function () {
    return this.options().filter(function (option) {
      return !option.hidden && option.getAttribute("aria-disabled") !== "true";
    });
  };

  Combobox.prototype.isOpen = function () {
    return this.input.getAttribute("aria-expanded") === "true";
  };

  Combobox.prototype.open = function () {
    if (!this.options().length) {
      return;
    }
    this.listbox.hidden = false;
    this.input.setAttribute("aria-expanded", "true");
  };

  Combobox.prototype.close = function () {
    this.listbox.hidden = true;
    this.input.setAttribute("aria-expanded", "false");
    this.setActive(null);
  };

  Combobox.prototype.setActive = function (option) {
    this.options().forEach(function (item) {
      item.setAttribute("aria-selected", item === option ? "true" : "false");
    });
    if (option) {
      this.input.setAttribute("aria-activedescendant", option.id);
      option.scrollIntoView({ block: "nearest" });
    } else {
      this.input.removeAttribute("aria-activedescendant");
    }
  };

  Combobox.prototype.active = function () {
    var id = this.input.getAttribute("aria-activedescendant");
    return id ? document.getElementById(id) : null;
  };

  Combobox.prototype.move = function (step) {
    var items = this.visible();
    if (!items.length) {
      return;
    }
    this.open();
    var index = items.indexOf(this.active());
    var next = index < 0 ? (step > 0 ? 0 : items.length - 1) : index + step;
    next = Math.max(0, Math.min(items.length - 1, next));
    this.setActive(items[next]);
  };

  /* The typed text no longer names the chosen record: drop the submitted
     value and the confirmation so the form can never send a stale choice. */
  Combobox.prototype.invalidate = function () {
    var hadChoice = Boolean(this.choice && this.choice.value);
    if (this.choice) {
      this.choice.value = "";
    }
    if (this.selection) {
      this.selection.hidden = true;
    }
    this.input.removeAttribute("aria-activedescendant");
    if (hadChoice) {
      this.root.dispatchEvent(new CustomEvent("combobox:clear", { bubbles: true }));
    }
  };

  Combobox.prototype.choose = function (option) {
    if (!option || option.getAttribute("aria-disabled") === "true") {
      return;
    }
    var label = option.querySelector("span") || option;
    this.input.value = label.textContent.trim();
    if (this.choice) {
      this.choice.value = option.getAttribute("data-value") || "";
    }
    this.close();
    this.root.dispatchEvent(new CustomEvent("combobox:select", {
      bubbles: true,
      detail: { value: option.getAttribute("data-value"), href: option.getAttribute("data-href") }
    }));
  };

  Combobox.prototype.filter = function () {
    var query = normalize(this.input.value);
    var matches = 0;
    this.options().forEach(function (option) {
      var hit = !query || normalize(option.textContent).indexOf(query) !== -1;
      option.hidden = !hit;
      matches += hit ? 1 : 0;
    });
    this.listbox.querySelectorAll(".combobox-group").forEach(function (group) {
      var next = group.nextElementSibling;
      var any = false;
      while (next && !next.classList.contains("combobox-group")) {
        any = any || !next.hidden;
        next = next.nextElementSibling;
      }
      group.hidden = !any;
    });
    this.report(matches);
  };

  Combobox.prototype.report = function (matches) {
    if (!this.status) {
      return;
    }
    this.status.textContent = matches === 1 ? this.text("result", 1)
      : matches ? this.text("results", matches) : this.text("empty", 0);
    if (matches) {
      this.open();
    } else {
      this.listbox.hidden = true;
      this.input.setAttribute("aria-expanded", "false");
    }
  };

  Combobox.prototype.search = function () {
    var self = this;
    if (self.request) {
      self.request.abort();
    }
    self.request = new AbortController();
    var body = new URLSearchParams();
    body.set("q", self.input.value);
    self.root.setAttribute("aria-busy", "true");
    if (self.status) {
      self.status.textContent = self.text("loading", 0);
    }
    fetch(self.endpoint, {
      method: "POST",
      body: body,
      credentials: "same-origin",
      headers: { "X-CSRFToken": csrfToken(self.root) },
      signal: self.request.signal
    }).then(function (response) {
      if (!response.ok) {
        throw new Error("search failed");
      }
      return response.text();
    }).then(function (html) {
      self.root.removeAttribute("aria-busy");
      self.listbox.innerHTML = html;
      self.report(self.options().length);
    }).catch(function (error) {
      if (error.name === "AbortError") {
        return;
      }
      self.root.removeAttribute("aria-busy");
      self.close();
      if (self.status) {
        self.status.textContent = self.text("error", 0);
      }
    });
  };

  Combobox.prototype.bind = function () {
    var self = this;
    self.input.addEventListener("input", function () {
      self.setActive(null);
      self.invalidate();
      if (self.endpoint) {
        window.clearTimeout(self.timer);
        self.timer = window.setTimeout(function () { self.search(); }, DEBOUNCE_MS);
      } else {
        self.filter();
      }
    });
    self.input.addEventListener("keydown", function (event) {
      if (event.key === "ArrowDown") {
        event.preventDefault();
        if (event.altKey) {
          self.open();
        } else {
          self.move(1);
        }
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        self.move(-1);
      } else if (event.key === "Enter" && self.isOpen() && self.active()) {
        event.preventDefault();
        self.choose(self.active());
      } else if (event.key === "Escape") {
        if (self.isOpen()) {
          event.preventDefault();
          event.stopPropagation();
          self.close();
        } else if (self.input.value && !self.root.closest("dialog")) {
          event.preventDefault();
          self.input.value = "";
          self.invalidate();
          self.filter();
          self.close();
        }
      } else if (event.key === "Tab") {
        self.close();
      }
    });
    self.listbox.addEventListener("mousedown", function (event) {
      event.preventDefault();
    });
    self.listbox.addEventListener("click", function (event) {
      var option = event.target.closest("[role=option]");
      if (option) {
        self.choose(option);
        self.input.focus();
      }
    });
    self.root.addEventListener("focusout", function (event) {
      if (!self.root.contains(event.relatedTarget)) {
        self.close();
      }
    });
  };

  function enhance(root) {
    root.querySelectorAll("[data-combobox]:not([data-enhanced])").forEach(function (element) {
      element.setAttribute("data-enhanced", "");
      if (element.querySelector("[role=combobox]") && element.querySelector("[role=listbox]")) {
        element.comboboxController = new Combobox(element);
      }
    });
  }

  document.addEventListener("DOMContentLoaded", function () { enhance(document); });
  document.addEventListener("htmx:load", function (event) { enhance(event.target); });
})();

/* Date picker enhancement for includes/components/datetime.html.
   The text inputs stay the source of truth (DD/MM/AAAA, HH:MM) and the
   server validates them in the clinic zone. This script adds a month grid:
   arrows move by day/week, PageUp/PageDown by month (Shift by year),
   Home/End to the week edges, Enter or Space picks, Escape closes and
   returns focus to the calendar button. "Today" comes from data-timezone. */
(function () {
  "use strict";

  var DAY_MS = 86400000;

  function pad(value) {
    return (value < 10 ? "0" : "") + value;
  }

  function utc(year, month, day) {
    return new Date(Date.UTC(year, month, day));
  }

  function iso(date) {
    return date.getUTCFullYear() + "-" + pad(date.getUTCMonth() + 1) + "-" + pad(date.getUTCDate());
  }

  function fromIso(text) {
    var match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(text || "");
    return match ? utc(+match[1], +match[2] - 1, +match[3]) : null;
  }

  function parseBr(text) {
    var match = /^(\d{2})\/(\d{2})\/(\d{4})$/.exec((text || "").trim());
    if (!match) {
      return null;
    }
    var date = utc(+match[3], +match[2] - 1, +match[1]);
    return date.getUTCDate() === +match[1] && date.getUTCMonth() === +match[2] - 1 ? date : null;
  }

  function formatBr(date) {
    return pad(date.getUTCDate()) + "/" + pad(date.getUTCMonth() + 1) + "/" + date.getUTCFullYear();
  }

  function todayIn(zone) {
    try {
      return fromIso(new Intl.DateTimeFormat("en-CA", {
        timeZone: zone, year: "numeric", month: "2-digit", day: "2-digit"
      }).format(new Date()));
    } catch (error) {
      return fromIso(new Date().toISOString().slice(0, 10));
    }
  }

  function addMonths(date, count) {
    var target = utc(date.getUTCFullYear(), date.getUTCMonth() + count, 1);
    var last = utc(target.getUTCFullYear(), target.getUTCMonth() + 1, 0).getUTCDate();
    return utc(target.getUTCFullYear(), target.getUTCMonth(), Math.min(date.getUTCDate(), last));
  }

  function Picker(root) {
    this.root = root;
    this.locale = document.documentElement.lang || "pt-BR";
    this.zone = root.getAttribute("data-timezone") || "UTC";
    this.min = fromIso(root.getAttribute("data-min"));
    this.max = fromIso(root.getAttribute("data-max"));
    this.input = root.querySelector("[data-datetime-date]");
    this.toggle = root.querySelector("[data-datetime-toggle]");
    this.calendar = root.querySelector("[data-datetime-calendar]");
    this.heading = root.querySelector("[data-datetime-month]");
    this.body = root.querySelector("[data-datetime-days]");
    this.today = todayIn(this.zone);
    this.focusDate = null;
    this.toggle.hidden = false;
    this.bind();
    if (root.hasAttribute("data-datetime-open")) {
      this.open(false);
    }
  }

  Picker.prototype.selected = function () {
    return parseBr(this.input.value);
  };

  Picker.prototype.allowed = function (date) {
    return !(this.min && date < this.min) && !(this.max && date > this.max);
  };

  Picker.prototype.render = function () {
    var focus = this.focusDate;
    var first = utc(focus.getUTCFullYear(), focus.getUTCMonth(), 1);
    var start = new Date(first.getTime() - first.getUTCDay() * DAY_MS);
    var selected = this.selected();
    var label = new Intl.DateTimeFormat(this.locale, { weekday: "long", day: "numeric", month: "long", year: "numeric", timeZone: "UTC" });
    var month = new Intl.DateTimeFormat(this.locale, { month: "long", year: "numeric", timeZone: "UTC" }).format(first);
    this.heading.textContent = month.charAt(0).toUpperCase() + month.slice(1);
    this.body.textContent = "";
    for (var week = 0; week < 6; week += 1) {
      var row = document.createElement("tr");
      for (var weekday = 0; weekday < 7; weekday += 1) {
        var date = new Date(start.getTime() + (week * 7 + weekday) * DAY_MS);
        var cell = document.createElement("td");
        var button = document.createElement("button");
        button.type = "button";
        button.className = "datetime-day" + (date.getUTCMonth() !== focus.getUTCMonth() ? " datetime-day--outside" : "");
        button.textContent = String(date.getUTCDate());
        button.setAttribute("aria-label", label.format(date));
        button.setAttribute("data-date", iso(date));
        button.tabIndex = iso(date) === iso(focus) ? 0 : -1;
        cell.setAttribute("aria-selected", selected && iso(date) === iso(selected) ? "true" : "false");
        if (this.today && iso(date) === iso(this.today)) {
          button.setAttribute("aria-current", "date");
        }
        if (!this.allowed(date)) {
          button.setAttribute("aria-disabled", "true");
        }
        cell.appendChild(button);
        row.appendChild(cell);
      }
      this.body.appendChild(row);
      if (new Date(start.getTime() + (week * 7 + 7) * DAY_MS).getUTCMonth() !== focus.getUTCMonth() && week >= 3) {
        break;
      }
    }
  };

  Picker.prototype.focusDay = function () {
    var button = this.body.querySelector('[data-date="' + iso(this.focusDate) + '"]');
    if (button) {
      button.focus();
    }
  };

  Picker.prototype.open = function (moveFocus) {
    this.focusDate = this.selected() || this.today || utc(2000, 0, 1);
    this.calendar.hidden = false;
    this.toggle.setAttribute("aria-expanded", "true");
    this.render();
    if (moveFocus) {
      this.focusDay();
    }
  };

  Picker.prototype.close = function (returnFocus) {
    this.calendar.hidden = true;
    this.toggle.setAttribute("aria-expanded", "false");
    if (returnFocus) {
      this.toggle.focus();
    }
  };

  Picker.prototype.pick = function (date) {
    if (!this.allowed(date)) {
      return;
    }
    this.input.value = formatBr(date);
    this.input.dispatchEvent(new Event("change", { bubbles: true }));
    this.close(true);
  };

  Picker.prototype.shift = function (date) {
    var monthChanged = date.getUTCMonth() !== this.focusDate.getUTCMonth();
    this.focusDate = date;
    if (monthChanged) {
      this.render();
    } else {
      this.body.querySelectorAll(".datetime-day").forEach(function (button) {
        button.tabIndex = button.getAttribute("data-date") === iso(date) ? 0 : -1;
      });
    }
    this.focusDay();
  };

  Picker.prototype.bind = function () {
    var self = this;
    self.toggle.addEventListener("click", function () {
      if (self.calendar.hidden) {
        self.open(true);
      } else {
        self.close(true);
      }
    });
    self.root.querySelector("[data-datetime-prev]").addEventListener("click", function () {
      self.focusDate = addMonths(self.focusDate, -1);
      self.render();
    });
    self.root.querySelector("[data-datetime-next]").addEventListener("click", function () {
      self.focusDate = addMonths(self.focusDate, 1);
      self.render();
    });
    self.body.addEventListener("click", function (event) {
      var button = event.target.closest(".datetime-day");
      if (button) {
        self.pick(fromIso(button.getAttribute("data-date")));
      }
    });
    self.body.addEventListener("keydown", function (event) {
      var date = self.focusDate;
      var moves = {
        ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7,
        Home: -date.getUTCDay(), End: 6 - date.getUTCDay()
      };
      if (Object.prototype.hasOwnProperty.call(moves, event.key)) {
        event.preventDefault();
        self.shift(new Date(date.getTime() + moves[event.key] * DAY_MS));
      } else if (event.key === "PageUp" || event.key === "PageDown") {
        event.preventDefault();
        var step = (event.key === "PageUp" ? -1 : 1) * (event.shiftKey ? 12 : 1);
        self.shift(addMonths(date, step));
      } else if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        self.pick(date);
      }
    });
    self.calendar.addEventListener("keydown", function (event) {
      if (event.key === "Escape") {
        event.preventDefault();
        self.close(true);
      }
    });
  };

  function enhance(root) {
    root.querySelectorAll("[data-datetime]:not([data-enhanced])").forEach(function (element) {
      element.setAttribute("data-enhanced", "");
      if (element.querySelector("[data-datetime-calendar]") && typeof Intl === "object") {
        element.datetimeController = new Picker(element);
      }
    });
  }

  document.addEventListener("DOMContentLoaded", function () { enhance(document); });
  document.addEventListener("htmx:load", function (event) { enhance(event.target); });
})();

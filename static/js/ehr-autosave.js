/* Server autosave for the SOAP draft (plan item 27, ADR-004, SD-8a).
   Replaces ehr-draft.js. The script only reports state: the server decides
   every outcome, and the status says "saved" only after the server has
   acknowledged the save with its revision. Clinical text is never written to
   browser storage or history state; it lives in the form fields and in the
   one request in flight.

   One autosave command per pause in typing (1.5 s). A command keeps its
   editor_command_id until the server acknowledges it: a dropped response or
   an offline period retries the same command, so the server's receipt makes
   the retry a replay and the revision moves once. States (data-state):
   saved, saving, unsaved, retrying, conflict, locked-by-other, error.
   A page restored from the back-forward cache is marked unconfirmed.
   Without JavaScript the form still posts natively. */
(function () {
  "use strict";

  var DEBOUNCE_MS = 1500;
  var RETRY_FIRST_MS = 1500;
  var RETRY_MAX_MS = 30000;
  var LOCK_RETRY_MS = 20000;
  var FIELDS = ["subjective", "objective", "assessment", "plan"];

  function newId() {
    if (window.crypto && typeof window.crypto.randomUUID === "function") {
      return window.crypto.randomUUID();
    }
    var bytes = window.crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    var hex = Array.prototype.map.call(bytes, function (b) {
      return (b + 0x100).toString(16).slice(1);
    }).join("");
    return hex.slice(0, 8) + "-" + hex.slice(8, 12) + "-" + hex.slice(12, 16) +
      "-" + hex.slice(16, 20) + "-" + hex.slice(20);
  }

  function enhance() {
    var form = document.getElementById("soap-form");
    var status = document.getElementById("save-state");
    if (!form || !status || !form.getAttribute("data-autosave") ||
        form.getAttribute("data-enhanced")) {
      return;
    }
    form.setAttribute("data-enhanced", "true");

    var url = form.getAttribute("data-autosave");
    var clinic = form.getAttribute("data-clinic");
    var csrf = form.querySelector('[name="csrfmiddlewaretoken"]');
    var versionInput = form.querySelector('[name="version_id"]');
    var revisionInput = form.querySelector('[name="revision"]');
    var sessionInput = form.querySelector('[name="editor_session"]');
    var conflictPanel = document.getElementById("conflict-panel");
    var lockPanel = document.getElementById("lock-panel");

    var debounce = null;
    var retryTimer = null;
    var lockTimer = null;
    var retryDelay = 0;
    var pending = null;      // the unacknowledged command, resent unchanged
    var inFlight = false;
    var dirty = false;       // edits not yet part of any command
    var blocked = null;      // "conflict" | "locked" | "closed" | "handed-over"
    var handoverWanted = false;
    var deferred = null;     // a submit waiting for the in-flight save
    var submitting = false;

    function text(key) {
      return status.getAttribute("data-text-" + key) || "";
    }

    function setState(state, message) {
      status.setAttribute("data-state", state);
      status.textContent = message;
      status.classList.toggle("feedback--error", state !== "saved" && state !== "saving");
      status.classList.toggle("feedback--success", state === "saved");
    }

    function revisionInputs() {
      return document.querySelectorAll('#soap-form [name="revision"], #finalize-form [name="revision"]');
    }

    function sections() {
      var result = {};
      FIELDS.forEach(function (field) {
        result[field] = form.elements[field].value;
      });
      return result;
    }

    function clearTimers() {
      window.clearTimeout(debounce);
      window.clearTimeout(retryTimer);
      window.clearTimeout(lockTimer);
    }

    function command(handover) {
      return {
        clinic_id: clinic,
        version_id: versionInput.value,
        expected_revision: Number(revisionInput.value),
        editor_command_id: newId(),
        editor_session: sessionInput.value,
        sections: sections(),
        handover: handover
      };
    }

    function flush() {
      window.clearTimeout(debounce);
      if (submitting || inFlight || blocked === "conflict" || blocked === "closed" ||
          blocked === "handed-over") {
        return;
      }
      if (!pending) {
        if (!dirty && blocked !== "locked") {
          return;
        }
        pending = command(handoverWanted);
        dirty = false;
      }
      send();
    }

    function send() {
      window.clearTimeout(retryTimer);
      inFlight = true;
      if (retryDelay === 0 && blocked !== "locked") {
        setState("saving", text("saving"));
      }
      var sent = pending;
      window.fetch(url, {
        method: "POST",
        credentials: "same-origin",
        cache: "no-store",
        headers: {
          "Accept": "application/json",
          "Content-Type": "application/json",
          "X-CSRFToken": csrf ? csrf.value : ""
        },
        body: JSON.stringify(sent)
      }).then(function (response) {
        return response.json().then(function (data) {
          return { status: response.status, data: data };
        }, function () {
          return { status: response.status, data: null };
        });
      }).then(function (reply) {
        handle(reply.status, reply.data || {}, sent);
      }, function () {
        failed(sent);
      });
    }

    function failed(sent) {
      inFlight = false;
      if (submitting || pending !== sent) {
        return;
      }
      // The save may or may not have reached the server: resend the same
      // command so the server's receipt turns a duplicate into a replay.
      setState("retrying", text("retrying"));
      retryDelay = Math.min(Math.max(retryDelay * 2, RETRY_FIRST_MS), RETRY_MAX_MS);
      retryTimer = window.setTimeout(send, retryDelay);
    }

    function acknowledged(data) {
      revisionInputs().forEach(function (input) {
        input.value = String(data.revision);
      });
      var time = String(data.saved_at || "").slice(11, 16);
      setState("saved", text("saved").replace("__TIME__", time).replace("__N__", String(data.revision)));
      if (data.lock === "handed_over") {
        blocked = "handed-over";
        FIELDS.forEach(function (field) { form.elements[field].readOnly = true; });
        showLock(text("handed-over"));
        return;
      }
      blocked = null;
      handoverWanted = false;
      if (lockPanel) {
        lockPanel.hidden = true;
      }
      if (deferred) {
        var next = deferred;
        deferred = null;
        next.form.requestSubmit(next.submitter);
        return;
      }
      if (dirty) {
        // Text typed during the save is newer than this revision.
        setState("unsaved", text("unsaved"));
        debounce = window.setTimeout(flush, DEBOUNCE_MS);
      }
    }

    function handle(code, data, sent) {
      inFlight = false;
      if (submitting || pending !== sent) {
        return;
      }
      if (code >= 500 || code === 429) {
        failed(sent);
        return;
      }
      pending = null;
      retryDelay = 0;
      if (code === 200) {
        acknowledged(data);
        return;
      }
      deferred = null;
      if (code === 409 && typeof data.current_revision === "number") {
        showConflict(data);
        return;
      }
      if (code === 423) {
        // Nothing was written; the text stays here, unsaved.
        dirty = true;
        blocked = "locked";
        showLock(data.code === "handover_requested" ? text("handover") : text("locked"));
        window.clearTimeout(lockTimer);
        lockTimer = window.setTimeout(flush, LOCK_RETRY_MS);
        return;
      }
      dirty = true;
      blocked = "closed";
      setState("error", code === 412 ? text("closed") : text("error"));
    }

    function showLock(message) {
      setState("locked-by-other", message);
      if (lockPanel) {
        lockPanel.querySelector("[data-lock-text]").textContent = message;
        lockPanel.querySelector("[data-request-handover]").hidden = blocked !== "locked";
        lockPanel.hidden = false;
      }
    }

    function element(tag, className, content) {
      var node = document.createElement(tag);
      if (className) {
        node.className = className;
      }
      if (content !== undefined) {
        node.textContent = content;
      }
      return node;
    }

    function diffFigure(section, revision) {
      var label = conflictPanel.getAttribute("data-label-" + section.section) || section.section;
      var id = "conflict-" + section.section;
      var added = 0;
      var removed = 0;
      var list = element("ol", "diff-lines");
      section.lines.forEach(function (line) {
        var item = element("li", "diff-line diff-line--" + line.kind);
        var marker = element("span", "diff-marker", line.kind === "added" ? "+" : line.kind === "removed" ? "\u2212" : " ");
        marker.setAttribute("aria-hidden", "true");
        var body = element("span");
        if (line.kind === "added" || line.kind === "removed") {
          added += line.kind === "added" ? 1 : 0;
          removed += line.kind === "removed" ? 1 : 0;
          body.appendChild(element("span", "visually-hidden",
            conflictPanel.getAttribute(line.kind === "added" ? "data-text-added" : "data-text-removed") + " "));
          body.appendChild(element(line.kind === "added" ? "ins" : "del", "", line.text));
        } else {
          body.textContent = line.text;
        }
        item.appendChild(marker);
        item.appendChild(body);
        list.appendChild(item);
      });
      var figure = element("figure", "diff");
      figure.id = id;
      figure.setAttribute("aria-labelledby", id + "-caption");
      var caption = element("figcaption", "diff-caption");
      caption.id = id + "-caption";
      caption.appendChild(element("strong", "", conflictPanel.getAttribute("data-text-title")
        .replace("__SECTION__", label).replace("__N__", String(revision))));
      caption.appendChild(element("span", "diff-summary", conflictPanel.getAttribute("data-text-summary")
        .replace("__ADDED__", String(added)).replace("__REMOVED__", String(removed))));
      figure.appendChild(caption);
      figure.appendChild(list);
      return figure;
    }

    function showConflict(data) {
      blocked = "conflict";
      dirty = true;
      var container = conflictPanel.querySelector("[data-conflict-sections]");
      container.textContent = "";
      data.diff.forEach(function (section) {
        var label = conflictPanel.getAttribute("data-label-" + section.section) || section.section;
        var wrap = element("div");
        wrap.setAttribute("data-conflict-section", section.section);
        wrap.appendChild(diffFigure(section, data.current_revision));
        var theirs = element("textarea");
        theirs.hidden = true;
        theirs.tabIndex = -1;
        theirs.setAttribute("aria-hidden", "true");
        theirs.setAttribute("data-theirs", section.section);
        theirs.value = section.theirs;
        wrap.appendChild(theirs);
        var use = element("button", "button button--secondary",
          conflictPanel.getAttribute("data-text-use-saved").replace("__SECTION__", label));
        use.type = "button";
        use.setAttribute("data-use-saved", section.section);
        wrap.appendChild(use);
        container.appendChild(wrap);
      });
      conflictPanel.querySelector("[data-merge-revision]").value = String(data.current_revision);
      conflictPanel.hidden = false;
      wireConflict();
      setState("conflict", text("conflict"));
      var heading = conflictPanel.querySelector("#conflict-title");
      if (heading) {
        heading.focus();
      }
    }

    function wireConflict() {
      if (!conflictPanel) {
        return;
      }
      conflictPanel.querySelectorAll("[data-use-saved]").forEach(function (button) {
        button.hidden = false;
      });
    }

    function merge() {
      var revision = conflictPanel.querySelector("[data-merge-revision]").value;
      revisionInputs().forEach(function (input) {
        input.value = revision;
      });
      conflictPanel.hidden = true;
      blocked = null;
      dirty = true;
      pending = null;
      flush();
    }

    form.addEventListener("input", function (event) {
      if (!event.target.name || FIELDS.indexOf(event.target.name) < 0) {
        return;
      }
      dirty = true;
      if (blocked === null && !inFlight && !pending) {
        setState("unsaved", text("unsaved"));
      }
      if (blocked === null || blocked === "locked") {
        window.clearTimeout(debounce);
        debounce = window.setTimeout(flush, DEBOUNCE_MS);
      }
    });

    document.addEventListener("click", function (event) {
      var target = event.target.closest ? event.target.closest("[data-use-saved], [data-request-handover]") : null;
      if (!target) {
        return;
      }
      if (target.hasAttribute("data-request-handover")) {
        handoverWanted = true;
        target.hidden = true;
        window.clearTimeout(lockTimer);
        flush();
        return;
      }
      var key = target.getAttribute("data-use-saved");
      var theirs = conflictPanel.querySelector('[data-theirs="' + key + '"]');
      if (theirs && form.elements[key]) {
        form.elements[key].value = theirs.value;
        dirty = true;
        form.elements[key].focus();
      }
    });

    function onSubmit(event) {
      var target = event.currentTarget;
      var submitter = event.submitter || null;
      if (submitter && submitter.value === "merge" && conflictPanel && !conflictPanel.hidden) {
        event.preventDefault();
        merge();
        return;
      }
      if (inFlight || pending || (target.id === "finalize-form" && dirty && blocked === null)) {
        // Finish the save first so the explicit action carries its revision
        // (and, for finalize, the text typed since the last save).
        event.preventDefault();
        deferred = { form: target, submitter: submitter };
        if (!inFlight) {
          flush();
        }
        return;
      }
      clearTimers();
      submitting = true;
      setState("saving", text("saving"));
    }

    form.addEventListener("submit", onSubmit);
    var finalizeForm = document.getElementById("finalize-form");
    if (finalizeForm) {
      finalizeForm.addEventListener("submit", onSubmit);
    }

    window.addEventListener("online", function () {
      if (pending && !inFlight && !submitting) {
        send();
      }
    });

    window.addEventListener("offline", function () {
      if (pending && !submitting) {
        setState("retrying", text("retrying"));
      }
    });

    window.addEventListener("pageshow", function (event) {
      if (event.persisted) {
        // The server state is unknown after a back-forward cache restore.
        submitting = false;
        inFlight = false;
        pending = null;
        dirty = true;
        setState("unsaved", text("unconfirmed"));
      }
    });

    if (conflictPanel && !conflictPanel.hidden) {
      blocked = "conflict";
      dirty = true;
      wireConflict();
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", enhance);
  } else {
    enhance();
  }
})();

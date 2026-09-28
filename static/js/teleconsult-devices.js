/* Device check reporting: speaker and network probes plus the result POST.

   Scope: `[data-device-report-panel]` inside a `[data-device-check]` panel.
   Camera and microphone come from the page's own media code, which dispatches
   `tc:devices` on the panel with `{camera, microphone}` in its state words.
   The report posts only closed result codes (the form's hidden session id in
   the body); no image, sound, device name, label or timing leaves the device.
   The speaker test plays a short tone and asks the participant; the network
   probe times three status requests. Copy comes from `data-text-*`. */
(function () {
  "use strict";

  var MEDIA = {
    ready: "ok",
    denied: "denied",
    missing: "missing",
    busy: "busy",
    unsupported: "unsupported",
    insecure: "unsupported",
    lost: "error",
    error: "error",
    idle: "not_checked",
  };
  var GOOD_MS = 400;
  var TONES = {
    ok: "badge--success",
    good: "badge--success",
    not_checked: "badge--pending",
    degraded: "badge--pending",
  };

  function enhance(panel) {
    if (panel.getAttribute("data-enhanced") === "true") {
      return;
    }
    panel.setAttribute("data-enhanced", "true");
    var host = panel.closest("[data-device-check]") || panel;
    var form = panel.querySelector("[data-device-form]");
    var url = form.getAttribute("action");
    var manual = panel.querySelector("[data-device-manual]");
    var extra = panel.querySelector("[data-device-extra]");
    var extraActions = panel.querySelector("[data-device-extra-actions]");
    var speakerBox = panel.querySelector("[data-speaker-check]");
    var speakerTest = panel.querySelector("[data-speaker-test]");
    var reportLine = panel.querySelector("[data-device-report]");
    var audioHint = panel.querySelector("[data-audio-hint]");
    var results = { camera: "not_checked", microphone: "not_checked", speaker: "not_checked", network: "not_checked" };
    var sequence = 0;

    /* JavaScript replaces the self-report with the measured results. */
    manual.hidden = true;

    function text(key) {
      return panel.getAttribute("data-text-" + key) || "";
    }

    function badge(kind, code) {
      var element = panel.querySelector('[data-result="' + kind + '"]');
      results[kind] = code;
      element.className = "badge " + (TONES[code] || "badge--error");
      element.textContent = text(kind + "-" + code);
      element.setAttribute("data-result-state", code);
    }

    function say(key, tone) {
      reportLine.className = "feedback feedback--" + tone;
      reportLine.textContent = text(key);
      reportLine.setAttribute("data-report-state", key);
      reportLine.hidden = false;
    }

    function hint() {
      var limited =
        (results.camera !== "ok" && results.camera !== "not_checked" && results.microphone === "ok") ||
        results.network === "degraded";
      audioHint.textContent = limited ? text("audio-hint") : "";
      audioHint.hidden = !limited;
    }

    function report() {
      Object.keys(results).forEach(function (name) {
        form.elements[name].value = results[name];
      });
      var mine = ++sequence;
      say("sending", "muted");
      return fetch(url, {
        method: "POST",
        body: new FormData(form),
        credentials: "same-origin",
        cache: "no-store",
        headers: { Accept: "application/json" },
      }).then(
        function (response) {
          if (mine === sequence) {
            say(response.ok ? "sent" : "failed", response.ok ? "success" : "error");
          }
        },
        function () {
          if (mine === sequence) {
            say("failed", "error");
          }
        }
      );
    }

    /* Three sequential status requests: the median round trip decides. */
    function probeNetwork() {
      if (!navigator.onLine) {
        return Promise.resolve("offline");
      }
      var times = [];
      function once() {
        var body = new FormData(form);
        body.set("action", "status");
        var started = window.performance.now();
        return fetch(url, { method: "POST", body: body, credentials: "same-origin", cache: "no-store" }).then(function (response) {
          if (!response.ok) {
            throw new Error("status " + response.status);
          }
          times.push(window.performance.now() - started);
        });
      }
      return once()
        .then(once)
        .then(once)
        .then(
          function () {
            times.sort(function (a, b) {
              return a - b;
            });
            return times[1] <= GOOD_MS ? "good" : "degraded";
          },
          function () {
            return "offline";
          }
        );
    }

    host.addEventListener("tc:devices", function (event) {
      var detail = event.detail || {};
      extra.hidden = false;
      extraActions.hidden = false;
      results.camera = MEDIA[detail.camera] || "error";
      results.microphone = MEDIA[detail.microphone] || "error";
      badge("network", "not_checked");
      probeNetwork().then(function (network) {
        badge("network", network);
        hint();
        report();
      });
    });

    speakerTest.addEventListener("click", function () {
      var Context = window.AudioContext || window.webkitAudioContext;
      if (!Context) {
        badge("speaker", "unsupported");
        report();
        return;
      }
      var context = new Context();
      var tone = context.createOscillator();
      var gain = context.createGain();
      gain.gain.value = 0.1;
      tone.frequency.value = 440;
      tone.connect(gain);
      gain.connect(context.destination);
      tone.addEventListener("ended", function () {
        context.close();
      });
      context.resume().then(function () {
        tone.start();
        tone.stop(context.currentTime + 0.8);
      });
      /* The question does not wait for the tone: the participant answers. */
      speakerBox.hidden = false;
      speakerBox.querySelector("[data-speaker-answer]").focus();
    });
    speakerBox.querySelectorAll("[data-speaker-answer]").forEach(function (button) {
      button.addEventListener("click", function () {
        speakerBox.hidden = true;
        badge("speaker", button.getAttribute("data-speaker-answer"));
        report();
        speakerTest.focus();
      });
    });
  }

  function start() {
    document.querySelectorAll("[data-device-report-panel]").forEach(enhance);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();

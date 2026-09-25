(function () {
  "use strict";

  function enhance() {
    var root = document.querySelector("[data-realtime-topic]");
    if (!root || root.dataset.realtimeEnhanced) return;
    root.dataset.realtimeEnhanced = "true";
    var source = null;
    var retry = null;
    var poll = null;
    var attempt = 0;
    var stopped = false;
    var pending = null;
    var notice = root.querySelector("[data-realtime-unavailable]");
    var kinds = ["agenda", "inbox", "queue", "messages", "ai_job"];

    function dispatch(kind) {
      document.body.dispatchEvent(new CustomEvent("rt:" + kind));
    }

    function state(value) {
      root.dataset.realtimeState = value;
      if (notice) notice.hidden = value === "connected";
      if (value === "connected") {
        window.clearInterval(poll);
        poll = null;
      } else if (!poll && root.dataset.realtimePolling === "true") {
        poll = window.setInterval(function () { dispatch("poll"); }, 30000);
      }
    }

    function disconnect() {
      if (source) source.close();
      source = null;
    }

    function backoff() {
      disconnect();
      state("offline");
      if (stopped || retry) return;
      var delay = Math.min(30000, 1000 * Math.pow(2, attempt++));
      retry = window.setTimeout(function () {
        retry = null;
        connect();
      }, delay);
    }

    function connect() {
      if (stopped) return;
      if (!window.EventSource || root.dataset.realtimeEnabled !== "true") {
        state("offline");
        return;
      }
      var csrf = root.querySelector("[name=csrfmiddlewaretoken]");
      if (!csrf) { state("offline"); return; }
      pending = new AbortController();
      fetch("/rt/stream", {
        method: "POST",
        credentials: "same-origin",
        cache: "no-store",
        headers: { "Content-Type": "application/json", "X-CSRFToken": csrf.value },
        body: JSON.stringify({ topics: [root.dataset.realtimeTopic] }),
        signal: pending.signal
      }).then(function (response) {
        if (response.status === 403) {
          stopped = true;
          state("denied");
          dispatch("poll");
          return null;
        }
        if (!response.ok) throw new Error("realtime unavailable");
        return response.json();
      }).then(function (value) {
        if (!value || stopped) return;
        if (typeof value.ticket !== "string" || !/^[A-Za-z0-9_-]{43}$/.test(value.ticket)) {
          throw new Error("invalid realtime ticket");
        }
        source = new EventSource("/rt/stream?t=" + encodeURIComponent(value.ticket));
        source.addEventListener("ready", function () {
          attempt = 0;
          state("connected");
          // Subscription ACK + reauthorization precede ready. Reconcile the gap.
          dispatch("poll");
        });
        source.onmessage = function (message) {
          var event;
          try { event = JSON.parse(message.data); }
          catch (_error) { return; }
          if (!event || typeof event !== "object" || Array.isArray(event) ||
              Object.keys(event).sort().join(",") !== "kind,topic_hash,version" ||
              kinds.indexOf(event.kind) < 0 || !Number.isSafeInteger(event.version) ||
              event.version < 0 || typeof event.topic_hash !== "string" ||
              !/^[0-9a-f]{64}$/.test(event.topic_hash)) return;
          dispatch(event.kind);
        };
        source.addEventListener("closed", function () {
          disconnect();
          state("reauthorizing");
          dispatch("poll");
          // New POST rechecks the session; native EventSource retries would reuse
          // a burned ticket. Even the ten-minute cap therefore mints a new one.
          connect();
        });
        source.onerror = backoff;
      }).catch(function (error) {
        if (error.name !== "AbortError") backoff();
      });
    }

    window.addEventListener("pagehide", function () {
      stopped = true;
      if (pending) pending.abort();
      disconnect();
      window.clearTimeout(retry);
      window.clearInterval(poll);
    });
    window.addEventListener("pageshow", function (event) {
      if (event.persisted) {
        stopped = false;
        retry = null;
        poll = null;
        connect();
      }
    });
    connect();
  }

  document.addEventListener("DOMContentLoaded", enhance);
  document.addEventListener("htmx:load", enhance);
}());

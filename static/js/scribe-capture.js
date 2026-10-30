(function () {
  "use strict";

  var SLICE_MS = 2000;
  var BUFFER_MS = 60000;
  var MAX_BYTES = 262144;

  function enhance(root) {
    if (root.dataset.enhanced) return;
    root.dataset.enhanced = "true";
    var form = root.querySelector("[data-capture-form]");
    if (!form) return;
    var device = root.querySelector("[data-capture-device]");
    var level = root.querySelector("[data-capture-level]");
    var meterUnavailable = root.querySelector("[data-capture-meter-unavailable]");
    var status = root.querySelector("[data-capture-status]");
    var indicator = root.querySelector("[data-capture-indicator]");
    var dialog = root.querySelector("[data-capture-dialog]");
    var token = form.elements.encounter_token.value;
    var session = form.elements.session_id.value;
    var epoch = form.elements.expected_epoch.value || null;
    var csrf = form.elements.csrfmiddlewaretoken.value;
    var phase = session ? "recovery" : "idle";
    var stream = null;
    var recorder = null;
    var analyser = null;
    var context = null;
    var animation = 0;
    var deadline = 0;
    var polling = 0;
    var lastSlice = 0;
    var nextSeq = 0;
    var pending = [];
    var uploading = false;
    var busy = false;
    var serverPaused = false;
    var autoPaused = false;
    var lost = 0;
    var received = 0;
    var afterDecision = null;
    var decisionOpener = null;
    var stopSignal = null;
    var mediaStopped = Promise.resolve();
    var currentMessage = session ? "recover" : "idle";
    var currentState = session ? "stale" : "default";

    function text(key) {
      return root.getAttribute("data-text-" + key);
    }

    function paint(message, state) {
      currentMessage = message;
      currentState = state;
      if (status.textContent !== text(message)) status.textContent = text(message);
      root.dataset.state = state;
      root.dataset.phase = phase;
      root.dataset.pending = String(pending.length);
      root.dataset.lost = String(lost);
      root.dataset.received = String(received);
      root.dataset.bufferedMs = String(pending.reduce(function (sum, item) {
        return sum + item.duration;
      }, 0));
      root.setAttribute("aria-busy", busy ? "true" : "false");
      var live = phase === "recording";
      indicator.hidden = !live;
      meterUnavailable.hidden = !live || (context && context.state === "running");
      document.body.toggleAttribute("data-scribe-live", live);
      device.disabled = busy || live || phase === "stopping";
      root.querySelectorAll("[data-capture-action]").forEach(function (button) {
        var action = button.dataset.captureAction;
        var visible = action === "start" ? phase === "idle" || phase === "done" :
          action === "pause" ? live :
          action === "resume" ? phase === "paused" || phase === "offline" :
          action === "recover" ? phase === "recovery" || phase === "offline" :
          Boolean(session) && phase !== "done";
        button.hidden = !visible;
        button.disabled = busy || (action === "resume" && (uploading || pending.length > 0));
      });
      root.dispatchEvent(new CustomEvent("scribe:state", {
        detail: { phase: phase, pending: pending.length, lost: lost, received: received }
      }));
    }

    function releaseMedia() {
      cancelAnimationFrame(animation);
      clearTimeout(deadline);
      animation = 0;
      deadline = 0;
      if (stream) stream.getTracks().forEach(function (track) { track.stop(); });
      stream = null;
      if (context) context.close().catch(function () { level.value = 0; });
      context = null;
      analyser = null;
      level.value = 0;
    }

    // Consent loss is a withdrawn permission, not a failure: whichever path
    // observes it paints the one permission-denied state.
    function stateFor(message) {
      return message === "revoked" ? "permission-denied" : "error";
    }

    function interrupt(message, state) {
      phase = "recovery";
      if (recorder && recorder.state !== "inactive") recorder.stop();
      releaseMedia();
      lost += pending.length;
      pending = [];
      clearInterval(polling);
      paint(message || "failed", state || stateFor(message));
    }

    // The upload refusal and the authority poll race to observe a revocation.
    // The first one stops the microphone and records the stop; a later one
    // finds the same state and changes nothing.
    async function revoke() {
      if (phase === "recovery" && currentMessage === "revoked") return;
      interrupt("revoked");
      await control("stop").catch(function () { paint("revoked", "permission-denied"); });
    }

    async function post(url, body, extra) {
      var response = await fetch(url, {
        method: "POST", credentials: "same-origin", cache: "no-store",
        headers: Object.assign({
          "Content-Type": "application/json", "X-CSRFToken": csrf,
          "Accept": "application/json"
        }, extra || {}),
        body: JSON.stringify(body), signal: AbortSignal.timeout(5000)
      });
      var result = await response.json();
      if (!response.ok) {
        let failure = new Error("capture_request_rejected");
        failure.code = result.code;
        throw failure;
      }
      return result;
    }

    async function control(action, command, reason) {
      var result = await post(root.dataset.controlUrl, {
        action: action, session_id: session || undefined,
        encounter_token: token, command_id: command || crypto.randomUUID(),
        expected_epoch: epoch, last_seq: nextSeq - 1, reason: reason || "user"
      });
      if (action === "status") return result;
      session = result.session_id;
      epoch = result.epoch;
      form.elements.session_id.value = session;
      form.elements.expected_epoch.value = epoch || "";
      return result;
    }

    function expire() {
      var now = performance.now();
      var previous = pending.length;
      pending = pending.filter(function (item) { return now < item.expires; });
      var expired = previous - pending.length;
      lost += expired;
      return expired > 0;
    }

    function armDeadline() {
      clearTimeout(deadline);
      if (!pending.length || phase !== "recording") return;
      deadline = setTimeout(function () {
        if (expire()) pauseForConnection();
        else armDeadline();
      }, Math.max(0, pending[0].expires - performance.now()));
    }

    function pauseForConnection() {
      if (autoPaused || phase !== "recording") return;
      autoPaused = true;
      phase = "offline";
      mediaStopped = stopMedia();
      releaseMedia();
      paint("offline", "offline");
      mediaStopped.then(function () {
        if (navigator.onLine) flush();
      }).catch(function () { interrupt("failed"); });
    }

    async function payload(item) {
      var bytes = new Uint8Array(await item.blob.arrayBuffer());
      var digest = new Uint8Array(await crypto.subtle.digest("SHA-256", bytes));
      var hex = Array.from(digest, function (byte) {
        return byte.toString(16).padStart(2, "0");
      }).join("");
      var binary = "";
      for (let offset = 0; offset < bytes.length; offset += 8192) {
        binary += String.fromCharCode.apply(null, bytes.subarray(offset, offset + 8192));
      }
      return {
        session_id: session, epoch: item.epoch, track: "microphone", seq: item.seq,
        sha256: hex, duration_ms: item.duration, codec: item.codec, bytes: btoa(binary)
      };
    }

    async function flush() {
      if (uploading || !session || !navigator.onLine || phase === "recovery") return;
      uploading = true;
      try {
        if (expire() && phase === "recording") pauseForConnection();
        while (pending.length) {
          let item = pending[0];
          let body = await payload(item);
          // Hashing and a stalled request consume the ORIGINAL capture TTL.
          // Never send an item that expired while it was being encoded.
          if (performance.now() >= item.expires) {
            expire();
            if (phase === "recording") pauseForConnection();
            continue;
          }
          let result;
          try {
            result = await post(root.dataset.chunkUrl, body, {
              "X-Encounter-Token": token
            });
          } catch (error) {
            if (error.code !== "chunk_expired") throw error;
            pending = pending.filter(function (queued) { return queued !== item; });
            lost += 1;
            continue;
          }
          if (phase === "done" || phase === "recovery") return;
          if (result.ack !== "received" && result.ack !== "duplicate") {
            if (result.code === "chunk_expired") {
              pending.shift();
              lost += 1;
              continue;
            }
            if (result.code === "consent_revoked") await revoke();
            else interrupt("failed");
            return;
          }
          pending = pending.filter(function (queued) { return queued !== item; });
          received += 1;
          root.dispatchEvent(new CustomEvent("scribe:receipt", {
            detail: { ack: result.ack, epoch: item.epoch, seq: item.seq }
          }));
        }
        if (autoPaused && !serverPaused) {
          await mediaStopped;
          await control("pause", null, "offline");
          serverPaused = true;
        }
        if (phase === "recording") paint("recording", "success");
        else if (phase === "offline") paint("offline", "offline");
      } catch (error) {
        if (phase === "done") return;
        if (error.code === "consent_revoked") {
          await revoke();
        } else if (error.code) {
          interrupt("failed");
        } else {
          paint(autoPaused ? "offline" : "pending", "offline");
          armDeadline();
        }
      } finally {
        uploading = false;
        if (phase !== "recovery") {
          root.dataset.pending = String(pending.length);
          root.dataset.bufferedMs = String(pending.reduce(function (sum, item) {
            return sum + item.duration;
          }, 0));
          root.querySelectorAll('[data-capture-action="resume"]').forEach(function (button) {
            button.disabled = busy || pending.length > 0;
          });
        }
        root.dispatchEvent(new CustomEvent("scribe:flush"));
      }
    }

    function meter() {
      if (!analyser || phase !== "recording") return;
      var values = new Float32Array(analyser.fftSize);
      analyser.getFloatTimeDomainData(values);
      var square = values.reduce(function (sum, value) { return sum + value * value; }, 0);
      level.value = Math.min(100, Math.sqrt(square / values.length) * 100);
      animation = requestAnimationFrame(meter);
    }

    function acceptSlice(event) {
      var now = performance.now();
      if (!event.data.size) return;
      if (phase === "offline" && autoPaused) {
        nextSeq += 1;
        lost += 1;
        lastSlice = now;
        paint("offline", "offline");
        return;
      }
      if (phase !== "recording" && phase !== "stopping") return;
      var duration = Math.round(now - lastSlice);
      var seq = nextSeq;
      nextSeq += 1;
      var capturedAt = lastSlice;
      lastSlice = now;
      if (duration < 1 || duration > 5000 || event.data.size > MAX_BYTES) {
        lost += 1;
        interrupt("failed");
        return;
      }
      if (expire()) {
        lost += 1;
        pauseForConnection();
        return;
      }
      var total = pending.reduce(function (sum, item) { return sum + item.duration; }, 0);
      if (total + duration > BUFFER_MS || pending.length >= BUFFER_MS / SLICE_MS) {
        lost += 1;
        pauseForConnection();
        return;
      }
      pending.push({
        seq: seq, epoch: epoch, blob: event.data, codec: recorder.mimeType,
        duration: duration, expires: capturedAt + BUFFER_MS
      });
      paint("recording", "success");
      armDeadline();
      flush();
    }

    function watchAuthority() {
      clearInterval(polling);
      polling = setInterval(async function () {
        if (!session || phase === "done" || phase === "recovery") return;
        try {
          let result = await control("status");
          if (result.code === "consent_revoked") {
            await revoke();
          } else if (result.code || result.epoch !== epoch ||
            (phase === "recording" && result.state !== "recording") ||
            result.state === "stopped" || result.state === "stopped_revoked") {
            interrupt(result.state === "stopped_revoked" ? "revoked" : "failed");
          }
        } catch (error) {
          if (error.code === "access_denied") interrupt("failed", "permission-denied");
          else if (phase === "recording") paint("pending", "stale");
        }
      }, 5000);
    }

    async function start(action) {
      if (phase === "done") {
        session = "";
        epoch = null;
        phase = "idle";
        lost = 0;
        received = 0;
        form.elements.command_id.value = crypto.randomUUID();
        form.elements.session_id.value = "";
        form.elements.expected_epoch.value = "";
      }
      if (!navigator.mediaDevices || !window.MediaRecorder) {
        paint("unsupported", "disabled");
        return;
      }
      var codec = ["audio/webm;codecs=opus", "audio/ogg;codecs=opus"].find(function (value) {
        return MediaRecorder.isTypeSupported(value);
      });
      if (!codec) {
        paint("unsupported", "disabled");
        return;
      }
      busy = true;
      paint("loading", "loading");
      try {
        if (!session) await control("authorize", form.elements.command_id.value);
        else {
          let authority = await control("status");
          if (authority.code) {
            let refusal = new Error("capture_authority_revoked");
            refusal.code = authority.code;
            throw refusal;
          }
        }
        stream = await navigator.mediaDevices.getUserMedia({
          audio: device.value ? { deviceId: { exact: device.value } } : true
        });
        recorder = new MediaRecorder(stream, { mimeType: codec, audioBitsPerSecond: 64000 });
        await control(action);
        nextSeq = 0;
        autoPaused = false;
        serverPaused = false;
        pending = [];
        phase = "recording";
        lastSlice = performance.now();
        recorder.addEventListener("dataavailable", acceptSlice);
        recorder.addEventListener("error", function () { interrupt("failed"); });
        recorder.addEventListener("stop", function () {
          if (stopSignal) { stopSignal(); stopSignal = null; }
        });
        stream.getAudioTracks().forEach(function (track) {
          track.addEventListener("ended", function () {
            if (phase === "recording") interrupt("failed");
          }, { once: true });
        });
        recorder.start(SLICE_MS);
        mediaStopped = Promise.resolve();
        context = new AudioContext();
        analyser = context.createAnalyser();
        context.createMediaStreamSource(stream).connect(analyser);
        context.addEventListener("statechange", function () {
          if (phase === "recording") paint(currentMessage, currentState);
        });
        // Input capture and receipts must not await the output meter's clock.
        context.resume().catch(function () {
          if (phase === "recording") paint(currentMessage, currentState);
        });
        meter();
        watchAuthority();
        paint("recording", "success");
      } catch (error) {
        releaseMedia();
        phase = session ? "recovery" : "idle";
        if (error.name === "NotAllowedError") {
          if (action === "resume") {
            phase = "paused";
            paint("denied-resume", "permission-denied");
          } else {
            if (session) await control("cancel").catch(function () { phase = "recovery"; });
            phase = "done";
            paint("denied", "permission-denied");
          }
        } else {
          let message = error.code === "consent_revoked" ? "revoked" : "failed";
          paint(message, stateFor(message));
        }
      } finally {
        busy = false;
        paint(currentMessage, currentState);
      }
    }

    async function stopMedia() {
      if (recorder && recorder.state !== "inactive") {
        await new Promise(function (resolve, reject) {
          var bound = setTimeout(function () {
            stopSignal = null;
            reject(new Error("capture_stop_unconfirmed"));
          }, 5000);
          stopSignal = function () { clearTimeout(bound); resolve(); };
          recorder.stop();
        });
      }
      releaseMedia();
    }

    async function finish(action) {
      if (!session || busy) return;
      busy = true;
      phase = "stopping";
      paint("failed", "loading");
      try {
        await stopMedia();
        await mediaStopped;
        if (action === "cancel") {
          lost += pending.length;
          pending = [];
        } else {
          // The current in-flight request is allowed to finish first. Its
          // promise, not a timer, drives the next flush.
          if (uploading) {
            await new Promise(function (resolve) {
              root.addEventListener("scribe:flush", resolve, { once: true });
            });
          }
          await flush();
        }
        // Anything still unconfirmed is a declared gap, never a spool that
        // survives pause/stop or receives a fresh lifetime on reconnection.
        lost += pending.length;
        pending = [];
        if (!(action === "pause" && serverPaused)) await control(action);
        serverPaused = action === "pause";
        phase = serverPaused ? "paused" : "done";
        if (!serverPaused) clearInterval(polling);
        paint(serverPaused ? (lost ? "paused-gap" : "paused") : action === "cancel" ? "cancelled" : "stopped",
          serverPaused ? "default" : "success");
      } catch (error) {
        interrupt(error.code === "consent_revoked" ? "revoked" : "failed");
      } finally {
        busy = false;
        paint(currentMessage, currentState);
      }
    }

    async function recover() {
      busy = true;
      try {
        if (phase === "offline") {
          await flush();
          lost += pending.length;
          pending = [];
        }
        await control("recover", null, "recovery");
        phase = "paused";
        serverPaused = true;
        lost += pending.length;
        pending = [];
        paint("paused", "default");
        watchAuthority();
      } catch (error) {
        interrupt(error.code === "consent_revoked" ? "revoked" : "failed");
      } finally {
        busy = false;
        paint(currentMessage, currentState);
      }
    }

    function decide(continuation, opener) {
      afterDecision = continuation;
      decisionOpener = opener;
      dialog.showModal();
      dialog.querySelector('[data-capture-choice="keep"]').focus();
    }

    form.addEventListener("submit", function (event) { event.preventDefault(); });
    root.addEventListener("click", function (event) {
      var button = event.target.closest("[data-capture-action]");
      if (!button || button.disabled) return;
      event.preventDefault();
      var action = button.dataset.captureAction;
      if (action === "start") start("start");
      else if (action === "resume") start("resume");
      else if (action === "recover") recover();
      else if (action === "cancel") decide(null, button);
      else finish(action);
    });
    dialog.addEventListener("click", async function (event) {
      var choice = event.target.closest("[data-capture-choice]");
      if (!choice) return;
      var action = choice.dataset.captureChoice;
      dialog.close();
      if (action !== "keep") {
        await finish(action);
        if (phase === "done" && afterDecision) afterDecision();
      }
      if (action === "keep" && decisionOpener) decisionOpener.focus();
      afterDecision = null;
    });
    dialog.addEventListener("cancel", function () {
      afterDecision = null;
      if (decisionOpener) decisionOpener.focus();
    });
    document.addEventListener("clinic:context-switch", function (event) {
      if (!session || phase === "done") return;
      event.preventDefault();
      event.detail.handled = true;
      var target = event.detail.form;
      decide(function () { window.ClinicPatientContext.request(target); },
        target.querySelector("button[type=submit]"));
    });
    window.addEventListener("click", function (event) {
      var link = event.target.closest("a[href]");
      if (!link || !session || phase === "done" || root.contains(link) ||
          link.getAttribute("href").charAt(0) === "#" || event.defaultPrevented) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      decide(function () { window.location.assign(link.href); }, link);
    }, true);
    document.addEventListener("submit", function (event) {
      var target = event.target;
      if (target === form || !session || phase === "done" || event.defaultPrevented) return;
      var action = event.submitter ? event.submitter.value : "";
      if (["open", "show", "open_unscheduled", "finalize"].indexOf(action) < 0) return;
      event.preventDefault();
      var submitter = event.submitter;
      decide(function () {
        if (submitter) target.requestSubmit(submitter);
        else target.requestSubmit();
      }, submitter);
    }, true);
    window.addEventListener("online", function () { if (session) flush(); });
    window.addEventListener("offline", function () {
      if (phase === "recording") paint("pending", "offline");
    });
    window.addEventListener("pagehide", function () {
      if (session && phase !== "done") interrupt("recover", "stale");
    });
    window.addEventListener("pageshow", function (event) {
      if (event.persisted && session) interrupt("recover", "stale");
    });
    document.addEventListener("visibilitychange", function () {
      if (document.hidden && phase === "recording") {
        finish("pause").catch(function () { interrupt("failed"); });
      }
    });
    document.addEventListener("htmx:beforeSwap", function (event) {
      if (session && phase !== "done" && event.detail.target.contains(root)) {
        interrupt("recover", "stale");
      }
    });
    if (navigator.mediaDevices) {
      navigator.mediaDevices.enumerateDevices().then(function (devices) {
        var index = 0;
        devices.filter(function (item) { return item.kind === "audioinput"; }).forEach(function (item) {
          index += 1;
          var option = document.createElement("option");
          option.value = item.deviceId;
          option.textContent = item.label || text("device").replace("__N__", String(index));
          device.appendChild(option);
        });
      }).catch(function () { device.disabled = true; });
    }
    paint(session ? "recover" : "idle", session ? "stale" : "default");
  }

  function initialize() {
    document.querySelectorAll("[data-scribe-capture]").forEach(enhance);
  }
  document.addEventListener("DOMContentLoaded", initialize);
  document.addEventListener("htmx:load", initialize);
}());

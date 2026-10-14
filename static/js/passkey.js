(function () {
  "use strict";

  function decode(value) {
    var raw = value.replace(/-/g, "+").replace(/_/g, "/");
    raw += "=".repeat((4 - raw.length % 4) % 4);
    return Uint8Array.from(atob(raw), function (character) {
      return character.charCodeAt(0);
    });
  }

  function encode(value) {
    if (value === null) return null;
    var bytes = new Uint8Array(value);
    var raw = "";
    bytes.forEach(function (byte) { raw += String.fromCharCode(byte); });
    return btoa(raw).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }

  function deviceOptions(options, registration) {
    options.challenge = decode(options.challenge);
    if (registration) options.user.id = decode(options.user.id);
    ["allowCredentials", "excludeCredentials"].forEach(function (name) {
      (options[name] || []).forEach(function (credential) {
        credential.id = decode(credential.id);
      });
    });
    return options;
  }

  function credentialPayload(credential, registration) {
    var response = credential.response;
    var result = {
      id: credential.id,
      rawId: encode(credential.rawId),
      type: credential.type,
      authenticatorAttachment: credential.authenticatorAttachment,
      response: { clientDataJSON: encode(response.clientDataJSON) }
    };
    if (registration) {
      result.response.attestationObject = encode(response.attestationObject);
      result.response.transports = response.getTransports ? response.getTransports() : [];
    } else {
      result.response.authenticatorData = encode(response.authenticatorData);
      result.response.signature = encode(response.signature);
      result.response.userHandle = encode(response.userHandle);
    }
    return result;
  }

  async function post(form, endpoint, payload, signal) {
    var response = await fetch(endpoint, {
      method: "POST",
      body: payload,
      credentials: "same-origin",
      cache: "no-store",
      signal: signal
    });
    if (!response.ok) throw new Error("passkey_denied");
    return response.json();
  }

  function enhance(form) {
    if (form.dataset.passkeyEnhanced) return;
    form.dataset.passkeyEnhanced = "true";
    var start = form.querySelector("[data-passkey-start]");
    var cancel = form.querySelector("[data-passkey-cancel]");
    var status = form.querySelector("[data-passkey-status]");
    var unavailable = form.querySelector("[data-passkey-unavailable]");
    var controller = null;
    var supported = window.isSecureContext && window.PublicKeyCredential &&
      navigator.credentials && navigator.credentials.create && navigator.credentials.get;
    if (!supported) return;
    start.hidden = false;
    unavailable.hidden = true;

    function announce(text, failure) {
      status.textContent = text;
      status.className = failure ? "feedback feedback--error" : "feedback";
      status.setAttribute("role", failure ? "alert" : "status");
      status.hidden = false;
      if (failure) status.focus();
    }

    function reset() {
      start.disabled = false;
      cancel.hidden = true;
      form.setAttribute("aria-busy", "false");
      controller = null;
    }

    cancel.addEventListener("click", function () {
      if (controller) controller.abort();
    });
    window.addEventListener("pagehide", function () {
      if (controller) controller.abort();
    });
    window.addEventListener("pageshow", function (event) {
      if (event.persisted) {
        reset();
        announce(form.dataset.textStale, true);
      }
    });
    form.addEventListener("submit", function (event) { event.preventDefault(); });
    start.addEventListener("click", async function () {
      if (controller) return;
      if (!navigator.onLine) {
        announce(form.dataset.textOffline, true);
        return;
      }
      controller = new AbortController();
      var signal = controller.signal;
      start.disabled = true;
      cancel.hidden = false;
      form.setAttribute("aria-busy", "true");
      announce(form.dataset.textLoading, false);
      try {
        var registration = form.dataset.passkey === "register";
        var payload = new FormData(form);
        if (!registration) payload.set("purpose", form.dataset.passkey);
        var options = await post(form, form.dataset.begin, payload, signal);
        var request = { publicKey: deviceOptions(options, registration), signal: signal };
        var credential = registration ?
          await navigator.credentials.create(request) :
          await navigator.credentials.get(request);
        if (!credential) throw new Error("passkey_denied");
        payload.set("credential", JSON.stringify(credentialPayload(credential, registration)));
        var result = await post(form, form.dataset.finish, payload, signal);
        window.location.assign(result.next);
      } catch (error) {
        var cancelled = error.name === "AbortError" || error.name === "NotAllowedError";
        announce(!navigator.onLine ? form.dataset.textOffline :
          cancelled ? form.dataset.textCancelled : form.dataset.textError, true);
      } finally {
        reset();
      }
    });
  }

  function enhanceAll(root) {
    if (root.matches && root.matches("[data-passkey]")) enhance(root);
    root.querySelectorAll("[data-passkey]").forEach(enhance);
  }
  document.addEventListener("DOMContentLoaded", function () { enhanceAll(document); });
  document.addEventListener("htmx:load", function (event) { enhanceAll(event.detail.elt); });
}());

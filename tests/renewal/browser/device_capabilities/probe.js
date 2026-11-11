'use strict';
// Capability probes for ADR-013. Each probe returns plain observations; the
// suite grades them. Nothing is stored, and no frame or sample leaves the page.
(() => {
  const results = {};
  // A probe that never settles (an engine that leaves a promise pending) is a
  // measured result, not a hang: the deadline is the observation.
  const PROBE_DEADLINE_MS = 15000;
  let partial = {};
  const progress = (out) => {
    partial = out;
    return out;
  };

  const once = (target, name) =>
    new Promise((resolve) => target.addEventListener(name, resolve, {once: true}));

  const wav = () => {
    const rate = 8000;
    const samples = rate;
    const bytes = new DataView(new ArrayBuffer(44 + samples));
    const text = (offset, value) =>
      [...value].forEach((char, index) => bytes.setUint8(offset + index, char.charCodeAt(0)));
    text(0, 'RIFF');
    bytes.setUint32(4, 36 + samples, true);
    text(8, 'WAVEfmt ');
    bytes.setUint32(16, 16, true);
    bytes.setUint16(20, 1, true);
    bytes.setUint16(22, 1, true);
    bytes.setUint32(24, rate, true);
    bytes.setUint32(28, rate, true);
    bytes.setUint16(32, 1, true);
    bytes.setUint16(34, 8, true);
    text(36, 'data');
    bytes.setUint32(40, samples, true);
    for (let index = 0; index < samples; index += 1) {
      bytes.setUint8(44 + index, 128 + Math.round(60 * Math.sin((2 * Math.PI * 440 * index) / rate)));
    }
    return new Blob([bytes], {type: 'audio/wav'});
  };

  const advance = async (element, ticks) => {
    const before = element.currentTime;
    for (let tick = 0; tick < ticks; tick += 1) await once(element, 'timeupdate');
    return element.currentTime > before;
  };

  const audio = async () => {
    const element = document.getElementById('tone');
    element.src = URL.createObjectURL(wav());
    const out = progress({
      media_session: 'mediaSession' in navigator,
      wake_lock: 'wakeLock' in navigator,
      visibility_before: document.visibilityState,
    });
    try {
      await element.play();
      out.playing = !element.paused;
      out.advances = await advance(element, 3);
    } catch (error) {
      out.playing = false;
      out.error = error.name;
      return out;
    }
    if (out.media_session) {
      navigator.mediaSession.metadata = new MediaMetadata({title: 'Synthetic tone'});
      navigator.mediaSession.playbackState = 'playing';
      let handler = true;
      try {
        navigator.mediaSession.setActionHandler('pause', () => {});
      } catch (error) {
        handler = false;
      }
      out.media_session_state = navigator.mediaSession.playbackState;
      out.media_session_handler = handler;
    }
    return out;
  };

  // Called after the suite sent the page to the background.
  const audioAfterBackground = async () => {
    const element = document.getElementById('tone');
    const hidden = document.visibilityState === 'hidden';
    const advances = element.paused ? false : await advance(element, 3);
    return {visibility: document.visibilityState, background_emulated: hidden, advances_in_background: advances};
  };

  const synthetic = () =>
    crypto.subtle
      .generateKey({name: 'ECDSA', namedCurve: 'P-256'}, true, ['sign'])
      .then((pair) => crypto.subtle.exportKey('raw', pair.publicKey))
      .then((raw) => new Uint8Array(raw));

  const push = async () => {
    const out = progress({
      notification_api: 'Notification' in window,
      push_manager: 'PushManager' in window,
      service_worker: 'serviceWorker' in navigator,
    });
    if (!out.service_worker) return out;
    out.permission = 'Notification' in window ? Notification.permission : 'unsupported';
    out.step = 'register';
    const registration = await navigator.serviceWorker.register('push-sw.js');
    out.step = 'ready';
    await navigator.serviceWorker.ready;
    out.registered = Boolean(registration.active);
    if (out.push_manager) {
      out.step = 'subscribe';
      try {
        const subscription = await registration.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: await synthetic(),
        });
        out.subscribed = Boolean(subscription.endpoint);
      } catch (error) {
        out.subscribed = false;
        out.subscribe_error = error.name;
      }
    }
    return out;
  };

  const notifications = async () => {
    const registration = await navigator.serviceWorker.getRegistration();
    if (!registration) return [];
    return (await registration.getNotifications()).map((item) => ({tag: item.tag, body: item.body}));
  };

  // Armed before a push is delivered; resolves once the worker showed it.
  const armPush = () => {
    if (!('serviceWorker' in navigator)) return;
    window.deviceCapabilities.pushSeen = new Promise((resolve) => {
      navigator.serviceWorker.addEventListener('message', resolve, {once: true});
    });
  };

  const camera = async () => {
    const out = progress({
      get_user_media: Boolean(navigator.mediaDevices && navigator.mediaDevices.getUserMedia),
      image_capture: 'ImageCapture' in window,
      file_capture_fallback: Boolean(document.querySelector('input[type=file][capture]')),
    });
    if (!out.get_user_media) return out;
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        video: {facingMode: 'environment', width: {ideal: 1920}, height: {ideal: 1080}},
      });
    } catch (error) {
      out.error = error.name;
      return out;
    }
    const video = document.getElementById('preview');
    video.srcObject = stream;
    await video.play();
    if (video.readyState < 2) await once(video, 'loadeddata');
    const settings = stream.getVideoTracks()[0].getSettings();
    out.width = settings.width || video.videoWidth;
    out.height = settings.height || video.videoHeight;
    const canvas = document.getElementById('frame');
    const context = canvas.getContext('2d', {willReadFrequently: true});
    context.drawImage(video, 0, 0, canvas.width, canvas.height);
    const pixels = context.getImageData(0, 0, canvas.width, canvas.height).data;
    const luma = new Float32Array(canvas.width * canvas.height);
    let sum = 0;
    for (let index = 0; index < luma.length; index += 1) {
      luma[index] = 0.299 * pixels[index * 4] + 0.587 * pixels[index * 4 + 1] + 0.114 * pixels[index * 4 + 2];
      sum += luma[index];
    }
    const mean = sum / luma.length;
    let variance = 0;
    for (let index = 0; index < luma.length; index += 1) variance += (luma[index] - mean) ** 2;
    out.luma_mean = mean;
    out.luma_variance = variance / luma.length;
    let edges = 0;
    for (let index = 1; index < luma.length; index += 1) edges += Math.abs(luma[index] - luma[index - 1]);
    out.edge_energy = edges / luma.length;
    stream.getTracks().forEach((track) => track.stop());
    video.srcObject = null;
    return out;
  };

  const flag = (bytes, mask) => (bytes[32] & mask) !== 0;

  const webauthn = async () => {
    const out = progress({public_key_credential: 'PublicKeyCredential' in window});
    if (!out.public_key_credential) return out;
    out.platform_available = await PublicKeyCredential.isUserVerifyingPlatformAuthenticatorAvailable();
    if (!out.platform_available) return out;
    const challenge = crypto.getRandomValues(new Uint8Array(32));
    try {
      const created = await navigator.credentials.create({
        publicKey: {
          rp: {id: location.hostname, name: 'Clinic Ops study'},
          user: {id: crypto.getRandomValues(new Uint8Array(16)), name: 'sintetico', displayName: 'Sintetico'},
          challenge,
          pubKeyCredParams: [{type: 'public-key', alg: -7}],
          authenticatorSelection: {
            authenticatorAttachment: 'platform',
            userVerification: 'required',
            residentKey: 'required',
          },
        },
      });
      out.created = true;
      out.attachment = created.authenticatorAttachment;
      const asserted = await navigator.credentials.get({
        publicKey: {
          rpId: location.hostname,
          challenge: crypto.getRandomValues(new Uint8Array(32)),
          userVerification: 'required',
          allowCredentials: [{type: 'public-key', id: created.rawId}],
        },
      });
      const data = new Uint8Array(asserted.response.authenticatorData);
      out.asserted = true;
      out.user_verified = flag(data, 0x04);
      out.user_present = flag(data, 0x01);
    } catch (error) {
      out.error = error.name;
    }
    return out;
  };

  const probes = {audio, push, camera, webauthn};
  window.deviceCapabilities = {results, probes, audioAfterBackground, notifications, armPush};
  for (const button of document.querySelectorAll('button[data-probe]')) {
    button.addEventListener('click', () => {
      const name = button.dataset.probe;
      const output = document.querySelector('[data-result="' + name + '"]');
      output.textContent = 'running';
      const deadline = new Promise((resolve) => {
        setTimeout(resolve, PROBE_DEADLINE_MS, 'stalled');
      });
      Promise.race([probes[name]().then(() => 'done'), deadline]).then(
        (state) => {
          results[name] = state === 'done' ? partial : {...partial, stalled: true};
          output.textContent = state;
        },
        (error) => {
          results[name] = {...partial, error: error.name};
          output.textContent = 'failed';
        },
      );
    });
  }
})();

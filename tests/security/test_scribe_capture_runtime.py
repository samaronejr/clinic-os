"""SM-I1: the shipped capture buffer drops audio 60 s after its capture time.

The browser suites drive this in Chromium. Here the shipped script runs, outside
any browser, in the Node runtime that the locked ``playwright`` wheel bundles
(every CI job syncs all dependency groups). A fake clock, stub media devices and
an offline network keep every slice in memory, so only the buffer's own expiry
can remove one.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Final

import playwright

ROOT: Final = Path(__file__).resolve().parents[2]
SCRIPT: Final = ROOT / "static" / "js" / "scribe-capture.js"
NODE: Final = Path(playwright.__file__).resolve().parent / "driver" / "node"
SLICE_MS: Final = 2000
BUFFER_MS: Final = 60000

# Minimal DOM, media and clock stand-ins; the script under test is unchanged.
HARNESS: Final = r"""
const source = require("fs").readFileSync(process.argv[2], "utf8");
let clock = 1000;
let timers = [];
let nextTimer = 1;
function setFakeTimeout(fn, delay) {
  const id = nextTimer++;
  timers.push({ id, due: clock + Math.max(0, delay || 0), fn });
  return id;
}
function clearFakeTimeout(id) { timers = timers.filter((t) => t.id !== id); }
const settle = async () => {
  for (let i = 0; i < 20; i++) await new Promise((r) => setImmediate(r));
};
async function advanceTo(target) {
  for (;;) {
    timers.sort((a, b) => a.due - b.due || a.id - b.id);
    const next = timers[0];
    if (!next || next.due > target) break;
    timers.shift();
    clock = next.due;
    next.fn();
    await settle();
  }
  clock = target;
  await settle();
}
function element(extra) {
  return Object.assign({
    dataset: {}, hidden: false, disabled: false, value: 0, textContent: "",
    attributes: {},
    getAttribute(name) { return name.startsWith("data-text-") ? name : null; },
    setAttribute(name, value) { this.attributes[name] = value; },
    addEventListener() {}, removeEventListener() {}, dispatchEvent() {},
    querySelector() { return element(); }, querySelectorAll() { return []; },
    appendChild() {}, contains() { return false; }, toggleAttribute() {},
    showModal() {}, close() {}, focus() {},
  }, extra);
}
const field = (value) => ({ value });
const form = element({
  elements: {
    encounter_token: field("token"), session_id: field(""),
    expected_epoch: field(""), csrfmiddlewaretoken: field("csrf"),
    command_id: field("00000000-0000-4000-8000-000000000001"),
  },
});
const rootListeners = {};
const root = element({
  dataset: { controlUrl: "/control", chunkUrl: "/chunk" },
  querySelector(selector) {
    return selector === "[data-capture-form]" ? form : element();
  },
  addEventListener(type, fn) { rootListeners[type] = fn; },
});
const documentListeners = {};
let recorder = null;
class FakeRecorder {
  static isTypeSupported() { return true; }
  constructor() {
    this.state = "inactive";
    this.mimeType = "audio/webm;codecs=opus";
    this.listeners = {};
  }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  emit(type, event) { (this.listeners[type] || []).forEach((fn) => fn(event)); }
  start() { this.state = "recording"; recorder = this; }
  stop() { this.state = "inactive"; this.emit("stop", {}); }
}
const track = { stop() {}, addEventListener() {} };
const stream = { getTracks: () => [track], getAudioTracks: () => [track] };
const fakeNavigator = {
  onLine: false,
  mediaDevices: {
    getUserMedia: async () => stream,
    enumerateDevices: async () => [],
  },
};
class FakeAudioContext {
  constructor() { this.state = "running"; }
  createAnalyser() { return { fftSize: 32, getFloatTimeDomainData() {} }; }
  createMediaStreamSource() { return { connect() {} }; }
  addEventListener() {}
  resume() { return Promise.resolve(); }
  close() { return Promise.resolve(); }
}
const define = (name, value) => Object.defineProperty(globalThis, name, {
  value, configurable: true, writable: true,
});
define("performance", { now: () => clock });
define("setTimeout", setFakeTimeout);
define("clearTimeout", clearFakeTimeout);
define("setInterval", () => 0);
define("clearInterval", () => {});
define("requestAnimationFrame", () => 0);
define("cancelAnimationFrame", () => {});
define("navigator", fakeNavigator);
define("MediaRecorder", FakeRecorder);
define("AudioContext", FakeAudioContext);
define("CustomEvent", class {
  constructor(type, init) { this.type = type; this.detail = init && init.detail; }
});
define("window", {
  MediaRecorder: FakeRecorder, addEventListener() {}, location: { assign() {} },
});
define("document", {
  body: element(),
  addEventListener(type, fn) { (documentListeners[type] ||= []).push(fn); },
  querySelectorAll(selector) {
    return selector === "[data-scribe-capture]" ? [root] : [];
  },
  createElement: () => element(),
});
define("fetch", async (url, init) => {
  const body = JSON.parse(init.body);
  if (url !== "/control") throw new Error("offline harness never uploads");
  return {
    ok: true,
    json: async () => ({
      session_id: "session", epoch: 1, state: "recording", action: body.action,
    }),
  };
});
const snapshot = (label) => ({
  label, at: clock, phase: root.dataset.phase, pending: Number(root.dataset.pending),
  lost: Number(root.dataset.lost), bufferedMs: Number(root.dataset.bufferedMs),
});
(async () => {
  require("vm").runInThisContext(source, { filename: "scribe-capture.js" });
  documentListeners.DOMContentLoaded.forEach((fn) => fn());
  rootListeners.click({
    target: {
      closest: () => ({ disabled: false, dataset: { captureAction: "start" } }),
    },
    preventDefault() {},
  });
  await settle();
  const started = clock;
  const slices = Number(process.argv[3]);
  const observed = [snapshot("started")];
  for (let i = 1; i <= slices; i++) {
    await advanceTo(started + i * 2000);
    recorder.emit("dataavailable", { data: { size: 512 } });
    await settle();
  }
  observed.push(snapshot("captured"));
  for (const offset of JSON.parse(process.argv[4])) {
    await advanceTo(started + offset);
    observed.push(snapshot("t+" + offset));
  }
  process.stdout.write(JSON.stringify(observed));
})().catch((error) => { console.error(error); process.exit(1); });
"""


def _run(slices: int, offsets: list[int]) -> list[dict[str, object]]:
    completed = subprocess.run(  # noqa: S603 - fixed local interpreter and input
        [str(NODE), "-", str(SCRIPT), str(slices), json.dumps(offsets)],
        input=HARNESS,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    observed: list[dict[str, object]] = json.loads(completed.stdout)
    return observed


def test_capture_buffer_drops_each_slice_sixty_seconds_after_its_capture_time() -> None:
    # Given two 2 s slices held in memory while the network is down: the first
    # captured over [t, t+2 s], the second over [t+2 s, t+4 s].
    started, captured, *later = _run(2, [BUFFER_MS - 1, BUFFER_MS, BUFFER_MS + 1999])
    assert started["phase"] == "recording"
    assert captured == {
        "label": "captured",
        "at": captured["at"],
        "phase": "recording",
        "pending": 2,
        "lost": 0,
        "bufferedMs": 2 * SLICE_MS,
    }
    # When the clock reaches 60 s after the first slice's capture START (not its
    # arrival at t+2 s), that slice leaves memory and is declared lost, and the
    # buffer pauses capture; the second slice, captured 2 s later, stays.
    just_before, at_cap, before_second_cap = later
    assert (just_before["pending"], just_before["lost"]) == (2, 0)
    assert (at_cap["pending"], at_cap["lost"], at_cap["bufferedMs"]) == (
        1,
        1,
        SLICE_MS,
    )
    assert at_cap["phase"] == "offline"
    assert before_second_cap["pending"] == 1

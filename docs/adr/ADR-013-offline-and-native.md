# ADR-013: No offline clinical mode; native companion only on a failed criterion

- Status: accepted 2026-09-24; study run 2026-11-09 (todo 69), decision provisional until EG-14
- Recorded by: todo 2; study result recorded by todo 69

## Context

Offline clinical editing would require storing clinical text or audio on the
device, which the class-3 browser-storage ban forbids (SD-8b). A native app
brings store review, a second release train and device key management. Some
needs (background audio when the screen locks, push, camera document capture,
platform authenticators) might not work well enough in mobile browsers. Todo 69
measured what emulation can measure; real devices have not been measured.

## Decision

There's no offline clinical mode. A native companion is built only if the
browser capability study fails a named criterion.

The study measures four criteria: background audio continuity when the screen
locks, push delivery, camera document capture quality and WebAuthn platform
authenticator support. Capability pages and the real-device protocol are in
`tests/renewal/browser/device_capabilities/` and
`docs/qa/real-device-protocol.md` (RD-6). The `device-capabilities` suite
runs the pages on Chromium, Firefox and WebKit through the renewal runner.

**Result (evidence level L1 emulation): no criterion failed on any engine, and
no native work is justified by this evidence.** That is not device proof. Every
criterion is `pending-EG-14`: EG-14 real-device runs have not happened, and
this ADR never counts emulation as one. Until EG-14 supplies device results the
decision stays provisional, and a failing device result reopens it.

### Study report

Each engine cell is the criterion's L1 status, with its subcheck counts.
`unmeasurable` is a subcheck emulation cannot decide; it is never counted as
`pass`. Source: `device.json` from the run at `bf880e9a` plus todo 69's
changes, one report per engine from the suite.

| Criterion | Chromium 156 | Firefox 151 | WebKit 26.5 (Playwright WPE) | Device (EG-14) |
|---|---|---|---|---|
| background-audio | pass (2 pass / 0 fail / 2 unmeasurable) | pass (2 pass / 0 fail / 2 unmeasurable) | pass (2 pass / 0 fail / 2 unmeasurable) | pending-EG-14 |
| push | pass (4 pass / 0 fail / 2 unmeasurable) | pass (2 pass / 0 fail / 4 unmeasurable) | pass (1 pass / 0 fail / 5 unmeasurable) | pending-EG-14 |
| camera-capture | pass (4 pass / 0 fail / 1 unmeasurable) | pass (2 pass / 0 fail / 3 unmeasurable) | pass (2 pass / 0 fail / 3 unmeasurable) | pending-EG-14 |
| webauthn-platform | pass (3 pass / 0 fail / 0 unmeasurable) | pass (1 pass / 0 fail / 2 unmeasurable) | pass (1 pass / 0 fail / 2 unmeasurable) | pending-EG-14 |

What emulation could not decide, on Chromium (the engine with the most
coverage):

- `background-audio` / `background_tab`: engine keeps visibilityState=visible for a background page in headless emulation.
- `background-audio` / `screen_lock`: emulation cannot lock a screen or let the OS suspend the tab; EG-14 RD-6.
- `push` / `subscribe`: no real push service reachable in emulation: AbortError.
- `push` / `delivery_to_locked_device`: needs FCM/APNs and a real device; EG-14 RD-6.
- `camera-capture` / `document_legibility`: optical quality of a printed document needs a real camera; EG-14 RD-6.

Firefox and WebKit additionally cannot be granted notification permission,
cannot be given an authenticator, and read a synthetic camera from the test
harness instead of a capture pipeline (see `tests/renewal/browser/engines.py`).
Playwright's WebKit is a WPE build without `Notification` and `PushManager`;
that gap is recorded as `unmeasurable`, not as a Safari failure. No engine
lets a background tab report `visibilityState: hidden`, and none can lock a
screen, so the lock-screen part of background audio is `unmeasurable` on all
three.

Machine-checked report (`tests/infra/test_device_capability_study.py` pins its
schema, the `pending-EG-14` device status and the L1 label):

```json study-report
{
  "schema_version": 1,
  "study": "ADR-013",
  "evidence_level": "L1-emulation",
  "device_proof": "pending-EG-14",
  "engines": [
    "chromium",
    "firefox",
    "webkit"
  ],
  "criteria": [
    {
      "id": "background-audio",
      "l1_status": "pass",
      "l1_by_engine": {
        "chromium": "pass",
        "firefox": "pass",
        "webkit": "pass"
      },
      "device_status": "pending-EG-14",
      "consequence": "If EG-14 shows playback stopping on screen lock, build a native audio capture shell; until then the PWA keeps its explicit paused state (ADR-005)."
    },
    {
      "id": "push",
      "l1_status": "pass",
      "l1_by_engine": {
        "chromium": "pass",
        "firefox": "pass",
        "webkit": "pass"
      },
      "device_status": "pending-EG-14",
      "consequence": "If EG-14 shows no lock-screen delivery (notably iOS outside an installed PWA), keep SMS/email and in-app reminders as the channel of record and propose a native push shell only for the failing platform."
    },
    {
      "id": "camera-capture",
      "l1_status": "pass",
      "l1_by_engine": {
        "chromium": "pass",
        "firefox": "pass",
        "webkit": "pass"
      },
      "device_status": "pending-EG-14",
      "consequence": "If printed 10-point text is not legible at 1280x720 on a real device, keep the file-input capture fallback and propose a native document scanner only if that fallback also fails."
    },
    {
      "id": "webauthn-platform",
      "l1_status": "pass",
      "l1_by_engine": {
        "chromium": "pass",
        "firefox": "pass",
        "webkit": "pass"
      },
      "device_status": "pending-EG-14",
      "consequence": "If a platform authenticator ceremony fails on a real device, step-up stays TOTP (todo 16 keeps both) and a native key-store shell is proposed only for that platform."
    }
  ],
  "decision": {
    "native_companion": "not-justified-by-l1",
    "offline_clinical_mode": "rejected",
    "status": "provisional-until-EG-14",
    "proposed_todos": []
  }
}
```

Schema: `schema_version`; `study`; `evidence_level` (`L1-emulation`, the only
level this ADR may carry until EG-14); `device_proof` (`pending-EG-14`);
`engines`; per criterion `id`, `l1_status` (`pass` or `fail`; `fail` if any engine
failed), `l1_by_engine`, `device_status` (`pending-EG-14` until a signed EG-14
record exists), `consequence` (what a failure triggers); and `decision`.

## Consequences

- The PWA stays online-only for clinical work, with clear offline and stale
  states instead of local queues.
- No native work starts. If a criterion fails, new todos are appended to the
  plan in the open; native work never starts silently. Todo 69 appends none,
  because nothing failed; the per-criterion consequences above say what a
  failing EG-14 result would trigger.
- Real-device evidence waits on EG-14. Emulation never substitutes for it.
- Failure path, exercised by the schema tests: a criterion that fails is
  recorded as `fail` with its consequence, and `l1_status` follows it.

## Rejected

- A speculative native app. It adds cost and risk before any measured browser
  gap exists.
- Reading L1 emulation as device proof. Playwright cannot lock a screen, reach
  a real push service or attach a real camera or authenticator.

## Revisit trigger

The first signed EG-14 real-device result for any of the four criteria.

## Owning todos

69. Consumers: 14, 50.

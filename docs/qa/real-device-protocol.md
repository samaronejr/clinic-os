# Real-device verification protocol

Status: protocol only. Nobody has run it on a real device yet. Real-device
evidence is an external artifact under EG-14 (devices supplied by the owner,
or an approved device-cloud account, which also needs EG-1 spend
authorization). Agents do not run this protocol and cannot mark it done.
Automated runs never count as real-device evidence.

## What automation covers and what it leaves out

Automated coverage is Playwright through `ops.testing.renewal_runner`:

- engines: Chromium (the CI default), Firefox and WebKit, selected with
  `CLINIC_BROWSER_ENGINE` or `--engine`;
- emulated devices: the iPhone 15 and Pixel 8 profiles in
  `tests/renewal/browser/engines.py` (viewport, pixel density, user agent,
  touch);
- accessibility: axe-core through `tests/renewal/browser/a11y_support.py`.

Emulation does not reproduce the following. Only a real device can show
them, so each one gets a procedure below:

- real permission prompts, and permissions granted or revoked in OS
  settings;
- the lock screen, and the OS suspending or throttling a backgrounded tab;
- interruptions: calls, alarms, Siri or Assistant, Bluetooth route changes;
- a real network handover (Wi-Fi to cellular) and loss of connectivity;
- hardware codecs, microphones and cameras, and iOS Safari's media and
  PWA rules.

## Device matrix

This matrix follows the plan's DV table. Record the exact OS and browser
build of every run.

| Device class | Minimum | Browser | Surfaces |
|---|---|---|---|
| iPhone (iOS Safari) | iPhone 15 class, current iOS and the release before it | Safari; installed PWA if the surface offers one | staff core (agenda view, inbox, messages), scribe capture, patient portal/PWA, teleconsult |
| Android phone (Chrome) | Pixel 8 class, current Android and the release before it | Chrome stable; installed PWA if the surface offers one | same as iPhone |

A procedure only applies once its surface exists in the build under test.
A missing surface is recorded as `not-applicable: surface absent in <sha>`,
never as `pass`.

## Preconditions

1. The build is a synthetic-mode deployment (`CLINIC_DATA_MODE=synthetic`)
   at a recorded git sha. It is never a live-data environment.
2. Accounts and patients are synthetic: names start with `Sintetico`, mail
   uses `*.invalid`. Real patient data must never appear on screen, so screen
   recordings are safe to keep.
3. Before recording, turn on Do Not Disturb and hide notification previews
   so personal notifications cannot show up in a recording.
4. Screen recording uses the OS recorder. Record audio only for the
   microphone procedures.
5. Clear the site data for the origin before each run: iOS Settings >
   Safari > Advanced > Website Data; Android Chrome > Site settings > All
   sites.

## Procedures

Each step lists the action and the expected result. A step fails when the
observed result differs in any way. In every procedure the page must stay
readable and every target at least 44x44 CSS px. The browser must never
store clinical text or audio: after the run, check Local Storage, Session
Storage, IndexedDB and Cache Storage (Safari Web Inspector or Chrome remote
debugging).

### RD-1 Microphone permission

| Step | Action | Expected |
|---|---|---|
| 1 | Start capture or join a teleconsult for the first time | The OS/browser prompt names the site. The UI explains why the microphone is needed before the prompt appears. |
| 2 | Deny | The UI shows a permission-denied state with pt-BR recovery instructions. Nothing is captured, and the app shows no fake "recording" state. |
| 3 | Grant in OS/browser settings, then return | The UI detects the grant after one explicit user action (no silent auto-start). |
| 4 | Revoke while capturing (Settings > site > Microphone) | Capture stops or pauses with a visible reason. Nothing uploaded before the revoke is lost. |

### RD-2 Lock screen during capture

| Step | Action | Expected |
|---|---|---|
| 1 | Start capture, then lock the device for 10 s | On return the UI shows the true state (paused, or continued if the platform allowed it). It never shows "recording" while the OS had suspended the capture. |
| 2 | Lock for longer than the 60 s volatile buffer (ADR-005) | Capture is explicitly paused. Chunks uploaded before the lock are intact. The UI offers resume and does not resume on its own. |
| 3 | Unlock and resume | A new capture epoch starts. The gap is visible to the clinician and is not silently filled. |

### RD-3 Background and interruption

| Step | Action | Expected |
|---|---|---|
| 1 | Switch to another app for 30 s during capture or a teleconsult | Same as RD-2 step 1. Teleconsult shows reconnecting, then recovers or ends with a clear message. |
| 2 | Take an incoming call (a second test phone) | The audio route change is handled: capture pauses with a reason, and the teleconsult shows interrupted media. After the call, resuming takes one explicit action. |
| 3 | Connect or disconnect Bluetooth headphones | The active input/output route is shown or re-selected, and capture does not continue on a dead route. |

### RD-4 Network switch

| Step | Action | Expected |
|---|---|---|
| 1 | Switch Wi-Fi to cellular during capture or a teleconsult | Uploads resume in order without duplicates. The teleconsult reconnects. The UI shows reconnecting, never a false success. |
| 2 | Turn airplane mode on for 20 s, then off | The offline state is shown within one failed request. Clinical input stays in memory only and is never written to browser storage. It is sent once after reconnection, or the loss is reported. |
| 3 | Submit a form while offline | The UI shows an error and a retry path. Submitting twice after reconnection does not create a duplicate record. |

### RD-5 Staff core and patient portal on the phone

| Step | Action | Expected |
|---|---|---|
| 1 | Sign in with TOTP, open the agenda view, inbox and messages | Tasks can be completed one-handed with touch, with no horizontal scrolling at the default text size. |
| 2 | Set the largest OS text size (Dynamic Type / Font size) | The text reflows with no overlap or clipped actions. |
| 3 | Use VoiceOver (iOS) or TalkBack (Android) on sign-in and one core task | Every control has a name, the reading order is logical, and focus returns after dialogs. The manual assistive-technology audit (EG-14) is a separate artifact by a qualified tester. |
| 4 | Install the PWA (where offered), then launch it from the home screen | It launches over HTTPS to the sign-in or home page. The service worker cache holds no clinical content. |

## Evidence template

Copy this block once per device and build. File names reference the
recordings. The template has no fields for PHI, credentials or session
identifiers.

```text
run_id:            RD-<yyyy-mm-dd>-<device-short>-<n>
date_utc:          <ISO 8601>
tester_role:       <e.g. QA contractor; qualified AT tester for RD-5.3>
authority:         EG-14 record <id>; EG-1 record <id> if device cloud
build_sha:         <40-hex git sha>
deployment:        <synthetic staging URL; CLINIC_DATA_MODE=synthetic>
device:            <model>
os_version:        <e.g. iOS 26.x / Android 16>
browser:           <name + exact version; "PWA" if installed>
network:           <Wi-Fi / cellular carrier-generation / switched>
procedures:
  - id:            RD-1.1
    result:        pass | fail | blocked | not-applicable: <reason>
    observed:      <one factual sentence>
    recording:     <file name>  sha256: <64-hex>
  - id:            RD-1.2
    ...
storage_check:     Local/Session Storage, IndexedDB, Cache Storage free of
                   clinical text/audio: yes | no (<detail>)
defects:           <tracker ids>
sign_off:          <owner or accountable reviewer, date>
```

Keep the recordings and the filled template together in the owner's
evidence store. Record their sha256 digests in the release evidence. A
failed or blocked step blocks that surface's real-device claim until it is
rerun on a fixed build.

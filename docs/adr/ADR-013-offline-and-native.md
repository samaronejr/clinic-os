# ADR-013: No offline clinical mode; native companion only on a failed criterion

- Status: accepted 2026-09-24; study result pending todo 69
- Recorded by: todo 2

## Context

Offline clinical editing would require storing clinical text or audio on the
device, which the class-3 browser-storage ban forbids (SD-8b). A native app
brings store review, a second release train and device key management. Some
needs (background audio when the screen locks, push, camera document capture,
platform authenticators) might not work well enough in mobile browsers. Nobody
has measured that yet.

## Decision

There's no offline clinical mode. A native companion is built only if the
browser capability study in todo 69 fails a named criterion.

The study measures, on emulation and on EG-14 real devices:

- background audio continuity when the screen locks;
- push delivery;
- camera document capture quality;
- WebAuthn platform authenticator support.

Results and the decision are recorded in this ADR.

## Consequences

- The PWA stays online-only for clinical work, with clear offline and stale
  states instead of local queues.
- If a criterion fails, new todos are appended to the plan in the open; native
  work never starts silently.
- Real-device evidence waits on EG-14.

## Rejected

- A speculative native app. It adds cost and risk before any measured browser
  gap exists.

## Revisit trigger

The todo 69 study result.

## Owning todos

69. Consumers: 14, 50.

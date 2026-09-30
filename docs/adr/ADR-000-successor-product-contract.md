# ADR-000: Successor product contract supersedes renewal scope bans

- Status: accepted 2026-09-24
- Recorded by: todo 2
- Implemented by: todo 1 (`docs/plans/clinic-ops-premium-successor.md`)

## Context

The renewal roadmap (`docs/plans/clinic-os-renewal-roadmap.md:56-60` at
`b39ae43`) excluded transcription and recording, automated clinical
recommendations, TISS, controlled prescriptions, full SNCR/RNDS, inventory,
native apps and advanced BI. Several app contracts and AGENTS.md files repeat
those bans. The owner specification for Clinic Ops puts all of them in scope,
so executors need one place that says which old rule still binds and which
one doesn't.

The Phase 1A plan (`docs/plans/clinic-os-phase1a-approved.md`) is frozen by a
`.sha256` sidecar, and every CI job snapshots it with the foundation SHA (H-1).
Safety, security, live-data and provenance obligations must survive any scope
change.

## Decision

The successor product contract supersedes the renewal scope bans listed in its
supersession (SD) map. It lives at
`docs/plans/clinic-ops-premium-successor.md`, and its machine ledger
`docs/plans/clinic-ops-premium-successor.supersession.json` is checked by
`tests/infra/test_successor_contract.py`.

- Historical documents keep their text and gain one pointer line to the
  successor contract.
- Class-3 rows (safety, security, live data, provenance) can only be
  PRESERVED or ADDED. The test rejects any other disposition.
- The frozen Phase 1A plan and its sidecar stay byte-identical.

## Consequences

- Product scope changes happen in one reviewed file plus its ledger, not by
  scattered edits to old plans.
- Readers of an old document see both the historical decision and the pointer
  to its successor.
- A later todo that lifts a ban cites the SD row it relies on. Todos that touch
  a class-3 obligation may only keep or strengthen it.
- The renewal roadmap stays readable as history, which keeps past release
  evidence meaningful.

## Rejected

- Editing the renewal roadmap or the frozen Phase 1A bytes in place. It would
  erase the record of what was decided and when, and changing the frozen plan
  breaks the sidecar digest checked by CI.

## Revisit trigger

An owner scope change. Any change goes through a reviewed commit that updates
the contract, the JSON ledger and the affected pointer line.

## Owning todos

1

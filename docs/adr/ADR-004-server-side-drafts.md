# ADR-004: Server-side durable drafts with compare-and-swap

- Status: accepted 2026-09-24
- Recorded by: todo 2
- Related decisions: SD-8

## Context

Clinicians lose typed text when a tab closes or a network call fails.
`apps/ehr/AGENTS.md` bans autosave today, and `static/AGENTS.md` bans browser
storage of clinical content. The successor contract (SD-8) lifts the autosave
ban but keeps the browser-storage ban as a class-3 obligation.

The EHR already keeps one draft and one current version per note with partial
unique constraints. AI suggestions (todo 42) will arrive while a clinician is
typing, so the draft model has to handle concurrent writers per section.

## Decision

Drafts are saved server-side, durably, as the clinician types.

- Each save carries the expected revision (compare-and-swap). A stale write is
  a conflict result, never a silent overwrite.
- A conflict shows a comparison so the clinician picks what to keep.
- Sections carry edit epochs so a late AI result or another tab can tell
  whether a section changed since its base.
- Nothing clinical is stored in the browser: no localStorage, sessionStorage,
  IndexedDB or service-worker cache.

## Consequences

- Acknowledged text is never lost, because the server holds every accepted
  revision.
- Each keystroke burst becomes a write, so autosave joins the performance
  harness in todo 71.
- The three-way merge rules in todo 42 build on the section epochs defined
  here.
- Offline editing isn't possible (see ADR-013).

## Rejected

- localStorage drafts. They put clinical text on shared devices and break the
  class-3 browser-storage ban.
- CRDT. It solves live co-editing, which nobody has asked for, at the cost of
  a much harder audit and finalization story.

## Revisit trigger

Real demand for multi-author, real-time co-editing of one clinical note.

## Owning todos

27. Consumer: 42.

# ADR-001: HTMX first, bounded React + TypeScript islands only if selected

- Status: accepted 2026-09-24; island selection pending todo 23
- Recorded by: todo 2
- Related decisions: D-14, SD-9

## Context

Every screen today is server-rendered with Django templates and HTMX (vendored
under `static/vendor/htmx`), with a native-form baseline that works with
JavaScript off. `static/AGENTS.md` bans npm, bundlers and JS lint, and
`DESIGN.md` owns the visual language.

Three planned surfaces carry dense client-side interaction: the multi-resource
agenda grid, the encounter view with AI review, and the staff inbox. It isn't
known yet whether HTMX plus small plain scripts can meet the interaction and
latency targets on those surfaces. Todo 23 builds the agenda day view both
ways, in the same timebox, and measures seven metrics.

## Decision

HTMX stays first. Bounded React + TypeScript islands (agenda grid, encounter
with AI review, inbox) are allowed only if the agenda vertical slice in todo 23
selects them.

If islands are selected:

- Primitives are built in-house against `DESIGN.md`. No third-party UI kit.
- Node is pinned to the current LTS by image digest, `package-lock.json` is
  committed, and installs run `npm ci --ignore-scripts`.
- The Vite build is committed under `static/islands` with a manifest, and CI
  fails on drift between source and build.
- Each surface has a flag in `ClinicConfiguration`. The HTMX version stays as
  the fallback for two releases.

If islands aren't selected, the tree keeps no Node toolchain and this ADR
records the losing variant with its measured numbers.

## Consequences

- The JS-off baseline and the existing `B agenda` suite keep passing in both
  outcomes.
- Islands run under the strict CSP of todo 10 (`script-src 'self'`), so they
  can't depend on inline scripts or eval.
- A selected island adds a Node image, a lockfile and a drift gate to CI, and
  dependency audits cover npm packages too (todo 73).
- Surfaces outside the three named ones stay HTMX. Adding an island elsewhere
  needs a new ADR.

## Rejected

- SPA rewrite. It drops the server-rendered authorization and JS-off baseline
  that every current suite relies on, for surfaces that don't need it.
- Islands everywhere. The toolchain and state duplication cost lands on simple
  forms and lists that HTMX already serves well.

## Revisit trigger

The agenda vertical slice metrics from todo 23. This ADR is updated
with the raw measurements for both variants.

## Owning todos

23 (decision and slice). Consumers: 10, 24, 42, 51.

# ADR-001: HTMX first, bounded React + TypeScript islands only if selected

- Status: accepted 2026-09-24; islands not selected by the agenda slice
  (todo 23, measured 2026-09-28)
- Recorded by: todo 2; decision by todo 23
- Related decisions: D-14, SD-9

## Context

Every screen today is server-rendered with Django templates and HTMX (vendored
under `static/vendor/htmx`), with a native-form baseline that works with
JavaScript off. `static/AGENTS.md` bans npm, bundlers and JS lint, and
`DESIGN.md` owns the visual language.

Three planned surfaces carry dense client-side interaction: the multi-resource
agenda grid, the encounter view with AI review, and the staff inbox. It wasn't
known whether HTMX plus small plain scripts could meet the interaction and
latency targets on those surfaces, so todo 23 built the agenda day view both
ways in the same timebox and measured seven criteria.

The slice is a clinic-local day with three physicians and two rooms (40
synthetic appointments), drag-to-reschedule plus a keyboard move dialog, and
realtime refetch from todo 8. Both variants render the same DOM contract, call
the same `move_appointment` service (a stale `expected_revision` refuses with
`revision_conflict`) and were measured by one suite,
`tests/renewal/browser/test_agenda_slice.py`, in one runner session:

- **A**: HTMX + a plain-JS component (`static/js/agenda-grid.js`, with the
  todo 12 resource grid and dialog scripts).
- **B**: a React 19.3 + TypeScript 7.0 island built by Vite 8.3 in
  `node:24.21.0-bookworm-slim@sha256:0e0ff40c39bc087845bfb27465a0df4ea419520094bc35842ff83dd8cbe6f9b6`
  (`npm ci --ignore-scripts`, committed lockfile, output to `static/islands/`
  with a manifest), mounted by a `{% island 'agenda' %}` tag over the server
  grid, props through `json_script`, data through the todo 10 UI API.

## Decision

HTMX stays first, and **no island is selected**: the agenda grid ships as
variant A, and `frontend/` does not exist in the tree. Measured on Chromium
(runner session both3; Firefox repeats every criterion except INP):

| Criterion | Budget | A: HTMX + plain JS | B: React + TS island |
| --- | --- | --- | --- |
| INP p75, Moto G Power profile, 4x CPU (7 runs each) | < 200 ms | 40 ms (pass) | 48 ms (pass) |
| Gzipped JS added over the agenda ledger | A < 15 KB, B < 60 KB | 4.25 KB (pass) | 69.42 KB (**fail**) |
| Keyboard-only reschedule | pass | pass, 21 keys | pass, 21 keys |
| axe violations (grid, dialog at 1280/375/320, conflict) | 0 | 0 (pass) | 0 (pass) |
| Two contexts move one appointment | 409 conflict, winner kept | pass | pass |
| `B agenda` (JS-off baseline) with the variant in the tree | green, unchanged | green (chromium, firefox) | green (chromium, firefox) |
| Strict CSP violations | 0 | 0 (pass) | 0 (pass) |
| Hand-written lines / files specific to the variant | - | 668 / 6 | 1,196 / 15, plus a 67-package lockfile and a 230 KB bundle |

The decision rule was "B only if A fails INP or concurrency, or needs more than
1.5x B's effort". A passes both, and needs 0.56x B's variant-specific lines. B
also fails its own JS budget: React 19's production runtime alone is about
60 KB gzipped, before any agenda code. Raw measurements, per-run INP values,
event-timing entries, CPU calibration and the losing build are in the todo 23
evidence (`E/slice-report.json`, `E/variant-b/`).

## Consequences

- The JS-off baseline and the existing `B agenda` suite keep passing.
- Todo 24 builds the agenda grid on variant A:
  `apps/scheduling/agenda_grid.py`, `agenda_grid_views.py`, the
  `scheduling/partials/agenda_grid.html` and `agenda_move_dialog.html`
  partials and `static/js/agenda-grid.js`.
- The `agenda-slice` browser suite stays as the regression gate for these
  criteria on the HTMX grid (INP budget on Chromium, the rest on every engine).
- No Node toolchain, lockfile, bundle, island tag or per-surface island flag
  ships. SD-9 stays conditional: a later ADR may reopen islands for the
  encounter or inbox surfaces only with a new slice that meets these budgets.
- Two HTMX rules came out of the slice: a grid that realtime refetches swaps
  with `settle:0ms` (HTMX installs the new element's triggers at settle, so a
  hint inside the default 20 ms settle was lost), and a move's outcome notice
  lives outside the refetched region, or the realtime echo of the user's own
  move erases it.

## Rejected

- The React + TypeScript island for the agenda (variant B). It met INP,
  keyboard, axe, concurrency, CSP and the JS-off suite, but it failed its JS
  budget (69.42 KB against 60 KB), needed about 1.8x A's variant-specific code
  plus a Node image, lockfile and drift gate, and would still have needed A as
  its fallback for two releases.
- SPA rewrite. It drops the server-rendered authorization and JS-off baseline
  that every current suite relies on, for surfaces that don't need it.
- Islands everywhere. The toolchain and state duplication cost lands on simple
  forms and lists that HTMX already serves well.

## Revisit trigger

Any agenda-slice criterion failing on the HTMX grid (the suite asserts each
budget), or a new surface whose own slice shows HTMX missing INP or
concurrency. A React runtime under the 60 KB budget alone does not reopen it.

## Owning todos

23 (decision and slice). Consumers: 10, 24, 42, 51.

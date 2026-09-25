---
name: Clinic OS
description: Warm-paper clinical workspace carrying the Clinic Ops navy and teal identity, with a navy dark theme and a compact density.
colors:
  brand-cyan: "#00e5d0"
  brand-teal: "#00b894"
  brand-teal-deep: "#007a87"
  brand-navy: "#0f2d3a"
  paper: "#f7f5f0"
  paper-raised: "#ffffff"
  paper-sunken: "#eeebe4"
  paper-line: "#dcd6c9"
  ink: "#0f2d3a"
  ink-soft: "#4b6470"
  ink-on-dark: "#f2f7f8"
  ink-on-dark-soft: "#b7cbd2"
  ink-on-dark-muted: "#7f98a1"
  nav-hover: "#1b3f4d"
  primary: "#007a87"
  primary-strong: "#005f6b"
  on-primary: "#ffffff"
  selection: "#def5f3"
  marker: "#00e5d0"
  focus: "#0f2d3a"
  focus-on-dark: "#00e5d0"
  success: "#0b6b52"
  success-tint: "#e6f7f1"
  success-marker: "#00b894"
  error: "#8f3f1f"
  error-tint: "#fbede6"
  warning: "#7a4d00"
  warning-tint: "#fcf0d9"
  info: "#1e5470"
  info-tint: "#e6f0f6"
  on-marker: "#0f2d3a"
  scrim: "rgb(15 45 58 / 0.55)"
  viz-1: "#007a87"
  viz-2: "#b36b00"
  viz-3: "#312a86"
  viz-4: "#c4337f"
  viz-5: "#3f6317"
  viz-6: "#69442f"
  dark-canvas: "#0b1f28"
  dark-raised: "#12303d"
  dark-sunken: "#1a3a48"
  dark-nav: "#07161d"
  dark-nav-hover: "#1a3a48"
  dark-line: "#2b4b58"
  dark-ink: "#e6f0f2"
  dark-ink-soft: "#a9c0c8"
  dark-control-border: "#8aa3ad"
  dark-primary: "#5cc8d3"
  dark-primary-strong: "#8adbe2"
  dark-on-primary: "#0b1f28"
  dark-selection: "#1b4a55"
  dark-focus: "#00e5d0"
  dark-success: "#7fd8b4"
  dark-success-tint: "#0f3a30"
  dark-error: "#f0aa8a"
  dark-error-tint: "#3a2219"
  dark-warning: "#f0c56c"
  dark-warning-tint: "#33290f"
  dark-info: "#a3cde3"
  dark-info-tint: "#15313f"
  dark-scrim: "rgb(2 10 14 / 0.72)"
  dark-viz-1: "#5cc8d3"
  dark-viz-2: "#e0913f"
  dark-viz-3: "#9a87f5"
  dark-viz-4: "#ef6f8e"
  dark-viz-5: "#d8e062"
  dark-viz-6: "#b9e8b0"
typography:
  title:
    fontFamily: "Avenir Next, Segoe UI Variable Text, Segoe UI, system-ui, Helvetica Neue, Arial, sans-serif"
    fontSize: "1.75rem"
    fontWeight: 700
    lineHeight: 1.2
    letterSpacing: "-0.01em"
  heading:
    fontFamily: "Avenir Next, Segoe UI Variable Text, Segoe UI, system-ui, Helvetica Neue, Arial, sans-serif"
    fontSize: "1.25rem"
    fontWeight: 650
    lineHeight: 1.3
  lede:
    fontFamily: "Avenir Next, Segoe UI Variable Text, Segoe UI, system-ui, Helvetica Neue, Arial, sans-serif"
    fontSize: "1.125rem"
    fontWeight: 400
    lineHeight: 1.55
  body:
    fontFamily: "Avenir Next, Segoe UI Variable Text, Segoe UI, system-ui, Helvetica Neue, Arial, sans-serif"
    fontSize: "1rem"
    fontWeight: 400
    lineHeight: 1.55
  small:
    fontFamily: "Avenir Next, Segoe UI Variable Text, Segoe UI, system-ui, Helvetica Neue, Arial, sans-serif"
    fontSize: "0.875rem"
    fontWeight: 400
    lineHeight: 1.5
  label:
    fontFamily: "Avenir Next, Segoe UI Variable Text, Segoe UI, system-ui, Helvetica Neue, Arial, sans-serif"
    fontSize: "0.875rem"
    fontWeight: 600
    lineHeight: 1.4
  micro:
    fontFamily: "Avenir Next, Segoe UI Variable Text, Segoe UI, system-ui, Helvetica Neue, Arial, sans-serif"
    fontSize: "0.75rem"
    fontWeight: 400
    fontFeature: "tnum"
rounded:
  control: "0.375rem"
  surface: "0.75rem"
  pill: "999rem"
spacing:
  "1": "0.25rem"
  "2": "0.5rem"
  "3": "0.75rem"
  "4": "1rem"
  "5": "1.5rem"
  "6": "2rem"
  "7": "2.5rem"
  "8": "3rem"
components:
  button-primary:
    backgroundColor: "{colors.primary}"
    textColor: "{colors.on-primary}"
    typography: "{typography.body}"
    rounded: "{rounded.control}"
    padding: "0.5rem 1rem"
    height: "2.75rem"
  button-primary-hover:
    backgroundColor: "{colors.primary-strong}"
    textColor: "{colors.on-primary}"
  button-secondary:
    backgroundColor: "{colors.paper-raised}"
    textColor: "{colors.primary-strong}"
    rounded: "{rounded.control}"
    padding: "0.5rem 1rem"
    height: "2.75rem"
  button-quiet:
    backgroundColor: "{colors.paper}"
    textColor: "{colors.primary-strong}"
    rounded: "{rounded.control}"
    padding: "0.5rem 0.75rem"
    height: "2.75rem"
  button-danger:
    backgroundColor: "{colors.paper-raised}"
    textColor: "{colors.error}"
    rounded: "{rounded.control}"
    padding: "0.5rem 1rem"
    height: "2.75rem"
  button-disabled:
    backgroundColor: "{colors.paper-sunken}"
    textColor: "{colors.ink-soft}"
  input:
    backgroundColor: "{colors.paper-raised}"
    textColor: "{colors.ink}"
    typography: "{typography.body}"
    rounded: "{rounded.control}"
    padding: "0.75rem"
    height: "2.75rem"
  input-disabled:
    backgroundColor: "{colors.paper-sunken}"
    textColor: "{colors.ink-soft}"
  nav:
    backgroundColor: "{colors.brand-navy}"
    textColor: "{colors.ink-on-dark-soft}"
    height: "3.5rem"
  nav-link-current:
    backgroundColor: "{colors.brand-navy}"
    textColor: "{colors.ink-on-dark}"
  badge-neutral:
    backgroundColor: "{colors.paper-sunken}"
    textColor: "{colors.ink}"
    typography: "{typography.small}"
    rounded: "{rounded.pill}"
    padding: "0.125rem 0.625rem"
  badge-success:
    backgroundColor: "{colors.success-tint}"
    textColor: "{colors.success}"
  badge-error:
    backgroundColor: "{colors.error-tint}"
    textColor: "{colors.error}"
  panel:
    backgroundColor: "{colors.paper-raised}"
    textColor: "{colors.ink}"
    rounded: "{rounded.surface}"
    padding: "1.5rem"
  notice:
    backgroundColor: "{colors.paper-raised}"
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
    padding: "0.75rem 1rem"
  notice-error:
    backgroundColor: "{colors.error-tint}"
    textColor: "{colors.error}"
  notice-success:
    backgroundColor: "{colors.success-tint}"
    textColor: "{colors.success}"
  notice-warning:
    backgroundColor: "{colors.warning-tint}"
    textColor: "{colors.warning}"
  notice-info:
    backgroundColor: "{colors.info-tint}"
    textColor: "{colors.info}"
  badge-warning:
    backgroundColor: "{colors.warning-tint}"
    textColor: "{colors.warning}"
  badge-info:
    backgroundColor: "{colors.info-tint}"
    textColor: "{colors.info}"
  state-note:
    backgroundColor: "{colors.paper-raised}"
    textColor: "{colors.ink}"
    typography: "{typography.small}"
    rounded: "{rounded.control}"
    padding: "0.75rem"
  dialog:
    backgroundColor: "{colors.paper-raised}"
    textColor: "{colors.ink}"
    rounded: "{rounded.surface}"
    padding: "1.5rem"
    width: "32rem"
  drawer:
    backgroundColor: "{colors.paper-raised}"
    textColor: "{colors.ink}"
    rounded: "{rounded.surface}"
    width: "26rem"
  combobox-option-current:
    backgroundColor: "{colors.selection}"
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
    height: "2.75rem"
  segmented-option-checked:
    backgroundColor: "{colors.paper-raised}"
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
  tab-current:
    textColor: "{colors.ink}"
    height: "2.75rem"
  provenance-ai-drafted:
    backgroundColor: "{colors.warning-tint}"
    textColor: "{colors.warning}"
    typography: "{typography.label}"
    rounded: "{rounded.control}"
  provenance-confirmed:
    backgroundColor: "{colors.success-tint}"
    textColor: "{colors.success}"
    typography: "{typography.label}"
    rounded: "{rounded.control}"
  citation:
    backgroundColor: "{colors.paper-raised}"
    textColor: "{colors.primary-strong}"
    typography: "{typography.label}"
    rounded: "{rounded.control}"
    height: "2.75rem"
  toast:
    backgroundColor: "{colors.paper-raised}"
    textColor: "{colors.ink}"
    typography: "{typography.small}"
    rounded: "{rounded.control}"
  button-primary-dark:
    backgroundColor: "{colors.dark-primary}"
    textColor: "{colors.dark-on-primary}"
    rounded: "{rounded.control}"
    height: "2.75rem"
  panel-dark:
    backgroundColor: "{colors.dark-raised}"
    textColor: "{colors.dark-ink}"
    rounded: "{rounded.surface}"
---

# Design System: Clinic OS

## Overview

**Creative North Star: "The Clinic Ledger"**

Clinic OS is a calm, trustworthy clinical workspace. Work happens on warm
paper: raised white surfaces sit on a warm off-white canvas, separated by tone
and precise one-pixel rules, never by shadow. Over that paper the Clinic Ops
identity supplies the ink: deep navy for every word, dark teal for every
action, and cyan reserved for the one thing that is current or selected. The
navigation bar is the only dark surface, a navy band the brand mark lives on,
so orientation is always visible without competing with the task below it.

The workspace is an Operate surface. Familiarity is the feature: every field,
button, status, table and panel uses the same vocabulary on every screen, every
interactive element ships with all of its states, and motion only confirms an
action. Loading is spoken in words and `aria-busy`, never in spinners.
Authentication screens stay one narrow column; workspace screens stretch to a
wide measure so agendas and registries breathe.

Version 2 (this revision) keeps every rule above and adds what a full
outpatient day needs: a navy **dark theme** for low-light rooms, a
**compact density** for desk work, status hues for **warning** and **info**,
a six-hue **data-visualization** palette, **motion tokens** for state
reveals, and a component library (dialog, drawer, combobox, date and time
picker, resource grid, command palette, announcer and toasts, tabs,
segmented control, editor shell, provenance badge, source citation, diff,
document viewer, chart) in which every component carries all thirteen SC-8
states. Theme and density are per-user preferences stored on the server
(`UserPreference`), never in the browser, and applied as `data-theme` and
`data-density` on `<html>` for signed-in pages.

**Key Characteristics:**
- Warm paper canvas with white raised work surfaces and tonal depth only
- Navy text, dark-teal actions, cyan reserved for current and selected markers
- One UI sans family, fixed rem scale, tabular numerals for data
- Every primitive carries default, focus, disabled, loading, error and success
- Plain-language loading and status; no spinners, no decorative motion
- Light by default; a navy dark theme and a compact density on request, per
  user, with the same contrast floors

## Colors

Restrained strategy: warm neutrals, one action color, and cyan as a rationed
marker; status colors are semantic only.

### Reconciliation with the Clinic Ops brand pack

The incumbent system (warm paper, restrained green-teal, rust focus) and the
user's brand pack (cyan, teal, dark teal, deep navy) disagreed on ink and
action colors. Decision, implemented in `static/css/clinic-os.css`:

- Kept from the incumbent world: the warm paper canvas (`#f7f5f0`), white
  raised surfaces, warm rule color (`#dcd6c9`), the rust error color
  (`#8f3f1f`, now paired with a tint) and the no-shadow depth model.
- Moved to the brand pack: text ink is now brand navy (`#0f2d3a`, replacing
  `#1c2b2d`), the action color is brand dark teal (`#007a87`, replacing
  `#0f6b5c`) with a derived darker hover (`#005f6b`), and the navigation
  surface is brand navy.
- Rust is no longer the focus color. Focus is navy on light surfaces and cyan
  on the navy navigation; both hold at least 9:1 against their surface and the
  2px offset keeps the ring on the surrounding surface rather than the control.
- Cyan (`#00e5d0`) and teal (`#00b894`) fail text contrast on light surfaces,
  so they never carry text or boundaries there. Cyan appears at full strength
  only as the current-item bar on navy and as a tint (`#def5f3`) for selected
  rows on light surfaces. Teal appears at full strength only inside navy
  (navigation success badge) and as a tint for success surfaces.
- Secondary text is a navy-tinted gray (`#4b6470`) so hints read as the same
  ink family as headings.

### Primary
- **Trust Teal** (`#007a87`): buttons, links, active borders, the loading
  border. White text on it reads at 5.1:1; as link text on paper it reads at
  4.7:1.
- **Deep Teal** (`#005f6b`): hover and active for teal actions, links inside
  tinted regions, secondary-button text.
- **Signal Cyan** (`#00e5d0`): the current-page bar and focus ring on the navy
  navigation only. Never text, never a boundary on light surfaces.
- **Selection Tint** (`#def5f3`): background of a selected table row or
  current item on light surfaces; text on it stays navy.

### Neutral
- **Clinic Navy** (`#0f2d3a`): all primary text, headings, labels, control
  text, focus ring on light surfaces, navigation background.
- **Navy Gray** (`#4b6470`): hints, metadata, placeholder text, control
  borders, disabled control text (5.7:1 on paper).
- **Paper** (`#f7f5f0`): page canvas and quiet-button background.
- **Raised Paper** (`#ffffff`): panels, inputs, notices, table body.
- **Sunken Paper** (`#eeebe4`): disabled controls, skeleton bars, neutral
  badges, muted table rows.
- **Rule** (`#dcd6c9`): panel, header and table rules only; never a form
  control border.
- **Light Ink** (`#f2f7f8`), **Light Ink Soft** (`#b7cbd2`) and **Light Ink
  Muted** (`#7f98a1`): text on the navy navigation (current, default and
  unavailable items); **Navy Hover** (`#1b3f4d`) is the navigation hover fill.

### Status
- **Success** (`#0b6b52` on `#e6f7f1`): verified fields, completed actions,
  success notices and badges; the brand teal `#00b894` marks success only on
  navy.
- **Error** (`#8f3f1f` on `#fbede6`): error headings, inline errors, invalid
  control borders, error notices, danger-button text.
- **Pending**: neutral badge and sunken paper with a text label; loading never
  gets its own hue.

- **Warning** (`#7a4d00` on `#fcf0d9`): stale data, conflicts waiting for a
  decision, held slots, AI drafts awaiting review. Never for errors.
- **Info** (`#1e5470` on `#e6f0f6`): offline and informational notices,
  imported provenance. Never cyan: info is a deep blue that reads on paper.

### Data visualization
Six categorical hues, assigned in order and never reused within a chart:
Trust Teal (`#007a87`), Amber (`#b36b00`), Indigo (`#312a86`), Magenta
(`#c4337f`), Moss (`#3f6317`) and Umber (`#69442f`). Each holds at least 3:1
against paper and raised paper, and every pair stays at least 12 ΔE apart
under simulated protanopia, deuteranopia and tritanopia
(`tests/renewal/test_design_tokens.py`). Series also differ by marker shape
(circle, square, triangle, diamond, inverted triangle, bar), so color is
never the only carrier. Reference ranges are a flat Success tint band with a
dashed Success edge; out-of-range points get an Error ring and are counted
in the chart's text summary.

### Dark theme
Chosen per user, never by the operating system alone. Surfaces are
navy-derived: canvas `#0b1f28`, raised `#12303d`, sunken and hover
`#1a3a48`, navigation `#07161d`, rules `#2b4b58`. Ink is `#e6f0f2` with a
soft `#a9c0c8`; control borders are `#8aa3ad`. The action color lightens to
`#5cc8d3` (hover `#8adbe2`) with navy `#0b1f28` text on filled buttons; the
status hues lighten to `#7fd8b4` success, `#f0aa8a` error, `#f0c56c` warning
and `#a3cde3` info on deep tints. Because every surface is navy, the focus
ring is cyan (`#00e5d0`) everywhere in dark, which keeps the Two-Ring Rule:
one ring per surface family. Data-viz hues shift to `#5cc8d3`, `#e0913f`,
`#9a87f5`, `#ef6f8e`, `#d8e062` and `#b9e8b0`. Navigation ink is unchanged.

### Named Rules
**The Navy Ink Rule.** Prose, labels, headings and data are always Clinic Navy
or Navy Gray. Teal, cyan, success and error colors are semantic and never
carry ordinary copy.

**The Cyan Ration Rule.** Full-strength cyan or teal appears only on the navy
navigation surface; on paper they exist as tints. Anything that must be
readable on paper uses Trust Teal or darker.

**The Two-Ring Rule.** One focus ring per surface: navy on paper, cyan on navy.
No component defines a third focus color.

**The Paired Theme Rule.** Every semantic color decided for light is decided
again for dark, and both meet the same floors: 4.5:1 for text, 3:1 for
control boundaries, focus rings and data-viz hues. A token added to one theme
without the other is a defect.

## Typography

**UI Font:** Avenir Next (with Segoe UI Variable Text, Segoe UI, system-ui,
Helvetica Neue, Arial, sans-serif)

**Character:** one workhorse sans for everything, tuned by weight rather than
by family. The previous serif body stack was retired: a clinical workspace
mixes labels, controls, data and prose on one screen, and a single family keeps
them reading as one instrument. Numerals in tables, badges and codes are
tabular.

### Hierarchy
- **Title** (700, 1.75rem, 1.2): one per page; balanced wrapping, tight
  -0.01em tracking.
- **Heading** (650, 1.25rem, 1.3): panel, section and table-caption headings.
- **Lede** (400, 1.125rem, 1.55): one introductory sentence under the title,
  in Navy Gray.
- **Body** (400, 1rem, 1.55): copy, inputs, buttons and table cells; prose
  measure at most 65ch.
- **Small** (400, 0.875rem, 1.5): hints, metadata, badges, table meta.
- **Label** (600, 0.875rem, 1.4): field labels, navigation items, status
  titles; sentence case, never uppercase tracking.
- **Micro** (400, 0.75rem, tabular): chart axis labels only, inside the SVG
  where they scale with the drawing; never for prose or controls.

### Named Rules
**The Fixed Scale Rule.** Sizes are fixed rem steps (ratio about 1.14 to
1.4), not viewport clamps; the workspace is read at a steady distance.

## Layout

All spacing derives from a 4px base (`--space-1` 0.25rem through `--space-8`
3rem). Three measures shape every page: narrow (36rem) for authentication and
other single-task forms, prose (44rem) for reading, and wide (80rem) for the
workspace. `main` uses the wide measure; a narrow form opts into
`.shell-narrow`, and gutters are 16px at 375px, 24px at 768px and 40px at
1280px. The page uses `min-height: 100dvh`, never a fixed viewport height.

Panels stack vertically with `--space-5` between them; related controls sit
`--space-2` apart, fields `--space-5` apart, and actions gather in a row at
768px and above, stacked below it. Tables live inside a horizontally
scrollable, keyboard-focusable region so that columns keep their meaning at
320px without forcing the page itself to scroll sideways. The layout stays one
column at 200% zoom.

### Density
Two densities scale spacing inside work surfaces through `--density-*`
tokens: comfortable (default) and compact. Compact moves field and panel
stacking from `--space-5` to `--space-3`, table and grid cell padding from
`--space-3` to `--space-1` block and `--space-2` inline, panel padding from
`--space-5` to `--space-4`, and notice padding from `--space-3` to
`--space-2`. Type sizes, radii and targets never change.

**The Target Floor Rule.** No density shrinks a target below 44px
(`--control-height`); compact removes air, never reach.

## Elevation & Depth

Depth is **tonal shift with structural rules**: white on paper for raised work,
sunken paper for inactive or placeholder material, navy for navigation. There
are no box shadows, glass, glow or gradients anywhere. A panel or notice is
bounded by a single 1px Rule border; a form control is bounded by a 1px Navy
Gray border; state changes move the border color (teal for loading and focus
context, error for invalid, success for verified) rather than adding layers.

The one overlay tone is the **scrim** behind a modal dialog or the command
palette: a flat navy at 55% (`rgb(15 45 58 / 0.55)`, dark `rgb(2 10 14 /
0.72)`), no blur. Docked drawers and toasts are bordered raised paper,
never lifted by shadow.

### Named Rules
**The Flat Ledger Rule.** Surfaces are flat at rest and flat in every state.
Elevation never communicates state; border color and text do.

## Shapes

Two radii: controls (inputs, buttons, notices, badges' rectangular kin) use
0.375rem, grouped surfaces (panels) use 0.75rem, and only inline badges are
pills. Rules are 1px; emphasis rules (table header, current-item bar) are 2px
to 3px. Corners never mix scales inside one component. Provenance badges and
citation chips are rectangles at the control radius, never pills, so they
never read as status badges. Dashed borders mean "not yet decided": empty
states, AI drafts awaiting confirmation, held slots and missing citations.

## Components

### Navigation
- **Surface:** navy band, at least 3.5rem tall, the Clinic Ops compact dark
  lockup (`brand/clinic-ops-logo-compact-dark.svg`, 2rem tall, `alt=""` with
  a visually hidden "Clinic Ops" wordmark that forced colors reveal in place
  of the artwork) at the left, then the clinic context, then a wrapping list
  of items with 44px targets, ending in the account name and sign-out. Below
  48rem the list takes its own row; nothing collapses behind a toggle.
- **Clinic context:** a "Clínica" label in Light Ink Soft over the clinic name
  in Light Ink at Label weight. With more than one clinic the context is a
  native `<details>` switcher: the summary adds a Small "trocar" hint, and the
  list of other clinics opens inline below 48rem and as a navy panel bordered
  in Navy Hover above it.
- **Agenda entry:** the first item, linking to today's agenda of the current
  clinic, with a `.nav-badge` carrying "hoje" and the clinic-local date so the
  day is unambiguous across time zones.
- **Default / hover:** Light Ink Soft text; hover fills Navy Hover.
- **Current:** `aria-current="page"`, Light Ink text and a 3px cyan bar along
  the bottom edge (an underline in forced colors).
- **Focus:** 2px cyan ring, 2px offset.
- **Disabled:** `role="link" aria-disabled="true"` without `href`, Light Ink
  Muted text, no pointer.
- **Loading:** `aria-busy="true"` or the HTMX request class on the item shows
  its `.nav-progress` text and a progress cursor.
- **Error / success:** a `.nav-badge` count or word on the item, error tint on
  error, brand teal with navy text on success. Badges carry words or counts,
  never a bare dot.

### Fields
- **Style:** visible label above, white control with a 1px Navy Gray border,
  0.375rem radius, 44px minimum height, hint below in Small Navy Gray, errors
  below the hint in Error.
- **Hover:** border moves to Deep Teal.
- **Focus:** navy ring; border unchanged.
- **Disabled:** sunken paper fill, Navy Gray text, Rule border, not-allowed
  cursor. Read-only keeps the border and takes the paper fill.
- **Loading:** `aria-busy="true"` on the field shows its `.field-progress`
  text and a Trust Teal border while a server check runs.
- **Error:** `aria-invalid="true"` or `.field--error`; Error border and a
  `.field-error` message that names the fix.
- **Success:** `.field--success`; Success border and a `.field-success`
  confirmation.

### Actions
- **Shape:** 0.375rem radius, 44px minimum height, 0.5rem by 1rem padding,
  body size, weight 600, wrapping text.
- **Primary:** Trust Teal fill, white text; hover and active Deep Teal; active
  translates 1px down.
- **Secondary:** white fill, Deep Teal text and Trust Teal border. **Quiet:**
  paper fill, Deep Teal text, no border. **Danger:** white fill, Error text
  and border; hover fills Error tint.
- **Focus:** navy ring at 2px offset on every variant.
- **Disabled:** sunken paper fill, Navy Gray text, Rule border, on every
  variant and even while the pointer still hovers (a held submit keeps its
  disabled look, never its hover tint).
- **Loading:** the containing form or `.actions` sets `aria-busy="true"`; the
  submit button is disabled and the adjacent `.htmx-indicator` text appears.
- **Error / success:** an `.action-outcome` message beside the action, in
  Error or Success, for actions that complete inline.

### Status
- **Notice (`.feedback`):** a titled region, 0.375rem radius, 1px border,
  white fill; error tone takes Error border, tint and text with `role="alert"`;
  success tone takes Success border, tint and text with `role="status"`; info
  keeps navy text; loading (`aria-busy`) takes a Trust Teal border and a
  progress cursor; muted tone uses sunken paper for unavailable outcomes.
- **Focus:** an error summary is `tabindex="-1"`, receives focus after an
  invalid submit, and shows the navy ring.
- **Badge (`.badge`):** pill, Small size, weight 600, tabular numerals, always
  carrying a word or count: neutral (sunken paper), success (Success tint),
  error (Error tint), pending (sunken paper with Navy Gray text), muted (paper
  with Navy Gray text and Rule border).

### Tables
- **Structure:** `.table-scroll` region (`role="region"`, labelled by the
  caption, `tabindex="0"`) around a `.table` with a visible caption in Heading
  style, header cells in Label style with a 2px Rule under them, body cells
  with 1px rules, top-aligned, `overflow-wrap: anywhere`.
- **Default:** even rows take paper. Availability ledgers instead group rows
  by physician (caption and count) and clinic-local day (`scope="rowgroup"`):
  paper day headers separate white time-range rows without zebra striping.
  Below 48rem the range stays together above its full-width retirement action;
  explicit table roles preserve semantics when rows become flex containers.
  The action's hidden context names the range, day and physician. Refused
  retirement marks only the affected row and never implies cancellation.
  The agenda is the same ledger read by time: the clinic-local range is the
  row header in tabular numerals, then patient, physician, a status badge and
  the row actions (secondary "Reagendar", quiet "Cancelar", side by side
  from 64rem, stacked in the narrower table); the week view opens one row
  group per clinic-local day, the day view none. Neither view stripes: a
  cancelled row is the only tint, `.table-row--muted` with a muted
  "Cancelada" badge and "Sem ação disponível" in place of actions. The
  patient cell wraps even mid-word so the actions never leave the screen.
- **Date bar (agenda):** the period as a Heading `<time>` ("Terça-feira,
  04/03/2031" or "Semana de … a …") with a success badge "hoje" when it
  holds today; then secondary previous/next, a quiet "Hoje", and a quiet
  Dia/Semana switch whose current item carries `aria-current="page"` on
  Selection Tint (underlined in forced colors). Every step is a state-free
  link; the bar wraps to two rows below 48rem.
- **Focus:** row actions are links or buttons with the navy ring; the scroll
  region itself shows the ring when focused.
- **Selected:** `.table-row--selected` takes Selection Tint. **Muted
  (disabled):** `.table-row--muted` takes sunken paper and Navy Gray text for
  rows that accept no action.
- **Loading:** `aria-busy="true"` on the table shows `.table-progress` text in
  the caption and renders `.skeleton` bars in sunken paper (no animation).
- **Error / success:** `.table-row--error` and `.table-row--success` tint the
  row and carry a badge in the status cell. **Empty:** `.table-empty` states
  what would appear here and how to add it; when the empty state has a next
  step, it also carries the action as a secondary button.
- **Row actions:** a row's button or link shows a short visible verb
  ("Agendar consulta") and appends the row's name in `.visually-hidden` so
  every action has a distinct accessible name when tabbing.
- **Stacked (below 48rem):** a registry whose row action must stay on screen
  on a phone (the intake patient list) turns each row into a block: the row
  header as the first line, each data cell prefixed by its column label from
  `data-label`, the action full width, rows separated by 1px rules on a white
  body. The markup carries explicit `role="table"`, `rowgroup`, `row`,
  `columnheader`, `rowheader` and `cell` so the semantics survive the display
  change; the header row becomes visually hidden, never `display: none`.
  Tables whose actions are links, or with more than three columns, keep the
  scroll region, except the agenda: its phone form is the day list (time
  first in Small Label style, the patient in Label weight, "Médico:" from
  `data-label`, the badge, then both actions on one full-width row), and the
  booking screen's offered windows become one block per window (physician,
  then day · hours).

### Panels
- **Shape:** 0.75rem radius, 1px Rule border, white fill, 1.5rem padding, last
  child without bottom margin.
- **Focus:** a panel that is a focus target (`tabindex="-1"`) shows the navy
  ring; a panel containing a focused control moves its border to Trust Teal.
- **Disabled:** `aria-disabled="true"` or `.panel--disabled`; sunken paper
  fill and Navy Gray text for a step that is not yet available.
- **Loading:** `aria-busy="true"`; Trust Teal border, progress cursor and a
  `.panel-progress` line.
- **Error / success:** `.panel--error` and `.panel--success` move the border
  and heading color to the status hue.

### Installable shell
- **Manifest:** `manifest.webmanifest` names the app "Clinic Ops", standalone
  display, paper background and navy theme color, with 192/512 "any" and 512
  maskable PNG icons plus the SVG mark, all rasterized from the brand symbol.
- **Worker:** `/sw.js` caches only exact URLs in the shell's digest-covered
  static asset list, using an anonymous-v2 cache namespace that retires the
  earlier policy. Only same-origin `/static/` GET requests with
  `credentials: "omit"` and no Authorization, Cookie or Proxy-Authorization
  header can use the cache; precache and stored requests are newly constructed
  anonymous requests without caller headers. Cookie-capable requests remain
  network-only, even when their Cookie header is hidden by the browser.
  Extra query variants and unlisted static files are not cached. Documents,
  navigations, product routes and any `no-store` or `private` runtime response
  are never stored, so offline reloads fail closed instead of replaying
  clinical content. Anonymous allowlisted static fetches still work offline.

### Component library (v2)
Every component is a template include in `templates/includes/components/`
with its signature in the include's header comment (mirrored in
`templates/AGENTS.md`), styles in `static/css/clinic-os-components.css`
loaded per page, and, where it needs one, a strict plain script in
`static/js/components/`. Every include accepts `state` with the SC-8
vocabulary: default, hover, focus, active, disabled, loading, error, success,
empty, stale, conflict, permission-denied, offline. Component-level states
speak through one **state note** (`includes/components/state.html`): an
icon, a Label-weight title and one sentence in the state's tone (error in
Error, success in Success, stale and conflict in Warning, offline in Info,
permission-denied and disabled on sunken paper, empty with a dashed Navy
Gray border). Errors use `role="alert"`; every other state `role="status"`.

- **Dialog:** native `<dialog>` opened with `showModal()` for decisions only
  (keep or discard, confirm a consequence); raised paper, surface radius,
  flat scrim, danger tone moves the border and title to Error. Focus is
  trapped by the browser and returns to the opener.
- **Drawer:** non-modal context panel, bordered raised paper; docks at the
  inline end from 64rem, reveals with the reveal motion; Escape closes and
  returns focus to its toggle. Without JavaScript it renders in flow.
- **Combobox:** ARIA 1.2 editable combobox with a listbox; the current
  option takes Selection Tint and an inset focus-colored outline; async
  search POSTs the term (never a URL) and speaks counts in a status line.
  A confirmed patient selection shows name, age and masked CPF in a Success
  line before anything binds to it.
- **Date and time picker:** DD/MM/AAAA and 24-hour HH:MM text inputs are the
  source of truth; the calendar grid is pt-BR, starts on Sunday, marks today
  in the clinic time zone with a border, the chosen day with a Trust Teal
  fill, and days outside bounds struck through.
- **Resource grid:** resources by clinic-local time in CSS grid with the ARIA
  grid pattern and one roving tab stop. Booked slots on Selection Tint, held
  slots dashed Warning, conflicts on Error tint, completed on Success tint,
  unavailable cells on sunken paper; every slot is a link, so moving is a
  dialog, never drag-only.
- **Command palette:** Ctrl+K / Cmd+K opens a modal combobox of destinations
  and actions grouped under Label headings; keys shown as `<kbd>` on paper.
- **Announcer and toast:** one polite and one assertive live region per page
  plus a toast stack at the inline end; toasts are bordered raised paper in
  their tone, stay until dismissed (no timers) and never carry clinical
  content.
- **Tabs:** Label-weight items with a 3px Trust Teal bottom rule on the
  current tab; in-page panels upgrade to the ARIA tabs pattern, server
  routes use links with `aria-current="page"`.
- **Segmented control:** native radios in a bordered track; the checked
  option is raised paper with a Trust Teal border, never color alone.
- **Editor shell:** titled sections of plain text with one save indicator
  (all saved, saving, unsaved, not saved, offline, changed elsewhere, newer
  version). A dirty section's field gets a 3px inline-start rule. The
  server decides every save; nothing is stored in the browser.
- **Provenance badge:** reported (speech glyph), observed (eye), imported
  (Info), AI draft (dashed Warning) and confirmed (Success); the kind is
  always written out.
- **Source citation:** a numbered chip linking to the supporting source span;
  the current citation takes a 2px Trust Teal border; missing, restricted
  and withdrawn sources are dashed and inert, so uncited text stays visible.
- **Diff viewer:** unified lines with +/− markers, `<ins>`/`<del>` and
  hidden "Adicionado/Removido" words on Success and Error tints.
- **Document viewer:** titled frame, page and zoom status, zoom steps of
  100/125/150%, a sunken canvas holding a raised page, and a list of region
  anchors that outline their region in Trust Teal.
- **Chart:** server-rendered SVG trend (`apps/core/charts.py`) with a text
  summary, a legend that names every series and the reference range, and a
  "Ver dados em tabela" disclosure holding the same rows as a table.

Motion: state reveals (drawer, toast) animate opacity over `--duration-2`
(160ms) and transform over `--duration-3` (240ms) with the shared ease-out;
color and border changes keep `--duration-1` (120ms). Reduced motion removes
all of it. Focus rings are never transitioned.

### Primitive showcase
A DEBUG-only route renders every primitive and component in every SC-8
state with inert, synthetic content plus stress cases (long labels, error summary, locale
wrapping) so responsive, keyboard, forced-colors and reduced-motion evidence is
captured from real CSS. It never contains a secret, a real name or a QR code.

## Do's and Don'ts

### Do:
- **Do** reference semantic tokens (`--color-primary`, `--space-4`,
  `--radius-1`) in every rule; raw values live only in `:root`.
- **Do** give every interactive primitive its default, focus, disabled,
  loading, error and success treatment before using it on a screen.
- **Do** speak loading in words with `aria-busy`; keep `role="alert"` for
  errors and `role="status"` for everything else.
- **Do** keep 44px targets, visible labels, `aria-describedby` hints and
  errors that name the recovery.
- **Do** move focus to the answer after a submit that stays on the page:
  `autofocus` plus `tabindex="-1"` on the error summary or the result status
  line (HTMX honours it after a swap, the browser on a native response).
- **Do** keep authentication and single-task forms in the narrow measure and
  workspace content in the wide measure.
- **Do** put cyan only on navy; on paper use its tint. In the dark theme every
  surface is navy, so cyan is the focus ring and marker there too.
- **Do** give every component all thirteen SC-8 states through its `state`
  argument and the shared state note before a screen uses it.
- **Do** decide every new color for both themes and prove it in
  `tests/renewal/test_design_tokens.py`.
- **Do** keep theme and density server-side (`UserPreference`); the browser
  never stores them.

### Don't:
- **Don't** add a color outside the frontmatter without updating this file
  first.
- **Don't** add box shadows, gradients, glass, glow, spinners or decorative
  animation.
- **Don't** put a kicker or eyebrow above a heading; the remaining `.eyebrow`
  lines in older templates are legacy and are removed as those screens are
  localized.
- **Don't** use cyan, teal or Rule color as text or as a form-control border on
  paper.
- **Don't** convey status with a bare colored dot; badges carry a word or a
  count.
- **Don't** nest a panel inside a panel or use a modal where an inline notice
  works.
- **Don't** shrink a target below 44px in compact density.
- **Don't** let a toast, announcement or state note carry clinical content,
  patient names or identifiers.
- **Don't** convey a chart series, diff change or provenance by color alone.

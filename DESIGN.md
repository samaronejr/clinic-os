# Clinic OS Design System

## 1. Atmosphere & Identity

Clinic OS is a calm, trustworthy clinical workspace: warm paper-like surfaces,
clear dark ink, and restrained teal actions keep attention on the task instead
of the interface. Its signature is quiet tonal separation—raised white work
areas sit on a warm off-white canvas, with precise borders and no decorative
shadows. Authentication copy is direct, reassuring, and explicit about the
next safe action.

## 2. Color

| Role | Token | Value | Usage |
| --- | --- | --- | --- |
| Text / primary | `--color-ink` | `#1c2b2d` | Headings, labels, controls, body copy |
| Text / secondary | `--color-ink-soft` | `#51686b` | Hints, supporting copy, metadata |
| Surface / canvas | `--color-surface` | `#f7f5f0` | Page background |
| Surface / raised | `--color-surface-raised` | `#ffffff` | Auth cards, inputs, notices |
| Action / primary | `--color-primary` | `#0f6b5c` | Buttons and links only |
| Action / strong | `--color-primary-strong` | `#0a4f44` | Hover, active, brand, success status |
| Focus / accent | `--color-accent` | `#c96f2f` | Focus rings and sparing interactive emphasis |
| Divider | `--color-line` | `#dcd6c9` | Surface dividers; never a form-control border |
| Error | `--color-error` | `#8f3f1f` | Error headings, text, and input borders |
| Control border | `--color-control-border` | `#51686b` | Input and button outlines |

The ink colors carry prose. Teal, rust, and error colors are semantic and are
never used as ordinary body copy. Raised surfaces separate from the canvas by
tone and a divider where structure requires one. No color outside this table
may be added without updating this section first.

## 3. Typography

| Level | Size | Weight | Line height | Usage |
| --- | --- | --- | --- | --- |
| H1 | `clamp(1.75rem, 5vw, 2rem)` | 700 | 1.2 | Page titles |
| H2 | `1.375rem` | 650 | 1.3 | Card and state headings |
| Body / large | `1.125rem` | 400 | 1.6 | Introductory copy |
| Body | `1rem` | 400 | 1.6 | Default copy and inputs |
| Body / small | `0.875rem` | 400 | 1.5 | Hints and metadata |
| Label | `0.875rem` | 600 | 1.4 | Form labels and status labels |

- Body: `"Iowan Old Style", "Palatino Linotype", "Book Antiqua", Georgia, serif`.
- Display and controls: `"Avenir Next", "Segoe UI Variable", "Segoe UI", "Helvetica Neue", sans-serif`.
- Headings and controls use the display stack; prose uses the body stack.
- Text remains selectable, resizes without clipping, and reflows at 200% zoom.

## 4. Spacing & Layout

All spacing derives from a 4px base unit.

| Token | Value | Usage |
| --- | --- | --- |
| `--space-1` | `0.25rem` | Tight inline separation |
| `--space-2` | `0.5rem` | Label-to-control, compact groups |
| `--space-3` | `0.75rem` | Control padding, related items |
| `--space-4` | `1rem` | Standard component spacing |
| `--space-5` | `1.5rem` | Card padding, grouped fields |
| `--space-6` | `2rem` | Page rhythm |
| `--space-7` | `2.5rem` | Major separation |
| `--space-8` | `3rem` | Desktop page padding |

- Radius tokens: `--radius-1: 0.375rem` (controls), `--radius-2: 0.75rem`
  (cards).
- Auth content is one readable column, at most 36rem wide; the base shell is at
  most 44rem wide.
- The page uses `min-height: 100dvh`, never a fixed viewport height.
- Mobile (375px): 16px page gutters and stacked actions. Tablet (768px): 24px
  gutters. Desktop (1280px): 40px gutters and the same focused content width.
- Layout remains one column at 200% zoom; horizontal scrolling is not required.

## 5. Components

### Auth shell and card

- **Structure:** skip link, landmark header, main heading and intro, raised form
  card, optional supporting action, footer.
- **Variants:** login, enrollment, verification, logout confirmation, showcase.
- **Spacing:** `--space-4` through `--space-7`; `--radius-2`.
- **States:** default, error summary, and success notice.
- **Accessibility:** one `h1`, logical headings, named landmarks, focused main
  target after navigation, and no secret values in page metadata.
- **Motion:** none on entry; authentication should feel stable.

### Form field

- **Structure:** visible label, control, optional hint, inline error linked with
  `aria-describedby` and `aria-invalid`.
- **Variants:** text, password, one-time code, hidden safe redirect value.
- **Spacing:** `--space-1` through `--space-3`; `--radius-1`.
- **States:** default, hover, focus-visible, error, disabled, and read-only.
- **Accessibility:** a minimum 44px control height, persistent visible label,
  autocomplete hints, and error text that does not rely on color.
- **Motion:** only color/outline transitions; disabled by reduced-motion rules.

### Action button and text link

- **Structure:** native `button` or `a`; never a clickable generic element.
- **Variants:** primary, secondary, and quiet link.
- **Spacing:** minimum 44px target; `--space-2`/`--space-4` padding.
- **States:** default, hover, active, focus-visible, disabled, HTMX loading, and
  success confirmation where the action completes inline.
- **Accessibility:** clear action-first text; disabled and busy states expose
  native state plus `aria-busy` where applicable.
- **Motion:** 120ms transform/opacity/color feedback only; no decorative motion.

### Feedback summary and status notice

- **Structure:** titled region with a concise explanation and linked field
  errors where applicable.
- **Variants:** error, success, and informational.
- **Spacing:** `--space-3`/`--space-4`; `--radius-1`.
- **States:** hidden when empty, visible after server validation, focused for a
  submitted invalid form, and `aria-live` for HTMX updates.
- **Accessibility:** role and live-region behavior match urgency; instructions
  name both the problem and recovery action.
- **Motion:** no automatic animation.

### Enrollment QR panel

- **Structure:** in-memory QR image, numbered setup instructions, named device,
  and confirmation form.
- **Variants:** live enrollment and inert showcase placeholder.
- **States:** ready, confirmation error, loading, and confirmed success.
- **Accessibility:** live QR has concise alternative text and an adjacent text
  explanation; the showcase never contains a real key or provisioning URI.
- **Motion:** none.

### Primitive state showcase

- **Structure:** labeled sections that render the primitives and each required
  state using inert data.
- **Variants:** 375px, 768px, and 1280px responsive captures.
- **States:** default, focus, error, disabled, loading, and success.
- **Accessibility:** available only in Django `DEBUG`; examples remain semantic
  and keyboard reachable, with a non-secret QR placeholder.
- **Motion:** loading state uses text and `aria-busy`, not an animated spinner.

## 6. Motion & Interaction

Interactive feedback uses a 120ms ease-out transition only for `transform`,
`opacity`, foreground color, background color, outline, and border color.
Buttons may translate by at most one pixel while active to confirm the press.
No surface or non-interactive element animates. Under
`prefers-reduced-motion: reduce`, transitions and transforms are removed.
HTMX requests preserve native form submission as the baseline and expose a
plain-language loading state; JavaScript is optional.

## 7. Depth & Surface

The depth strategy is **tonal shift with structural borders**. The warm canvas
and white raised surface provide the primary separation. One-pixel `--color-line`
borders may delimit cards, header, footer, and notices, but form controls use
`--color-control-border`. Decorative box shadows, glass, glow, and gradients are
not part of Clinic OS. Radius is functional and restrained: 6px for controls,
12px for grouped surfaces.

## 8. Accessibility Constraints & Accepted Debt

### Constraints

- Target WCAG 2.2 AA: 4.5:1 for body text, 3:1 for large text and UI boundaries.
- Every action is reachable by keyboard in DOM order with an unambiguous
  focus-visible ring. Touch targets are at least 44 by 44 CSS pixels.
- Screen-reader users receive labels, error summaries, status changes, and the
  current authentication step without encountering a secret value.
- Users with low vision can zoom to 200% and use forced-colors mode without
  losing controls, focus, errors, or reading order.
- Reduced-motion preferences remove all nonessential transitions.
- Copy minimizes memory demands: one task per screen, numbered setup steps,
  explicit recovery, and no jargon-only errors.
- Secret TOTP material exists only in the live enrollment response, is never
  written to screenshots/evidence/logs, and never appears in the showcase.

### Inclusive personas

- A keyboard-only clinic administrator must complete login, enrollment,
  verification, and logout without a pointer.
- A physician using 200% zoom must understand the current step and recover from
  an invalid or replayed code without horizontal scrolling.
- A screen-reader user must hear the page purpose, field errors, and successful
  verification in a predictable order.
- A user under time pressure must receive short instructions and a single clear
  primary action on each screen.

### Accepted debt

None. Any future accessibility or persona debt must name the affected users,
location, severity, repair, owner, and explicit acceptance before release.

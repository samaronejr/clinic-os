# Redesign audit: the 29 renewal browser suites

Status: defect list for the successor plan. Each defect is closed by the
feature todo named in its row, using the design system v2 in `DESIGN.md`
and the component library in `templates/includes/components/`. This audit
changes no screen itself; it records what the rebuilt surfaces must fix.

## Method

The audit follows the redesign-existing-projects sequence (scan, diagnose,
fix by owner, no rewrite) inside the fixed Clinic Ledger world.

- **Scan.** All 29 suites in `ops/testing/renewal_runner.py` `SUITES` ran on
  the baseline commit `e944824` with the supervised runtime (real
  middleware, `clinic_app` role, Chromium). All 29 passed. Their 1,300+
  captures at 1280, 768, 375 and 320 px, forced colors and 200% zoom were
  reviewed as per-suite contact sheets. Synthetic data only.
- **Diagnose.** Captures were read against the redesign audit categories
  (typography, color and surfaces, layout, interactivity and states,
  content, components, iconography, code quality, strategic omissions) and
  against `DESIGN.md`. A source scan backs up each visual finding
  (templates, `static/css`, form widgets).
- **Fix.** Nothing is restyled here. Every defect names the todo that
  rebuilds the surface, and the component or token that todo uses.

Severity: **P1** breaks a DESIGN.md rule or a WCAG 2.2 AA expectation;
**P2** is an inconsistency a clinician would notice in daily use; **P3** is
polish.

## What already holds

These hold on every suite and must not regress: the warm paper canvas, navy
ink and teal actions; flat 1px-rule depth with no decorative shadows; 44px
targets; worded loading (`aria-busy` plus text, no spinners); focusable
error summaries; reflow to one column at 320 px; forced-colors borders and
focus; and pt-BR copy with clinic-local times.

## Defects

| ID | Sev | Category | Where (suites / source) | Defect | Owning todo | Fix with |
|---|---|---|---|---|---|---|
| RA-01 | P1 | Layout / navigation | every staff suite at 375 and 320 px (agenda, billing, retention, staff-intake, patient-access, encounter, prescribing...) | The navy navigation wraps into three to four rows of 44px items and takes 35-45% of the first phone viewport before the page title. | 13 | Destination registry, command palette, collapsed small-screen navigation |
| RA-02 | P1 | Content / typography | encounter, teleconsult (staff), retention, consent, clinical-history, intake patient pages, auth screens; `templates/**` (27 files carry `.eyebrow`) | Kicker/eyebrow lines ("REGISTRO CLÍNICO", "CONFORMIDADE", "CLÍNICA", "PORTAL DO PACIENTE") above headings, banned by DESIGN.md; `clinic-os-auth.css` still styles them with uppercase tracking. | 13 (staff shell and headers), 50 (patient portal pages), 15 (auth screens) | Remove the kicker; the heading carries the context |
| RA-03 | P1 | Interactivity / locale | agenda booking and reschedule, waitlist, staff-intake search and registration, self-booking; `apps/scheduling/appointment_forms.py:82`, `apps/scheduling/waitlist_views.py:45-48`, `apps/scheduling/forms.py:61`, `apps/intake/forms.py:62,201`, `templates/scheduling/patient_booking.html:37` | Native `date`/`datetime-local` inputs render in the browser's locale ("mm/dd/yyyy, --:-- --", "09:00 AM" in the runner), contradicting DD/MM/AAAA and the 24-hour clock, and hide the clinic zone inside a hint. | 24 (staff agenda and booking), 21 (availability), 17 (patient registration dates), 50 (self-booking) | `includes/components/datetime.html` (pt-BR text inputs, clinic zone, calendar grid) |
| RA-04 | P2 | Content | agenda, availability, booking tables, prescribing, teleconsult; physician columns | Professionals appear as login usernames ("dr-maximiliano-de-albuquerque-...-3841", "dra-ana-sintetica-9fbc") instead of a display name and council registration, which breaks table rhythm and wraps across four lines. | 21 (professional directory), 24 (agenda), 34 (prescriber header) | Display name + council ID; resource grid column headers |
| RA-05 | P2 | Layout | agenda booking and cancel, encounter, prescribing, retention, teleconsult, staff-intake at 1280 px | Workspace tasks use the 36-44rem narrow measure centered in a 1280 px screen: the encounter is a single thin column with 60% empty canvas, and retention forms stack a long column that needs heavy scrolling. | 24 (booking), 27 and 42 (encounter: sections plus review panel), 26 (retention/operations), 25 (reception Today) | Wide measure with a drawer or split layout; editor shell |
| RA-06 | P2 | Components | encounter (`templates/ehr/encounter.html`), prescribing | Patient-context navigation ("Problemas e alergias", "Anexos", "Prescrição sintética", "Teleconsulta") is a vertical stack of secondary buttons. It reads as four equal actions rather than places, and repeats on every encounter page. | 28 (patient workspace tabs), 13 (patient banner) | `includes/components/tabs.html` (links mode) plus the patient banner |
| RA-07 | P2 | Interactivity / states | encounter draft, prescribing draft, clinic settings | Save state is a static line ("Versão salva carregada. Novas alterações precisam ser salvas.") and a failed save becomes an error box inside the form. There is no unsaved, offline or changed-elsewhere state, and no per-section indicator. | 27 | `includes/components/editor.html` save indicator, `state.html` conflict with a compare action, `diff.html` |
| RA-08 | P2 | Color and surfaces | clinic settings; `static/css/clinic-os-settings.css:9,10,13`, `static/css/clinic-os-intake.css:222` | Raw hex outside `:root` (`#fff`, `#0f2d3a`, `#115e59`, and a `#1f6b3a` fallback) bypasses the tokens; `clinic-brand--teal` is a color the system does not define, and none of these follow the dark theme. | 15 (clinic onboarding and branding settings) | Semantic tokens (`--color-surface-raised`, `--color-ink`, `--color-primary-strong`) |
| RA-09 | P2 | Components / tables | patient-access at 768 px, billing list, retention matrix | Tables with five or more columns clip their row action ("Revogar a...") at 768 px inside the scroll region; billing lists ten identical "Abrir" buttons with no visible distinction; retention lists raw record IDs (UUIDs) as primary text. | 19 (patient access and grants), 56 (finance lists), 26 (retention/operations) | Stacked table pattern or a detail drawer; visible row labels |
| RA-10 | P2 | States | billing charge detail, prescribing provider outcome, reminders | Stale and waiting states are only a manual "Atualizar estado" button; the screen never says when it last refreshed or that newer data may exist. | 8 (realtime refetch), 57 (payments), 35 (signing), 51 (messages) | `state.html` stale state plus announcer on refetch |
| RA-11 | P2 | Components | teleconsult (patient and physician), video-recovery, patient-video | The video area is a large sunken box with one line of text; device check, reconnecting and audio-only are paragraphs in bordered boxes, and the physician room stacks video, notes and actions in two uneven columns. | 37 | Drawer for notes, state notes for device and connection states, segmented control for audio or video |
| RA-12 | P2 | Accessibility | patient-access forced colors (1280 px) | When focused, the skip link overlaps the brand lockup and clinic name instead of pushing content; readable but it covers the orientation area. | 13 | Shell skip-link placement |
| RA-13 | P2 | Content | every suite footer; page titles | The footer and titles say "Clinic OS · Tecnologia a serviço da medicina" while the wordmark says "Clinic Ops"; the footer is a leftover "foundation" line that neither links to legal pages nor to help. | 70 (brand and help surfaces), 78 (support), after the renaming decision in PRODUCT.md | Footer with help, privacy and terms links |
| RA-14 | P3 | Typography | billing charge detail, prescribing evidence, retention | Machine values (SHA-256 digests, references, UUIDs) are the largest text in their panels and wrap mid-token; they compete with the state the user came to check. | 35, 56, 26 | Small tabular meta line, copy action, provenance and state first |
| RA-15 | P3 | Components | questionnaires, consent, waitlist, reminders | Status is carried by paragraphs in tinted boxes where a badge plus one state note would do. The same outcome is phrased differently across suites ("Nada foi removido", "Nenhum aviso foi enviado", "nada foi emitido"). | 29 (questionnaires), 20 (consent), 24 (waitlist), 51 (reminders) | `state.html` shared wording; badges with words |
| RA-16 | P3 | Layout / density | agenda week, billing list, patient registry | Every list uses the comfortable rhythm; desk staff cannot see a full morning or a day's charges without scrolling. | 24, 56, 17 | `data-density="compact"` from `UserPreference` (this todo) applied to those tables |
| RA-17 | P3 | Strategic omission | all staff suites | There is no dark theme for low-light rooms (consultation, video) and no way to choose density. | 12 (delivered: `UserPreference`, `/account/preferences/`); navigation entry in 13 | Settings destination links to Display preferences |
| RA-18 | P3 | Components | agenda day and week | The agenda is a table of rows, not resources by time. Conflicts, holds and room or equipment availability have no visual form beyond a badge. | 24 | `includes/components/resource_grid.html` |
| RA-19 | P3 | Data display | clinical-history, results (future), billing summaries | There is no chart or trend anywhere; values over time are lists. | 28 (vitals and observations), 31 (results), 63 (reports) | `includes/components/chart.html` with its table alternative |

## Not defects (checked and kept)

- Flat depth: the only `box-shadow` in `static/css` is the inset
  current-item bar on the navigation (`clinic-os.css`), which DESIGN.md
  documents; it is a 3px rule, not elevation.
- Loading: every busy state across the 29 suites is worded, and none spins.
- Error recovery: error summaries are focused and link to the fields
  (staff-intake, availability, billing).
- Synthetic disclosure: every provider-backed surface keeps its
  "ensaio sintético" label.

## Evidence

The baseline runs and contact sheets are in the task evidence directory
`task-12/audit/` of the successor plan (L1, local): one runner receipt per
suite plus `sheets/<suite>.png`. The component library states that fix these
defects are captured in `task-12/showcase/`.

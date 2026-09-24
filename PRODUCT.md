# Product

<!-- impeccable:product-schema 1 -->

<!--
Provenance: written by plan task 7 (clinic-os-renewal) from the approved plan
(.omo/plans/clinic-os-renewal.md), the repository, and the user-supplied brand
pack (Clinic_Ops_SVG/ in the main checkout). No interactive product interview
was possible in the execution session; facts marked [inferred] come from the
plan or code rather than a direct user answer and should be confirmed.
-->

## Platform

web

## Users

- Clinic staff in Brazilian outpatient clinics: administrators, reception and
  scheduling staff, physicians and other clinicians. They work at a desk or on
  a phone between patients, often under time pressure, and need to finish one
  clinical or administrative task at a time (sign in, find a patient, book or
  move an appointment, record an encounter).
- Patients of those clinics, who register, answer intake questionnaires, book
  or reschedule appointments and join teleconsultations from their own
  devices. [inferred from the plan's patient-portal scope; not yet shipped]
- Both audiences read Brazilian Portuguese. Display localization is owned by
  plan task 8; stored enums, URLs and audit vocabulary stay in English.

## Product Purpose

A clinic operations system for modern Brazilian clinics: scheduling and
agenda, patient registration and intake, clinical encounters and records,
consent, teleconsultation, non-controlled prescriptions, PIX billing and
reminders, delivered as one modular Django monolith with HTMX-enhanced
server-rendered screens. Success means staff complete daily work faster with
fewer errors while every patient record stays isolated per clinic, auditable
and lawful under LGPD.

## Positioning

Security and compliance are structural, not features: PostgreSQL row-level
security, role-based access, TOTP for privileged roles and an immutable audit
trail are enforced at the database and middleware layers, and live patient data
stays closed until recovery and compliance gates are proven. Interoperability
(RNDS/SNCR) is prepared through stable identifiers and versioned documents but
remains a tested stub until real requirements are recorded.

## Operating Context

- Server-rendered Django templates with HTMX progressive enhancement; native
  form submission is always the baseline and JavaScript is optional.
- Every clinic has its own time zone; appointments store UTC and display
  clinic-local times. Patient identifiers never travel in URLs or logs.
- Staff sign in with a password; privileged roles then enroll and verify a
  TOTP authenticator, and repeat a step-up check before sensitive actions.
- The product is verified by an agent-run browser runner against a real
  supervised runtime (`ops/testing/renewal_runner.py`) using synthetic data
  only; screenshots must never contain real patient content or secrets.
- Installable PWA shell: the service worker caches versioned static assets
  only; clinical content, navigations and credentialed requests are never
  cached offline.

## Capabilities and Constraints

- Shipped today: authentication (login, TOTP enrollment, verification,
  step-up, logout), patient registry search and registration, availability
  blocks, agenda (day/week), booking, reschedule and cancel.
- Delivered in synthetic mode by the renewal: patient contacts, invitations
  and sessions, waitlist, reminders (email/SMS/WhatsApp), encounters with
  SOAP and specialty templates, problems/allergies/attachments, consent,
  teleconsultation, physician verification, prescriptions with
  PDF/QR/signatures, PIX and receipts, clinic configuration and branding.
  Every provider-backed slice runs on a labelled synthetic adapter and stays
  `waiting_external` until its capability record is approved; nothing here
  claims live readiness.
- No new UI framework: styling is hand-written CSS custom properties served as
  static files; no Tailwind, no component library, no client bundle.
- Product name: the codebase and copy say "Clinic OS"; the user's brand pack
  reads "Clinic Ops". The final name is an open decision. Until it is made,
  the shell wordmark, the installable manifest and the icon alt text carry
  the brand's "Clinic Ops" (plan task 9); code identifiers, routes, static
  paths, page titles and other copy keep "Clinic OS".
- Terminology: "clinic" (tenant unit inside an organization), "agenda",
  "availability block", "appointment", "encounter", "step-up verification".

## Brand Commitments

- Brand pack supplied by the user at `Clinic_Ops_SVG/` in the main checkout
  (not copied into task worktrees): a modular medical-cross symbol with
  compact, horizontal, monochrome and tagline lockups for light and dark
  surfaces; all lettering is outlined paths, no font files are included.
- Brand palette: cyan `#00E5D0` (principal), teal `#00B894` (secondary), dark
  teal `#007A87` (trust), deep navy `#0F2D3A` (text).
- Portuguese taglines from the brand board: "O sistema operacional para
  clínicas modernas"; "Mais tempo para o que importa"; "Tecnologia a serviço
  da medicina"; "Organiza · Automatiza · Conecta · Libera tempo".
- Voice: direct, reassuring, plain language; each screen names the next safe
  action and errors name both the problem and the recovery.
- Incumbent visual world to preserve: warm paper canvas with raised white work
  surfaces, tonal separation with precise 1px borders, no decorative shadows,
  glass, glow or gradients. DESIGN.md reconciles this world with the brand
  palette.

## Evidence on Hand

- Real: the running codebase, its tests and browser evidence under
  `.omo/evidence/clinic-os-renewal/`; the brand pack SVGs and previews.
- Absent and not to be fabricated: customers, testimonials, usage metrics,
  provider approvals (PIX, messaging, signatures, RNDS), legal sign-off, pilot
  results. All demonstration content is synthetic and labeled as such.

## Product Principles

1. Fail closed: when identity, tenancy, consent or recovery is uncertain, deny
   and explain; never guess.
2. One task per screen: a single primary action, short instructions, explicit
   recovery.
3. Familiar before clever: staff must trust every control at a glance; brand
   lives in precise details, not in novel affordances.
4. Evidence over claims: every capability is proven by an executed test or
   browser journey before it is described as working.
5. Portuguese first, identifiers stable: user-facing language is pt-BR while
   stored values, routes and audit vocabulary do not change.

## Accessibility & Inclusion

Target WCAG 2.2 AA. Every action is keyboard reachable in DOM order with a
visible, unobscured focus indicator; touch targets are at least 44 by 44 CSS
pixels; layouts reflow to one column at 320 CSS px and 200% zoom without
horizontal scrolling; forced-colors mode keeps controls, focus, errors and
status visible; reduced-motion preferences remove all nonessential motion.
Screen-reader users hear the page purpose, field errors and status changes in a
predictable order and never a secret value. Copy minimizes memory demands.

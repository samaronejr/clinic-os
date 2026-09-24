# Design-partner pilot record

**Status: WAITING_EXTERNAL — no pilot is authorized and none is running.**

This is the accountable record for the scoped design-partner pilot (renewal
plan task 47). It records what is already decided and names every external
input that is still missing. Its presence grants no authority: the
[live-data gate](../compliance/LIVE-DATA-GATE.md) stays UNAPPROVED until the
named accountable owners supply the records defined in
[RELEASE-EVIDENCE.md](../compliance/RELEASE-EVIDENCE.md). Nothing in this
file is legal review or permission to deploy.

## Pilot scope

- **Clinic:** exactly one approved design-partner clinic —
  `waiting_external` (no clinic has been approved or enrolled; this agent
  does not send outreach or enroll a clinic without explicit authorization).
- **Phase 1 — scheduling only:** staff availability, booking, day/week
  agendas, rescheduling, terminal cancellation, patient self-booking,
  waitlist and appointment reminders. This is the first feature a pilot
  clinic uses early (`initial_plan_en.md`), and it is the only phase this
  record opens.
- **Phase 2 — expansion:** clinical/provider capabilities (encounters,
  teleconsultation, signed documents, PIX billing) only when each capability
  is *separately* ready: its capability record in
  [docs/integrations/capabilities.md](../integrations/capabilities.md) is
  approved, its live evidence record passes `R live`, and the owner records
  the expansion here. No capability is expanded by implication.
- **Out of scope for the pilot:** controlled prescriptions and notifications,
  SNCR/RNDS exchange, TISS/insurance, and anything the product marks
  deferred or forbidden.

## Required record fields

| Field | Value |
| --- | --- |
| Design-partner clinic | `waiting_external` — owner-approved clinic identity |
| Selected infrastructure | `waiting_external` — no provider selected; `initial_plan_en.md` figures (AWS sa-east-1 ~US$50–150/month pilot sizing) are 2025–2026 references to reconfirm, not a selection |
| Budget approval | `waiting_external` — owner-approved spend envelope |
| Capacity plan | `waiting_external` — expected clinics/practitioners/appointments per week for the pilot |
| Deployment authority | `waiting_external` — the accountable `CLINIC_LIVE_ACTIVATION_APPROVAL` reference; activation refuses without it |
| Operator training | `waiting_external` — named operators, roles trained (reception, physician, clinic admin), date and trainer |
| Restore procedure | Defined for synthetic rehearsal only — see below; provider PITR/encrypted provider backup remain `waiting_external` (task 43) |
| Disable procedure | Defined — `A disable` below; stops new live writes/jobs without deleting or publishing records |
| Monitoring recipients | `waiting_external` — named recipients for health/incident delivery; the `monitoring_delivery` capability record is unsatisfied |

## Activation procedure (only after authority is supplied)

The synthetic acceptance environment and the pilot environment are separate.
Activation never inherits synthetic state:

1. **Exit and discard the synthetic runner environment.** Stop the
   `renewal_runner` Gunicorn/browser processes and remove its provisioned
   PostgreSQL container and volume (the runner's own teardown does this on
   exit; verify `docker ps`/`docker volume ls` show no `clinic_renewal_*`
   resources). Unset every synthetic-only variable so none leaks into the
   pilot shell: `COMMS_SYNTHETIC_CHANNELS`, `TELECONSULT_SYNTHETIC_PROVIDER`,
   `TELECONSULT_SYNTHETIC_FAIL`, `PRESCRIPTION_SYNTHETIC_SIGNING`,
   `PHYSICIAN_SYNTHETIC_REGISTRY`, `BILLING_SYNTHETIC_PIX`,
   `BILLING_SYNTHETIC_PIX_SECRET`, `CLINIC_LIVE_ACTIVATION_REHEARSAL`, and
   the runner's `CLINIC_RENEWAL_*` exports. The live contract rejects any of
   these being set.
2. **Load the independently provisioned pilot environment** — never reuse
   synthetic DB/server variables: `DJANGO_SETTINGS_MODULE` =
   `config.settings.prod` (runtime, `clinic_app`) or `config.settings.release`
   (owner operations); `CLINIC_DATA_MODE=live`; `APP_DATABASE_URL` on the
   pilot endpoint as `clinic_app`; `SECRET_KEY`, `ALLOWED_HOSTS`,
   `SECURE_SSL_HOST`; `CLINIC_SECRET_BACKEND`/`CLINIC_SECRET_DIR` naming the
   approved managed store; `EHR_ATTACHMENT_ROOT` absolute and dedicated;
   `CLINIC_LIVE_ACTIVATION_STATE` absolute; `CLINIC_RELEASE_EVIDENCE_ROOT`
   and `CLINIC_RELEASE_ID`; `CELERY_BROKER_URL`; and
   `CLINIC_LIVE_ACTIVATION_APPROVAL` carrying the accountable approval
   reference verbatim.
3. **Run the dependent steps in order, each only after the previous exits 0:**
   - `R live` — `uv run --frozen --no-sync --no-env-file python -m
     ops.release.readiness check --mode live` must report ready against the
     pilot evidence root (all 28 capabilities satisfied, none synthetic).
   - `A preflight` — `python -m ops.release.activation preflight` must report
     ready with no live-environment gap.
   - `A activate` — `python -m ops.release.activation activate` writes the
     bound activation record and claims the pilot database endpoint and
     attachment root.
   - Verify `/healthz` and `/readyz` on the pilot deployment before any
     operator use.

## Disable / rollback procedure

`python -m ops.release.activation disable` marks the activation record
`disabled` atomically. Running processes halt at the request boundary
(`LiveModeHaltMiddleware`), the shared job boundary and the attachment
storage boundary on their next unit of work — new live writes and jobs stop
in already-running processes, not just new ones. Disable deletes nothing and
makes nothing public; the endpoint claim and root ownership marker persist
because the storage still holds live-bound data. During the pilot this is
the approved rollback path — do not improvise destructive cleanup.

## Restore procedure

The only verified restore today is the disposable logical rehearsal
(`make restore-rehearsal`, see [RUNBOOK.md](../RUNBOOK.md)): a synthetic
integrity check into a task-owned target, not a live backup design and not
reusable as an incident procedure. Provider PITR and encrypted provider
backup are `waiting_external` (task 43); until they carry real evidence,
production restore authority does not exist and this field cannot be
satisfied.

## Monitoring and incident handling

- Monitoring delivery is `waiting_external`: no recipients are named and the
  `monitoring_delivery` capability record is unsatisfied. Before activation,
  record here the named recipients for uptime, readiness and incident
  notifications.
- Incidents during the pilot follow the approved incident procedure
  ([docs/compliance/INCIDENT-APPROVAL.md](../compliance/INCIDENT-APPROVAL.md)
  and [SECURITY.md](../SECURITY.md)): preserve evidence, record the timeline,
  and use `A disable` for live-write rollback. Every recorded incident —
  notified or not — is retained at least five years from registration per
  the `incident_record_retention` requirement.

## Observation and acceptance plan

- **Phase 1 gate:** one full week (seven consecutive operating days) of the
  pilot clinic's real scheduling through the product — availability
  management, booking, agenda use, rescheduling, cancellation and reminder
  delivery — with uptime, delivery and recovery observations recorded.
- **Phase 2 gate:** the broader workflow criteria agreed with the owner for
  each separately expanded capability.
- **Evidence rules:** report actual evidence and incidents only; no patient
  names, identifiers, clinical content, screenshots of real patient data,
  credentials or secrets in repository artifacts. Privacy-safe counts,
  dates, uptime figures and incident timelines only.
- **Acceptance:** pilot scope plus owner-recorded acceptance with real
  operating evidence. If access, elapsed observation or authority is
  missing, this task stays `waiting_external` and the roadmap is not
  complete.

## Current readiness snapshot

`A preflight` in synthetic mode (the only mode that exists today) reports
`ready: true` for the shipped synthetic evidence bundle and a live gap
report of 28 unsatisfied capabilities plus 10 live-environment gaps — every
one of them an external prerequisite, not a code defect. The synthetic
end-to-end rehearsal (task 45, `B end-to-end`) passes; it proves the product
surface, not pilot readiness.

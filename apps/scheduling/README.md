# Scheduling

Phase 1A implements practitioner availability creation, listing and retirement;
appointment booking; day/week agendas; rescheduling; and terminal cancellation.
Staff workflows preserve tenant and clinic scope, role restrictions,
idempotency, overlap protection, and clinic-local time handling.

The staff service facade is `services.py`; HTTP routes are in `urls.py`.
`patient_booking.py` exposes `/patient/appointments/` to enrollment-bound sessions
with an explicit `booking` operation. New invitations grant this operation;
existing invitations/sessions are not upgraded. Patient authority never sets a
staff actor or tenant GUC. Narrow database policies expose only the patient's
clinic appointments; resolver projections return free slots, not occupied rows.

Patients choose signed 30-minute clinic-local slots (up to 200 per selected day).
Submission derives enrollment/clinic from the validated session and reuses the
staff booking service's revalidation, lock order, idempotency and exclusion
constraints. Replaying a successful key returns the current row, even after a
transition. Rescheduling retains the practitioner; cancellation uses
`patient_request`, remains terminal and is safe to repeat. Database-owned
immutable `PatientBookingEvent` receipts identify the real patient session,
without fabricating staff audit events. Native forms carry identifiers only in
POST bodies and reject hidden scope fields. The registered `self-booking`
browser suite exercises these flows against the `clinic_app` server.

## Waitlists and offers

`waitlist.py` records an enrolled patient's requested practitioner and UTC window.
The immutable entry ID is its FIFO sequence. Staff manage the queue at
`/scheduling/clinics/<clinic>/waitlist/`; patients inspect only their own offers at
`/patient/offers/`, using the existing invited `booking` authority. Identifiers
travel in POST bodies; staff routes preserve clinic roles and privileged TOTP.

Issuing an offer chooses the first matching, currently eligible entry. Overlapping
pending offers are excluded in PostgreSQL, with one pending offer per entry.
Offers expire after 30 minutes, shown explicitly in the clinic timezone. They
are not reservations: ordinary booking remains possible. Acceptance locks and
rechecks the offer, calls the existing booking service atomically, then checks
the deadline again after booking locks. Expiry during booking rolls the booking
back. Replays return the original terminal result, never another appointment.

Expired, declined and fulfilled entries and every offer remain in history.
A stale opening returns the entry to its original FIFO position for another
opening. Expiry is reconciled on viewing or issuing; staff explicitly issue the
next offer. There is no timer-driven delivery or inferred clinical priority.

Offers are notices in the patient portal. External notice eligibility requires
an explicit `waitlist_offer` preference and a verified current destination;
reminder/confirmation consent is not reused. `waitlist_notice_channels` rechecks
consent, verification and offer state/expiry. It is an eligibility gate, not a
delivery receipt. Task 19 implements appointment-reminder workers, not external
waitlist-offer delivery; real messaging remains blocked by independent approvals.
The registered `waitlist` browser suite exercises cancel/offer/accept/FIFO,
expired/replayed/stale offers, scope denial, and native responsive journeys.

## Resources, services and calendar definitions

Scheduling Settings is linked from clinic Settings at
`/scheduling/clinics/<clinic>/settings/`. Native, no-store forms publish resources,
service types, weekly templates, holidays and absences; selectors stay in POST
bodies. Scheduler, reception and clinic-manager booking permissions authorize
configuration, as does organization configuration authority. Remove-only permission
grants are rechecked. Physician booking authority is limited to their own schedule;
new service bookings also support nurse/allied practitioners when the service's
required professional roles permit them. Legacy physician-only entrypoints retain
their existing authority and exclusion semantics.

`prepare_booking(*, clinic_id, enrollment_id, service_type_id=None, resource_ids=())`
returns scoped availability and selected duration/buffer metadata.
`create_service_appointment(*, clinic_id, enrollment_id, practitioner_id, booking,
idempotency_key)` takes `ServiceBooking(local_range, service_type_id, resource_ids)`.
It shares the legacy `create_appointment` write path, without changing that public
signature or old fingerprints. Service/resource selection participates in the new
fingerprint and is immutable after booking. Move and cancel keep those bindings;
cancellation releases occupancy without deleting reservation history.

A resource represents 1-64 interchangeable units of a room, equipment or location.
A database-owned trigger allocates the lowest free unit. `AppointmentResource`
excludes overlapping `[start,end)` effective intervals per `(resource, unit)`;
runtime callers can only read these rows. Capacity is therefore enforced even
without Python prechecks. Service buffers also have a separate practitioner
exclusion; the original practitioner and patient exclusions are unchanged. The
shared advisory order is identity, clinic, practitioner/user, resource UUIDs, then
patient. Availability and appointment row locks follow. Clinic row share/exclusive
locks serialize calendar-definition changes against direct SQL booking writes.

Definitions are immutable except one-way `active -> retired`. Service duration is
1-720 minutes, each buffer 0-240 minutes. `insurer_billable` and `price_ref` are
configuration metadata, not a charge or payer authorization. A template has one
local interval, selected weekdays (Monday=0), inclusive validity dates, exactly one
practitioner/resource and a captured clinic timezone. Multiple daily intervals use
separate templates. Template creation freezes clinic timezone before any generation.

The explicit-date `generate_availability` job creates concrete `AvailabilityBlock`
rows keyed by `(template_id, generated_date)`, with deterministic idempotency UUIDs.
It is atomic, retryable, bounded to 366 date steps, and never depends on a timer or
current time. DST gaps/folds are refused, not silently resolved. Settings invokes the
same job; unattended callers can use `manage.py generate_scheduling_availability`
with `--user-id --organization-id --clinic-id --template-id --start-date --end-date`
on the ordinary runtime connection. These are trusted execution bindings, not a
service-level actor override; revoked permission still refuses the job.

Closures use closed, non-clinical reason codes. A holiday/absence overlapping an
existing occupied appointment is refused; it never silently invalidates or cancels
that booking. Occupied generated templates cannot be retired. Service failures expose
`resource_conflict`, `outside_template`, `holiday` or `buffer_violation` with pt-BR
messages; unknown and foreign selectors share the same denial. Patient free-slot
projections also exclude holidays, absences, retired templates and practitioner
buffers, and submission independently rechecks the database rules.

Migration `0005_resources_templates` is additive. Empty-schema reverse/forward is
supported; downgrade refuses once any new definition is populated, rather than
losing history. After rollout, rollback is a restore, not hard deletion. Todo 22
owns lifecycle widening; capacity guards already name `{held, scheduled, arrived,
in_progress}` while the existing appointment lifecycle constraints remain unchanged.

## Appointment reminders

Booking (including patient and waitlist booking) atomically schedules eligible
reminders through the comms-owned database hook. Cancellation and rescheduling
invalidate pending notices under the shared send lock. The agenda links to the
clinic-scoped delivery-status screen. See [communications](../comms/README.md)
for the 24-hour clinic-local schedule, independent gates and receipt semantics.

Current screens and fixtures are synthetic-only; see
[the architecture](../../docs/ARCHITECTURE.md) and
[the live-data gate](../../docs/compliance/LIVE-DATA-GATE.md).

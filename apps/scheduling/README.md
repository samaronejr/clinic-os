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

## Appointment reminders

Booking (including patient and waitlist booking) atomically schedules eligible
reminders through the comms-owned database hook. Cancellation and rescheduling
invalidate pending notices under the shared send lock. The agenda links to the
clinic-scoped delivery-status screen. See [communications](../comms/README.md)
for the 24-hour clinic-local schedule, independent gates and receipt semantics.

## Machine availability reads

Task 7 declares only availability SELECT in `AGENT_GRANTS`. The `clinic_agent`
role has no appointment or scheduling write privileges. Availability keeps its
existing tenant policy and additionally applies the restrictive `agent_grant`
policy, requiring a login-bound, active `appointment.read` grant for that exact
clinic. Consumers use `.using('agent')` inside `service_principal_context`.
Booking/moving is not enabled by a read grant and awaits its domain policy.

Current screens and fixtures are synthetic-only; see
[the architecture](../../docs/ARCHITECTURE.md) and
[the live-data gate](../../docs/compliance/LIVE-DATA-GATE.md).

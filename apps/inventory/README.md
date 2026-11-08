# Inventory contract - todo 61

Inventory is a clinic-scoped domain service boundary. Its public entrypoints
are in `services.py`; it adds no browser surface. Tests run against PostgreSQL
roles with the actual policies, grants, encryption and transition triggers.

## Scope and authority

Track consumables by item, lot, expiry and location. Keep immutable receipt,
use, return, write-off and transfer facts; derive balances from those facts.
Purchasing is draft -> approved -> received. Maintain suppliers, equipment,
append-only calibration and maintenance records, minimum-stock alerts, and
budget-bound replenishment drafts. Never dispense controlled drugs or derive
treatment decisions from available stock.

All commands are keyword-only, explicitly clinic-scoped, and derive authority
from the current authenticated database context, never a supplied actor.
Unknown and foreign selectors have the same `InventoryAccessDeniedError`.
Inventory services translate authorization refusals to that domain error.

Todo 61's specific inventory authority overrides the general financial write-off
row: clinic_manager and legacy clinic_admin manage inventory and approve stock
disposal/purchases. Nurse may use and return stock and request purchases.
Physician, allied, reception, scheduler, finance, org_admin and owner do not
inherit inventory disposal/use privileges merely from their other roles.
Organization-wide policy is not a clinic stock-use grant.

Permissions extend the current v2 bundle mechanism: `inventory.read`,
`inventory.use`, `inventory.manage` and `inventory.transfer_org`. Released v1
terms remain unchanged. Both Python and SQL honor current membership and v2
RoleGrant subtraction.
Removing `inventory.use` does not subtract `inventory.manage`: catalog
publication, receipt, disposal, transfer and purchase approval use their exact
management permission in services, policies and triggers.
Cross-clinic transfer requires explicit organizational transfer permission plus
authority at both endpoints; cross-organization transfer is always denied.
Migration `identity.0044_inventory_permissions` extends the current bound-actor
evaluator and subtraction constraint without replacing `has_permission`,
losing privacy/workflow authority, or upgrading clinical scope.

## Stock command seam

The public facade is `apps.inventory.services`.

| Service | Keyword arguments beyond clinic_id | Result |
| --- | --- | --- |
| receive | batch_id, location_id, quantity, command_id | StockMovement |
| use | batch_id, location_id, quantity, command_id, procedure_entry_id=None | StockMovement |
| return_stock | use_movement_id, quantity, command_id | StockMovement |
| write_off | batch_id, location_id, quantity, reason, command_id | StockMovement |
| transfer | batch_id, source_location_id, destination_location_id, quantity, command_id | one StockMovement containing both endpoints |
| balance | batch_id, location_id | integer units |
| stock_alerts | no additional selector | tuple of frozen alert values |

`return_stock` expresses the plan's `return` operation without a Python keyword.
Quantities are strictly positive integer smallest units, excluding booleans and
floats. A return references its original use, shares its item/batch/location
and cannot cumulatively exceed that debit. A transfer atomically records both
legs under one command; it must never create an intermediate committed deficit.
No mutable balance is authoritative. No UPDATE, DELETE or TRUNCATE of movements.
The database must independently refuse insufficient stock and unauthorized
disposal, including direct runtime inserts.

Serialize Inventory writes under one organization advisory transaction lock,
then reload committed facts and revalidate. This intentionally coarse first
version also orders cross-clinic stock, cumulative returns and budget decisions
without lock-order inversion. Services and direct-write triggers use the same
lock; runtime writes require READ COMMITTED. A transfer is one immutable fact,
so debit and credit cannot commit separately. Its originating batch identity
remains retained; an authorized destination can use incoming stock without
gaining clinical narrative access.
Exact command replay has one effect; altered payload reuse raises
`InventoryConflictError("idempotency_conflict")`. Stock shortage is
`insufficient_stock`, expired-use refusal is `batch_expired`, and excess return
is `return_exceeds_use`. Errors expose machine codes, not patient data.

Expiry uses the clinic timezone and a single authoritative DB clock; a batch
whose expires_on equals the local current date is unavailable for use/transfer.
Expiry tests include retained expired lots, usable future lots and the exact
clinic-local date boundary. Expired receipts may be recorded for stock
accounting and disposal, but cannot be consumed in care.

## Procedure link

Use `ehr.SpecialtyEntry(kind="procedure")`, not a new competing procedure model.
The existing `record_procedure` service owns scanned consent/image links and its
signature stays unchanged. `StockMovement.procedure_entry` references that exact
retained entry. Verify entry kind, clinic, organization and current scope without
granting the inventory user clinical narrative access. Never accept a patient
identity as a substitute for the procedure's stored binding.

## Procurement and equipment seams

Manager-published catalog commands are `create_item`, `create_batch`,
`create_location`, `create_supplier` and `set_minimum_stock`. All take clinic_id
and command_id as keyword-only parameters. Catalog identities are retained,
not silently rewritten. MinimumStock publications are additive; the latest
item/location publication controls alerts, which exclude expired units from
usable stock and flag positive lots expiring within 30 days.

`create_purchase_request(clinic_id, item_id, supplier_id, quantity, amount_minor,
command_id)` creates a draft; `approve_purchase(clinic_id, purchase_request_id,
command_id)` authorizes without receiving stock; `receive_purchase(clinic_id,
purchase_request_id, batch_id, location_id, command_id)` receives the approved
quantity once. All arguments are keyword-only. BRL `amount_minor` is positive
integer centavos. Receiving before approval raises `purchase_not_approved`.

`PurchaseBudget` retains the amount, approving manager and approval time.
`approve_budget(clinic_id, amount_minor, command_id)` publishes this immutable
approval using current manager authority and database time.
`propose_replenishment(clinic_id, budget_id, item_id, supplier_id, quantity,
amount_minor, command_id)` produces only a draft within remaining approved
budget. A missing or malformed budget selector is refused; it cannot become
an ordinary unbudgeted request. Approval reserves the amount atomically;
receipt does not free it for
double spending. Insufficient remaining funds raises `budget_exceeded`. No
supplier contact, external order, payment or autonomous purchase is implied.

`register_equipment(clinic_id, supplier_id, location_id, name, serial,
command_id)` retains an asset; `record_calibration` and `record_maintenance`
take clinic_id, equipment_id, performed_on, due_on, outcome, command_id.
`equipment_status(clinic_id, equipment_id)` returns retained calibration and
maintenance IDs, overdue flags and usability; failed/overdue calibration cannot
be cleared merely by recording maintenance. The database refuses a performed
date after the current clinic-local date, so future evidence cannot make an
asset usable today.

## Database and privacy obligations

Every new tenant model inherits TenantScopedModel with clinic scope, FORCE RLS,
same-scope foreign binding checks, minimum grants and immutable history guards.
No grants to clinic_agent by default. The app declares RLS_TARGETS,
CUSTOM_RLS_TABLES, NON_RLS_TABLES, RUNTIME_GRANTS, COLUMN_GRANTS and AGENT_GRANTS
in rls.py. New SECURITY DEFINER resolvers need exact owner/search_path/grant
catalog registration. Clinical references never enter audit/outbox payloads.
Supplier contact data and any free-text personal data require purpose-bound
encryption; do not add plaintext shadow fields. Audit every successful command
with a registered event and the existing closed payload keys.

The exact resolver catalog and per-category posture digests come from the
fresh migrated PostgreSQL catalog. `tests/inventory/test_posture.py` pins policy
roles, commands, predicates, grants, column types, constraints, triggers and
function bodies; the full captured catalog belongs in the task evidence.
Every retained inventory relation is classified in the recovery manifest.
The protected initial migration is irreversible; rollback = restore.

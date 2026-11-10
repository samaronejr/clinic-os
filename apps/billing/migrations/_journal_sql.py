"""Frozen migration assembly: least privilege, FORCE RLS and constraint triggers."""

from apps.billing.migrations._journal_cash_sql import CASH_SQL
from apps.billing.migrations._journal_collection_sql import COLLECTION_SQL
from apps.billing.migrations._journal_core_sql import CORE_SQL
from apps.billing.migrations._journal_definitions_sql import DEFINITIONS_SQL
from apps.billing.migrations._journal_legacy_sql import (
    LEGACY_AUTH_SQL,
    LEGACY_BRIDGE_SQL,
)
from apps.billing.migrations._journal_payout_sql import PAYOUT_SQL
from apps.billing.migrations._journal_post_sql import POST_SQL
from apps.billing.migrations._journal_recognition_sql import RECOGNITION_SQL
from apps.billing.migrations._journal_refund_sql import REFUND_SQL
from apps.tenancy.migrations_support import revoke_default_dml

TABLES = (
    "journalbook",
    "account",
    "journalentry",
    "journalline",
    "practitionerpayoutrule",
    "invoiceterms",
    "journalsettlement",
    "settlementallocation",
    "refund",
    "payout",
    "cashsession",
    "cashsessionevent",
)
_INSERT = {
    "journalbook",
    "practitionerpayoutrule",
    "invoiceterms",
    "journalsettlement",
    "settlementallocation",
    "refund",
    "payout",
    "cashsession",
}
_RESOLVER_INSERT = {
    "account",
    "journalentry",
    "journalline",
    "invoiceterms",
    "journalsettlement",
    "settlementallocation",
    "cashsessionevent",
}
_COLUMNS = {
    "journalsettlement": ("state",),
    "refund": ("state", "evidence_reference"),
    "payout": ("state", "evidence_reference"),
    "cashsession": ("state", "counted_minor", "reason", "last_command_id"),
}
FUNCTIONS = (
    ("billing_finance_read(uuid)", True),
    ("billing_lock_book(uuid)", True),
    ("billing_lock_clinic_book(uuid)", True),
    ("billing_book_access(uuid,text)", True),
    ("billing_entry_readable(uuid)", True),
    ("billing_practitioners(uuid)", True),
    ("billing_finance_session(uuid)", True),
    ("billing_round_ratio(numeric,numeric)", True),
    ("billing_line(text,text,bigint,uuid)", False),
    ("billing_post_internal(uuid,text,uuid,jsonb,uuid,date[])", False),
    ("billing_post(uuid,text,uuid,jsonb,uuid,date[])", True),
    ("billing_opening_balance(uuid,text,bigint,uuid)", True),
    ("billing_journal_guard()", False),
    ("billing_entry_balance()", False),
    ("billing_definition_guard()", False),
    ("billing_seed_chart()", False),
    ("billing_charge_authorized(uuid)", True),
    ("billing_collection_authorized(uuid)", False),
    ("billing_settlement_journal_guard()", False),
    ("billing_refunded_prepayments(uuid)", False),
    ("billing_allocation_guard()", False),
    ("billing_recognize_completion(uuid,uuid,uuid)", True),
    ("billing_complete_appointment(uuid,uuid)", True),
    ("billing_refund_guard()", False),
    ("billing_payout_guard()", False),
    ("billing_account_balance(uuid,text)", False),
    ("billing_cash_guard()", False),
    ("billing_cash_receipt()", False),
    ("billing_import_legacy(uuid,uuid)", True),
    ("billing_allocation_paid()", False),
)

_sql = ""
for _table in TABLES:
    _name = f"billing_{_table}"
    _sql += revoke_default_dml(_name, ())
    _sql += f"REVOKE ALL ON clinic_app.{_name} FROM PUBLIC,clinic_app,clinic_agent;\n"
    _sql += f"GRANT SELECT ON clinic_app.{_name} TO clinic_app,clinic_resolver;\n"
    if _table in _INSERT:
        _sql += f"GRANT INSERT ON clinic_app.{_name} TO clinic_app;\n"
    if _table in _RESOLVER_INSERT:
        _sql += f"GRANT INSERT ON clinic_app.{_name} TO clinic_resolver;\n"
    if _table in _COLUMNS:
        _cols = ",".join(_COLUMNS[_table])
        _sql += f"GRANT UPDATE({_cols}) ON clinic_app.{_name} TO clinic_app;\n"

_sql += """
GRANT UPDATE(id) ON clinic_app.billing_journalbook TO clinic_resolver;
GRANT UPDATE(state,posted_at) ON clinic_app.billing_journalentry TO clinic_resolver;
GRANT UPDATE(completion_entry_id,completion_evidence)
 ON clinic_app.billing_invoiceterms TO clinic_resolver;
GRANT UPDATE(state) ON clinic_app.billing_journalsettlement TO clinic_resolver;
SET LOCAL ROLE clinic_resolver;
"""
_sql += (
    LEGACY_AUTH_SQL
    + CORE_SQL
    + POST_SQL
    + DEFINITIONS_SQL
    + COLLECTION_SQL
    + RECOGNITION_SQL
    + REFUND_SQL
    + PAYOUT_SQL
    + CASH_SQL
    + LEGACY_BRIDGE_SQL
)
for _signature, _public_api in FUNCTIONS:
    _sql += (
        f"REVOKE ALL ON FUNCTION clinic_app.{_signature} "
        "FROM PUBLIC,clinic_app,clinic_agent;\n"
    )
    if _public_api:
        _sql += f"GRANT EXECUTE ON FUNCTION clinic_app.{_signature} TO clinic_app;\n"
    _sql += f"GRANT EXECUTE ON FUNCTION clinic_app.{_signature} TO clinic_owner;\n"
_sql += """
GRANT EXECUTE ON FUNCTION clinic_app.billing_immutable() TO clinic_owner;
RESET ROLE;
DROP POLICY billing_staff ON clinic_app.billing_invoice;
CREATE POLICY billing_staff ON clinic_app.billing_invoice
 USING (organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid
 AND clinic_app.billing_charge_authorized(clinic_id))
 WITH CHECK (organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid
 AND clinic_app.billing_charge_authorized(clinic_id));
"""

_GUARDS = {
    "journalbook": "billing_definition_guard",
    "account": "billing_definition_guard",
    "practitionerpayoutrule": "billing_definition_guard",
    "invoiceterms": "billing_definition_guard",
    "journalentry": "billing_journal_guard",
    "journalline": "billing_journal_guard",
    "journalsettlement": "billing_settlement_journal_guard",
    "settlementallocation": "billing_allocation_guard",
    "refund": "billing_refund_guard",
    "payout": "billing_payout_guard",
    "cashsession": "billing_cash_guard",
}
for _table in TABLES:
    _scope = (
        "clinic_app.billing_finance_read(clinic_id)"
        if _table == "journalbook"
        else "clinic_app.billing_entry_readable(entry_id)"
        if _table == "journalline"
        else "clinic_app.billing_book_access(book_id,'journal.read')"
    )
    _predicate = (
        "organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid"
        f" AND {_scope}"
    )
    _sql += f"""
ALTER TABLE clinic_app.billing_{_table} ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.billing_{_table} FORCE ROW LEVEL SECURITY;
CREATE POLICY billing_journal_scope ON clinic_app.billing_{_table}
 USING ({_predicate}) WITH CHECK ({_predicate});
CREATE TRIGGER billing_no_truncate BEFORE TRUNCATE ON clinic_app.billing_{_table}
 FOR EACH STATEMENT EXECUTE FUNCTION clinic_app.billing_immutable();
"""
    if _table in _GUARDS:
        _sql += f"""
CREATE TRIGGER billing_journal_guard BEFORE INSERT OR UPDATE OR DELETE
 ON clinic_app.billing_{_table} FOR EACH ROW
 EXECUTE FUNCTION clinic_app.{_GUARDS[_table]}();
"""
    else:
        _sql += f"""
CREATE TRIGGER billing_history_immutable BEFORE UPDATE OR DELETE
 ON clinic_app.billing_{_table} FOR EACH ROW
 EXECUTE FUNCTION clinic_app.billing_immutable();
"""

_sql += """
CREATE TRIGGER billing_seed_chart AFTER INSERT ON clinic_app.billing_journalbook
 FOR EACH ROW EXECUTE FUNCTION clinic_app.billing_seed_chart();
CREATE TRIGGER billing_cash_receipt AFTER INSERT OR UPDATE
 ON clinic_app.billing_cashsession FOR EACH ROW
 EXECUTE FUNCTION clinic_app.billing_cash_receipt();
CREATE TRIGGER billing_allocation_paid AFTER INSERT
 ON clinic_app.billing_settlementallocation FOR EACH ROW
 EXECUTE FUNCTION clinic_app.billing_allocation_paid();
CREATE CONSTRAINT TRIGGER billing_entry_balanced AFTER INSERT OR UPDATE
 ON clinic_app.billing_journalentry DEFERRABLE INITIALLY DEFERRED
 FOR EACH ROW EXECUTE FUNCTION clinic_app.billing_entry_balance();
CREATE CONSTRAINT TRIGGER billing_entry_balanced AFTER INSERT
 ON clinic_app.billing_journalline DEFERRABLE INITIALLY DEFERRED
 FOR EACH ROW EXECUTE FUNCTION clinic_app.billing_entry_balance();
SET LOCAL ROLE clinic_resolver;
REVOKE EXECUTE ON FUNCTION clinic_app.billing_immutable() FROM clinic_owner;
"""
for _signature, _public_api in FUNCTIONS:
    _sql += f"REVOKE EXECUTE ON FUNCTION clinic_app.{_signature} FROM clinic_owner;\n"
# The owner evaluates the billing_staff invoice policy on every maintenance
# write; its boolean charge check must stay callable there or the policy errors
# (the same contract questionnaire_staff kept before this migration).
_sql += """GRANT EXECUTE ON FUNCTION clinic_app.billing_charge_authorized(uuid)
 TO clinic_owner;
RESET ROLE;
"""
SQL = _sql

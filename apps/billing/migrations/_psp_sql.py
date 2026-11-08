"""Least-privilege FORCE RLS and immutable PSP history, including truncate guards."""

from apps.billing.migrations._psp_guard_sql import SQL as GUARD_SQL
from apps.billing.migrations._psp_scope_sql import SQL as SCOPE_SQL
from apps.tenancy.migrations_support import revoke_default_dml

TABLES = ("psppolicy", "pspintent", "pspbinding", "pspevidence", "pspreceipt")
FUNCTIONS = (
    ("billing_psp_access(uuid)", True),
    ("billing_psp_scope(text,text)", True),
    ("billing_psp_intent_scope(uuid)", True),
    ("billing_psp_seen(text,text,text)", True),
    ("billing_psp_dispatchable(uuid,boolean)", True),
    ("billing_patient_psp_instructions(uuid)", True),
    ("billing_psp_guard()", False),
)
SQL = ""
for _table in TABLES:
    SQL += revoke_default_dml(f"billing_{_table}", ())
    SQL += f"""
REVOKE ALL ON clinic_app.billing_{_table} FROM PUBLIC,clinic_app,clinic_agent;
GRANT SELECT,INSERT ON clinic_app.billing_{_table} TO clinic_app;
GRANT SELECT ON clinic_app.billing_{_table} TO clinic_resolver;
"""
SQL += "SET LOCAL ROLE clinic_resolver;\n" + SCOPE_SQL + GUARD_SQL
for _signature, _public in FUNCTIONS:
    SQL += f"""
REVOKE ALL ON FUNCTION clinic_app.{_signature} FROM PUBLIC,clinic_app,clinic_agent;
GRANT EXECUTE ON FUNCTION clinic_app.{_signature} TO clinic_owner;
"""
    if _public:
        SQL += f"GRANT EXECUTE ON FUNCTION clinic_app.{_signature} TO clinic_app;\n"
SQL += """
GRANT EXECUTE ON FUNCTION clinic_app.billing_immutable() TO clinic_owner;
RESET ROLE;
"""
for _table in TABLES:
    _scope = (
        "(clinic_app.billing_book_access(book_id,'journal.read') "
        "OR clinic_app.billing_book_access(book_id,'charge.collect'))"
        if _table == "psppolicy"
        else "clinic_app.billing_psp_access(id)"
        if _table == "pspintent"
        else "clinic_app.billing_psp_access(intent_id)"
    )
    # Intent insert authority is checked by the trigger before the row exists.
    _check = "true" if _table == "pspintent" else _scope
    if _table == "psppolicy":
        _check = "clinic_app.billing_book_access(book_id,'finance.policy')"
    SQL += f"""
ALTER TABLE clinic_app.billing_{_table} ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.billing_{_table} FORCE ROW LEVEL SECURITY;
CREATE POLICY billing_psp_read ON clinic_app.billing_{_table} FOR SELECT
 USING ({_scope});
CREATE POLICY billing_psp_insert ON clinic_app.billing_{_table} FOR INSERT
 WITH CHECK (organization_id=
 NULLIF(current_setting('app.current_tenant',true),'')::uuid AND {_check});
CREATE TRIGGER billing_psp_guard BEFORE INSERT OR UPDATE OR DELETE
 ON clinic_app.billing_{_table} FOR EACH ROW
 EXECUTE FUNCTION clinic_app.billing_psp_guard();
CREATE TRIGGER billing_psp_no_truncate BEFORE TRUNCATE
 ON clinic_app.billing_{_table} FOR EACH STATEMENT
 EXECUTE FUNCTION clinic_app.billing_immutable();
"""
SQL += "SET LOCAL ROLE clinic_resolver;\n"
for _signature, _public in FUNCTIONS:
    SQL += f"REVOKE EXECUTE ON FUNCTION clinic_app.{_signature} FROM clinic_owner;\n"
SQL += """
REVOKE EXECUTE ON FUNCTION clinic_app.billing_immutable() FROM clinic_owner;
RESET ROLE;
"""

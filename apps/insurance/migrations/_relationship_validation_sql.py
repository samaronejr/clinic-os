"""RLS DDL used only within the owner relationship-validation transaction."""

RELAX_FORCE_SQL = "ALTER TABLE clinic_app.{table} NO FORCE ROW LEVEL SECURITY"
RESTORE_FORCE_SQL = "ALTER TABLE clinic_app.{table} FORCE ROW LEVEL SECURITY"

"""Rule/policy expression relations with unique names and real runtime oracles."""

from .clock_residual_probes import Probe

GRANTS = (
    "GRANT USAGE ON SCHEMA r9 TO clinic_owner; "
    "GRANT ALL ON ALL TABLES IN SCHEMA r9 TO clinic_owner; "
)

RULE = Probe(
    "B3-EXPRESSION-RELATIONS",
    "CREATE SCHEMA r9; CREATE TABLE r9.zq_t(v integer); "
    "CREATE TABLE r9.zq_journal(v integer,at timestamptz DEFAULT clock_timestamp()); "
    "CREATE RULE zq_also AS ON INSERT TO r9.zq_t DO ALSO "
    "INSERT INTO r9.zq_journal(v) VALUES(NEW.v); "
    "CREATE FUNCTION clinic_app.scheduling_r9_root(p integer) RETURNS integer "
    "LANGUAGE sql VOLATILE AS $$ INSERT INTO r9.zq_t VALUES($1) RETURNING v $$; "
    + GRANTS
    + "SELECT clinic_app.scheduling_r9_root(1);",
    "DROP FUNCTION clinic_app.scheduling_r9_root(integer); DROP SCHEMA r9 CASCADE;",
    "SELECT count(*)=1 AND bool_and(at IS NOT NULL) FROM r9.zq_journal",
)

POLICY = Probe(
    "B3-EXPRESSION-RELATIONS",
    "CREATE SCHEMA r9; CREATE TABLE r9.zq_gate(ends_at timestamptz); "
    "ALTER TABLE r9.zq_gate ENABLE ROW LEVEL SECURITY; "
    "ALTER TABLE r9.zq_gate FORCE ROW LEVEL SECURITY; "
    "CREATE POLICY zq_pg ON r9.zq_gate USING(ends_at > now()); "
    "CREATE TABLE r9.zq_rows(v integer); "
    "ALTER TABLE r9.zq_rows ENABLE ROW LEVEL SECURITY; "
    "ALTER TABLE r9.zq_rows FORCE ROW LEVEL SECURITY; "
    "CREATE POLICY zq_pr ON r9.zq_rows "
    "USING(NOT EXISTS(SELECT 1 FROM r9.zq_gate)); "
    "INSERT INTO r9.zq_gate VALUES('2999-01-01'); INSERT INTO r9.zq_rows VALUES(1); "
    "CREATE FUNCTION clinic_app.scheduling_r9_root() RETURNS integer "
    "LANGUAGE sql VOLATILE AS $$ SELECT v FROM r9.zq_rows LIMIT 1 $$; "
    "GRANT USAGE ON SCHEMA r9 TO clinic_owner; "
    "GRANT ALL ON ALL TABLES IN SCHEMA r9 TO clinic_owner; ",
    "DROP FUNCTION clinic_app.scheduling_r9_root(); DROP SCHEMA r9 CASCADE;",
    "SELECT clinic_app.scheduling_r9_root()=1",
)

POLICY_FUNCTION = Probe(
    "B3-EXPRESSION-RELATIONS",
    "CREATE SCHEMA r9; "
    "CREATE TABLE r9.zq_journal(at timestamptz DEFAULT clock_timestamp()); "
    "INSERT INTO r9.zq_journal DEFAULT VALUES; "
    "CREATE FUNCTION r9.zq_helper() RETURNS boolean LANGUAGE sql VOLATILE "
    "AS $$ SELECT count(*)>0 FROM r9.zq_journal $$; "
    "CREATE TABLE r9.zq_rows(v integer); INSERT INTO r9.zq_rows VALUES(1); "
    "ALTER TABLE r9.zq_rows ENABLE ROW LEVEL SECURITY; "
    "ALTER TABLE r9.zq_rows FORCE ROW LEVEL SECURITY; "
    "CREATE POLICY zq_pr ON r9.zq_rows USING(r9.zq_helper()); "
    "CREATE FUNCTION clinic_app.scheduling_r9_root() RETURNS integer "
    "LANGUAGE sql VOLATILE AS $$ SELECT v FROM r9.zq_rows LIMIT 1 $$; "
    "GRANT USAGE ON SCHEMA r9 TO clinic_owner; "
    "GRANT ALL ON ALL TABLES IN SCHEMA r9 TO clinic_owner; ",
    "DROP FUNCTION clinic_app.scheduling_r9_root(); DROP SCHEMA r9 CASCADE;",
    "SELECT clinic_app.scheduling_r9_root()=1 AND "
    "(SELECT count(*)=1 AND bool_and(at IS NOT NULL) FROM r9.zq_journal)",
    owner_expected=(True,),
)

"""Keep operation kind and the action payload binding immutable after enqueue."""

SQL = """
CREATE FUNCTION clinic_app.comms_action_binding_guard()
RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
BEGIN
 IF ROW(NEW.kind, NEW.payload_digest) IS DISTINCT FROM ROW(OLD.kind, OLD.payload_digest)
 THEN RAISE EXCEPTION 'operation action binding is immutable' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.comms_action_binding_guard() FROM PUBLIC;
CREATE TRIGGER comms_action_binding BEFORE UPDATE
 ON clinic_app.comms_integrationoperation
 FOR EACH ROW EXECUTE FUNCTION clinic_app.comms_action_binding_guard();
"""

REVERSE_SQL = """
DROP TRIGGER comms_action_binding ON clinic_app.comms_integrationoperation;
DROP FUNCTION clinic_app.comms_action_binding_guard();
"""

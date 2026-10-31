# ruff: noqa: S608 - migration-owned identifiers only; no caller input reaches SQL
"""Frozen SQL for the omnichannel inbox: integrity, RLS, ACLs, guards, resolver.

Every rule that Python enforces is repeated here where PostgreSQL can: clinic
coherence, immutable thread identity, the WhatsApp service window, approved
template and purpose fit, and the inbound-only-by-provider-principal rule.

Time is the database's own: a message's ``created_at`` is overwritten with the
statement time, so a caller can never back-date a send into an open window. The
window opens only when ``comms_open_window`` stamps ``last_inbound_at`` from an
inbound message that proved the provider secret (``comms_ingest_account``); the
runtime role holds no UPDATE on that column and cannot insert it. The roles
``clinic_owner`` and ``clinic_resolver`` (fixtures, definers) are not the runtime
role, so the runtime-only rules key on ``current_user = 'clinic_app'``.
"""

from apps.comms.rls import INBOX_TABLES, apply_comms_rls

_APP = "clinic_app"
_DELETE_FN = "comms_inbox_reject_delete_v1"
_CLINIC_FK_TABLES = (
    "comms_provideraccount",
    "comms_templateversion",
    "comms_quickreply",
    "comms_conversation",
)

_INTEGRITY = (
    "".join(
        f"""
ALTER TABLE {_APP}.{table}
    ADD CONSTRAINT {table}_org_clinic_fk
    FOREIGN KEY (organization_id, clinic_id)
    REFERENCES {_APP}.identity_clinic (organization_id, id) NOT DEFERRABLE;
"""
        for table in _CLINIC_FK_TABLES
    )
    + f"""
ALTER TABLE {_APP}.comms_conversation
    ADD CONSTRAINT comms_conversation_enrollment_fk
    FOREIGN KEY (organization_id, clinic_id, enrollment_id)
    REFERENCES {_APP}.intake_patientclinicenrollment (organization_id, clinic_id, id)
    NOT DEFERRABLE;
ALTER TABLE {_APP}.comms_conversation
    ADD CONSTRAINT comms_conversation_account_fk
    FOREIGN KEY (organization_id, clinic_id, provider_account_id)
    REFERENCES {_APP}.comms_provideraccount (organization_id, clinic_id, id)
    NOT DEFERRABLE;
"""
)

_GUARDS = f"""
CREATE FUNCTION {_APP}.comms_conversation_guard_v1()
RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, {_APP}, pg_temp AS $f$
BEGIN
 IF TG_OP = 'INSERT' THEN
  IF current_user = 'clinic_app' AND NEW.last_inbound_at IS NOT NULL THEN
   RAISE EXCEPTION 'the service window is opened by inbound messages only'
    USING ERRCODE='23514';
  END IF;
 END IF;
 IF TG_OP = 'UPDATE' THEN
  IF current_user = 'clinic_app'
     AND NEW.last_inbound_at IS DISTINCT FROM OLD.last_inbound_at
  THEN RAISE EXCEPTION 'the service window is opened by inbound messages only'
       USING ERRCODE='23514';
  END IF;
  IF ROW(NEW.id, NEW.organization_id, NEW.clinic_id, NEW.provider_account_id,
         NEW.channel, NEW.contact_fingerprint, NEW.created_at)
     IS DISTINCT FROM ROW(OLD.id, OLD.organization_id, OLD.clinic_id,
         OLD.provider_account_id, OLD.channel, OLD.contact_fingerprint,
         OLD.created_at)
  THEN RAISE EXCEPTION 'conversation identity is immutable' USING ERRCODE='23514';
  END IF;
  -- Marketing never changes; a transactional thread becomes care only when the
  -- patient link is verified in the same update.
  IF NEW.purpose IS DISTINCT FROM OLD.purpose AND NOT (
       OLD.purpose = 'transactional' AND NEW.purpose = 'care'
       AND OLD.enrollment_id IS NULL AND NEW.enrollment_id IS NOT NULL)
  THEN RAISE EXCEPTION 'conversation purpose is fixed' USING ERRCODE='23514';
  END IF;
  IF OLD.enrollment_id IS NOT NULL
     AND NEW.enrollment_id IS DISTINCT FROM OLD.enrollment_id
  THEN RAISE EXCEPTION 'patient link is fixed' USING ERRCODE='23514';
  END IF;
  IF OLD.last_inbound_at IS NOT NULL AND (NEW.last_inbound_at IS NULL
     OR NEW.last_inbound_at < OLD.last_inbound_at)
  THEN RAISE EXCEPTION 'service window never moves back' USING ERRCODE='23514';
  END IF;
 END IF;
 RETURN NEW;
END $f$;
REVOKE ALL ON FUNCTION {_APP}.comms_conversation_guard_v1()
    FROM PUBLIC, clinic_app, clinic_resolver;
CREATE TRIGGER comms_conversation_guard BEFORE INSERT OR UPDATE
    ON {_APP}.comms_conversation
    FOR EACH ROW EXECUTE FUNCTION {_APP}.comms_conversation_guard_v1();

CREATE FUNCTION {_APP}.comms_message_guard_v1()
RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, {_APP}, pg_temp AS $f$
DECLARE c record; t record; actor uuid;
BEGIN
 IF TG_OP = 'UPDATE' THEN
  IF ROW(NEW.id, NEW.organization_id, NEW.conversation_id, NEW.direction, NEW.body,
         NEW.template_id, NEW.sent_by_id, NEW.command_id, NEW.event_id,
         NEW.template_parameters, NEW.supported, NEW.created_at)
     IS DISTINCT FROM ROW(OLD.id, OLD.organization_id, OLD.conversation_id,
         OLD.direction, OLD.body, OLD.template_id, OLD.sent_by_id, OLD.command_id,
         OLD.event_id, OLD.template_parameters, OLD.supported, OLD.created_at)
  THEN RAISE EXCEPTION 'message is immutable' USING ERRCODE='23514';
  END IF;
  RETURN NEW;
 END IF;
 NEW.created_at := statement_timestamp();
 SELECT * INTO c FROM {_APP}.comms_conversation WHERE id = NEW.conversation_id;
 IF c.id IS NULL OR c.organization_id <> NEW.organization_id THEN
  RAISE EXCEPTION 'conversation not visible' USING ERRCODE='42501';
 END IF;
 actor := NULLIF(current_setting('app.current_user_id', true), '')::uuid;
 IF NEW.direction = 'in' THEN
  -- Only the conversation's own provider account, proven by its secret,
  -- receives messages; the definer stamps the window from the database clock.
  IF actor IS NOT NULL OR c.provider_account_id IS NULL
     OR {_APP}.comms_ingest_account(c.clinic_id, c.channel)
        IS DISTINCT FROM c.provider_account_id
  THEN RAISE EXCEPTION 'inbound requires the provider principal'
       USING ERRCODE='42501';
  END IF;
  PERFORM {_APP}.comms_open_window(c.id);
  RETURN NEW;
 END IF;
 IF actor IS NULL OR NEW.sent_by_id IS DISTINCT FROM actor THEN
  RAISE EXCEPTION 'outbound requires the signed-in sender' USING ERRCODE='42501';
 END IF;
 IF c.state = 'closed' THEN
  RAISE EXCEPTION 'conversation is closed' USING ERRCODE='23514';
 END IF;
 IF NEW.template_id IS NULL THEN
  IF c.purpose = 'marketing' THEN
   RAISE EXCEPTION 'marketing requires an approved template' USING ERRCODE='23514';
  END IF;
  IF c.channel = 'whatsapp' AND (c.last_inbound_at IS NULL
     OR NEW.created_at < c.last_inbound_at
     OR NEW.created_at >= c.last_inbound_at + interval '24 hours')
  -- created_at is the statement time set above, never the caller's value.
  THEN RAISE EXCEPTION 'service window closed; template required'
       USING ERRCODE='23514';
  END IF;
  RETURN NEW;
 END IF;
 SELECT * INTO t FROM {_APP}.comms_templateversion WHERE id = NEW.template_id;
 IF t.id IS NULL OR t.organization_id <> NEW.organization_id
    OR t.clinic_id <> c.clinic_id OR t.channel <> c.channel
    OR t.approval_status <> 'approved'
 THEN RAISE EXCEPTION 'template is not approved for this conversation'
      USING ERRCODE='23514';
 END IF;
 IF NOT ((t.category = 'marketing' AND c.purpose = 'marketing')
      OR (t.category = 'utility' AND c.purpose IN ('transactional', 'care'))
      OR (t.category = 'authentication' AND c.purpose = 'transactional'))
 THEN RAISE EXCEPTION 'template does not fit the conversation purpose'
      USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $f$;
REVOKE ALL ON FUNCTION {_APP}.comms_message_guard_v1()
    FROM PUBLIC, clinic_app, clinic_resolver;
CREATE TRIGGER comms_message_guard BEFORE INSERT OR UPDATE
    ON {_APP}.comms_message
    FOR EACH ROW EXECUTE FUNCTION {_APP}.comms_message_guard_v1();

CREATE FUNCTION {_APP}.comms_template_guard_v1()
RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, {_APP}, pg_temp AS $f$
BEGIN
 IF TG_OP = 'INSERT' THEN
  -- A runtime insert is always a draft; approval is a later, attributed step.
  IF current_user = 'clinic_app'
     AND (NEW.approval_status <> 'draft' OR NEW.approval_changed_at IS NOT NULL)
  THEN RAISE EXCEPTION 'a template version starts as a draft'
       USING ERRCODE='23514';
  END IF;
  RETURN NEW;
 END IF;
 IF ROW(NEW.id, NEW.organization_id, NEW.clinic_id, NEW.channel, NEW.name,
        NEW.language, NEW.version, NEW.category, NEW.purpose, NEW.body,
        NEW.parameter_count, NEW.content_digest, NEW.created_by_id, NEW.created_at)
    IS DISTINCT FROM ROW(OLD.id, OLD.organization_id, OLD.clinic_id, OLD.channel,
        OLD.name, OLD.language, OLD.version, OLD.category, OLD.purpose, OLD.body,
        OLD.parameter_count, OLD.content_digest, OLD.created_by_id, OLD.created_at)
 THEN RAISE EXCEPTION 'template version is immutable' USING ERRCODE='23514';
 END IF;
 IF NEW.approval_status IS DISTINCT FROM OLD.approval_status THEN
  IF NOT (
      (OLD.approval_status = 'draft' AND NEW.approval_status = 'pending')
   OR (OLD.approval_status = 'pending'
       AND NEW.approval_status IN ('approved', 'rejected'))
   OR (OLD.approval_status = 'approved'
       AND NEW.approval_status IN ('paused', 'disabled'))
   OR (OLD.approval_status = 'paused'
       AND NEW.approval_status IN ('approved', 'disabled')))
  THEN RAISE EXCEPTION 'invalid template approval transition'
       USING ERRCODE='23514';
  END IF;
  IF current_user = 'clinic_app' THEN
   -- WhatsApp templates are approved only by the provider's authenticated
   -- status event; email and SMS only by a signed-in staff member.
   IF OLD.channel = 'whatsapp' THEN
    IF {_APP}.comms_ingest_account(NEW.clinic_id, NEW.channel) IS NULL THEN
     RAISE EXCEPTION 'provider approval required' USING ERRCODE='42501';
    END IF;
   ELSIF NULLIF(current_setting('app.current_user_id', true), '') IS NULL THEN
    RAISE EXCEPTION 'staff approval required' USING ERRCODE='42501';
   END IF;
  END IF;
  NEW.approval_changed_at := statement_timestamp();
 END IF;
 RETURN NEW;
END $f$;
REVOKE ALL ON FUNCTION {_APP}.comms_template_guard_v1()
    FROM PUBLIC, clinic_app, clinic_resolver;
CREATE TRIGGER comms_template_guard BEFORE INSERT OR UPDATE
    ON {_APP}.comms_templateversion
    FOR EACH ROW EXECUTE FUNCTION {_APP}.comms_template_guard_v1();

CREATE FUNCTION {_APP}.{_DELETE_FN}()
RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, {_APP} AS $f$
BEGIN
 RAISE EXCEPTION 'inbox records are never hard-deleted' USING ERRCODE='23514';
END $f$;
REVOKE ALL ON FUNCTION {_APP}.{_DELETE_FN}()
    FROM PUBLIC, clinic_app, clinic_resolver;
""" + "".join(
    f"""
CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {_APP}.{table}
    FOR EACH ROW EXECUTE FUNCTION {_APP}.{_DELETE_FN}();
"""
    for table in INBOX_TABLES
)

_SELECT_INSERT = tuple(t for t in INBOX_TABLES if t != "comms_provideraccount")
_ACL = (
    "".join(
        f"REVOKE ALL ON TABLE {_APP}.{table} FROM PUBLIC, clinic_app, clinic_agent;\n"
        for table in INBOX_TABLES
    )
    + f"GRANT SELECT ON TABLE {_APP}.comms_provideraccount TO clinic_app;\n"
    + "".join(
        f"GRANT SELECT, INSERT ON TABLE {_APP}.{table} TO clinic_app;\n"
        for table in _SELECT_INSERT
    )
    + f"""
GRANT UPDATE (approval_status, approval_changed_at)
    ON TABLE {_APP}.comms_templateversion TO clinic_app;
GRANT UPDATE (active) ON TABLE {_APP}.comms_quickreply TO clinic_app;
GRANT UPDATE (state, assignee_id, enrollment_id, purpose, needs_clinician,
    triage_category, last_activity_at, revision, updated_at,
    contact_label) ON TABLE {_APP}.comms_conversation TO clinic_app;
GRANT UPDATE (provider_state) ON TABLE {_APP}.comms_message TO clinic_app;
GRANT UPDATE (state, decided_by_id, used_message_id)
    ON TABLE {_APP}.comms_suggestedreply TO clinic_app;
GRANT SELECT ON TABLE {_APP}.comms_provideraccount,
    {_APP}.identity_serviceprincipal TO clinic_resolver;
GRANT SELECT ON TABLE {_APP}.comms_conversation TO clinic_resolver;
GRANT UPDATE (last_inbound_at, last_activity_at)
    ON TABLE {_APP}.comms_conversation TO clinic_resolver;
"""
)
_RESOLVER = f"""
SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION {_APP}.comms_resolve_inbound(
    account pg_catalog.uuid, proof pg_catalog.text)
RETURNS TABLE(organization_id pg_catalog.uuid, clinic_id pg_catalog.uuid,
    principal_id pg_catalog.uuid, channel pg_catalog.text,
    provider pg_catalog.text, external_account_id pg_catalog.text)
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, {_APP}, pg_temp AS $f$
 SELECT a.organization_id, a.clinic_id, a.principal_id, a.channel, a.provider,
        a.external_account_id
 FROM {_APP}.comms_provideraccount a
 JOIN {_APP}.identity_serviceprincipal p ON p.id = a.principal_id
  AND p.organization_id = a.organization_id AND p.clinic_id = a.clinic_id
 WHERE a.id = account AND a.active AND p.active
   AND p.purpose = 'messaging_ingest'
   AND pg_catalog.length(proof) BETWEEN 16 AND 256
   AND a.signing_key_digest =
       pg_catalog.encode({_APP}.digest(proof, 'sha256'), 'hex')
   AND NULLIF(current_setting('app.current_user_id', true), '') IS NULL
   AND NULLIF(current_setting('app.current_patient_session', true), '') IS NULL
   AND NULLIF(current_setting('app.current_principal', true), '') IS NULL
$f$;
REVOKE ALL ON FUNCTION {_APP}.comms_resolve_inbound(pg_catalog.uuid, pg_catalog.text)
    FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
    {_APP}.comms_resolve_inbound(pg_catalog.uuid, pg_catalog.text) TO clinic_app;

CREATE FUNCTION {_APP}.comms_ingest_account(
    clinic pg_catalog.uuid, channel_name pg_catalog.text)
RETURNS pg_catalog.uuid
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, {_APP}, pg_temp AS $f$
 SELECT a.id
 FROM {_APP}.comms_provideraccount a
 JOIN {_APP}.identity_serviceprincipal p ON p.id = a.principal_id
  AND p.organization_id = a.organization_id AND p.clinic_id = a.clinic_id
 WHERE a.clinic_id = clinic AND a.channel = channel_name
   AND a.active AND p.active AND p.purpose = 'messaging_ingest'
   AND a.organization_id =
       NULLIF(current_setting('app.current_tenant', true), '')::pg_catalog.uuid
   AND a.principal_id =
       NULLIF(current_setting('app.current_principal', true), '')::pg_catalog.uuid
   AND NULLIF(current_setting('app.current_user_id', true), '') IS NULL
   AND NULLIF(current_setting('app.current_patient_session', true), '') IS NULL
   AND a.signing_key_digest = pg_catalog.encode(
       {_APP}.digest(current_setting('app.comms_inbound_proof', true), 'sha256'),
       'hex')
 LIMIT 1
$f$;
REVOKE ALL ON FUNCTION {_APP}.comms_ingest_account(pg_catalog.uuid, pg_catalog.text)
    FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
    {_APP}.comms_ingest_account(pg_catalog.uuid, pg_catalog.text) TO clinic_app;

CREATE FUNCTION {_APP}.comms_open_window(conversation pg_catalog.uuid)
RETURNS void
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, {_APP}, pg_temp AS $f$
BEGIN
 UPDATE {_APP}.comms_conversation c
    SET last_inbound_at = statement_timestamp(),
        last_activity_at = statement_timestamp()
  WHERE c.id = conversation
    AND c.provider_account_id =
        {_APP}.comms_ingest_account(c.clinic_id, c.channel);
 IF NOT FOUND THEN
  RAISE EXCEPTION 'inbound requires the provider principal' USING ERRCODE='42501';
 END IF;
END $f$;
REVOKE ALL ON FUNCTION {_APP}.comms_open_window(pg_catalog.uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION {_APP}.comms_open_window(pg_catalog.uuid) TO clinic_app;
RESET ROLE;
"""
_RLS = "".join(apply_comms_rls(table, "organization_id") for table in INBOX_TABLES)
SQL = _INTEGRITY + _GUARDS + _RLS + _ACL + _RESOLVER

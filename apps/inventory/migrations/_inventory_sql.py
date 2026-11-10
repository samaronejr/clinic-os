"""Frozen Inventory v1 scope, serialization and append-only contract."""

TABLES = (
    "inventory_supplier",
    "inventory_item",
    "inventory_batch",
    "inventory_location",
    "inventory_minimumstock",
    "inventory_purchasebudget",
    "inventory_purchaserequest",
    "inventory_stockmovement",
    "inventory_equipment",
    "inventory_calibration",
    "inventory_maintenance",
    "inventory_commandreceipt",
)
# Staff guards refuse an active machine context (identity README, service
# principals). The shared permission evaluator reads only the actor and tenant,
# so every Inventory decision that consults has_permission conjoins this itself.
NO_MACHINE_CONTEXT = "NULLIF(current_setting('app.current_principal',true),'') IS NULL"

FUNCTIONS = """
SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.inventory_today(clinic uuid)
RETURNS date LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT (statement_timestamp() AT TIME ZONE c.timezone)::date
 FROM clinic_app.identity_clinic c WHERE c.id=clinic
 AND c.organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid
 AND __NO_MACHINE__ AND clinic_app.has_permission('inventory.read',clinic,NULL)
$f$;

CREATE FUNCTION clinic_app.inventory_batch_allowed(batch uuid,clinic uuid)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT __NO_MACHINE__ AND clinic_app.has_permission('inventory.read',clinic,NULL)
 AND EXISTS(
 SELECT 1 FROM clinic_app.inventory_batch b WHERE b.id=batch
 AND b.organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid
 AND (b.clinic_id=clinic OR EXISTS(
 SELECT 1 FROM clinic_app.inventory_stockmovement m
 JOIN clinic_app.inventory_location l ON l.id=m.destination_location_id
 WHERE m.batch_id=b.id AND m.kind='transfer' AND l.clinic_id=clinic
 AND l.organization_id=b.organization_id AND m.organization_id=b.organization_id)))
$f$;

CREATE FUNCTION clinic_app.inventory_sum(batch uuid,location uuid)
RETURNS bigint LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT coalesce(sum(
 CASE WHEN m.location_id=location THEN
 CASE WHEN m.kind IN ('receipt','return') THEN m.quantity::bigint
 ELSE -m.quantity::bigint END ELSE 0 END
 + CASE WHEN m.destination_location_id=location THEN m.quantity::bigint ELSE 0 END
 ),0)::bigint FROM clinic_app.inventory_stockmovement m
 WHERE m.batch_id=batch
 AND m.organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid
 AND (m.location_id=location OR m.destination_location_id=location)
$f$;

CREATE FUNCTION clinic_app.inventory_balance(batch uuid,location uuid)
RETURNS bigint LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE clinic uuid;
BEGIN
 SELECT l.clinic_id INTO clinic FROM clinic_app.inventory_location l
 WHERE l.id=location AND l.organization_id=NULLIF(
 current_setting('app.current_tenant',true),'')::uuid;
 IF clinic IS NULL OR NOT clinic_app.inventory_batch_allowed(batch,clinic) THEN
 RAISE EXCEPTION 'inventory_denied' USING ERRCODE='42501'; END IF;
 RETURN clinic_app.inventory_sum(batch,location);
END $f$;

CREATE FUNCTION clinic_app.inventory_guard()
RETURNS trigger LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE actor uuid := NULLIF(current_setting('app.current_user_id',true),'')::uuid;
 tenant uuid := NULLIF(current_setting('app.current_tenant',true),'')::uuid;
 runtime boolean := current_setting('role') IN ('clinic_app','clinic_agent')
 OR (current_setting('role')='none' AND session_user IN ('clinic_app','clinic_agent'));
 row_data jsonb; parent jsonb; field_name text; parent_table text;
 permission text := 'inventory.manage'; b clinic_app.inventory_batch;
 source clinic_app.inventory_stockmovement; purchase
 clinic_app.inventory_purchaserequest;
 destination clinic_app.inventory_location; spent bigint;
BEGIN
 IF TG_OP IN ('DELETE','TRUNCATE') OR (TG_OP='UPDATE'
 AND TG_TABLE_NAME<>'inventory_purchaserequest') THEN
 RAISE EXCEPTION 'inventory_immutable' USING ERRCODE='23514'; END IF;
 IF NEW.organization_id IS DISTINCT FROM tenant OR NOT EXISTS(
 SELECT 1 FROM clinic_app.identity_clinic c WHERE c.id=NEW.clinic_id
 AND c.organization_id=tenant) THEN
 RAISE EXCEPTION 'inventory_denied' USING ERRCODE='42501'; END IF;
 -- One organization lock also orders cross-clinic transfer and budget writes.
 -- VOLATILE queries after the lock reload committed facts at READ COMMITTED.
 PERFORM pg_advisory_xact_lock(hashtextextended('inventory:'||tenant::text,0));
 IF runtime AND current_setting('transaction_isolation')<>'read committed' THEN
 RAISE EXCEPTION 'inventory_isolation' USING ERRCODE='23514'; END IF;
 row_data:=to_jsonb(NEW);
 IF TG_TABLE_NAME='inventory_purchaserequest'
 THEN permission:='inventory.use'; END IF;
 IF TG_TABLE_NAME='inventory_commandreceipt' THEN
  IF NEW.operation IN (
  'use','return_stock','create_purchase_request','propose_replenishment')
  THEN permission:='inventory.use'; END IF;
 END IF;
 IF TG_TABLE_NAME='inventory_stockmovement' THEN
  IF NEW.kind IN ('use','return') THEN permission:='inventory.use'; END IF;
 END IF;
 IF TG_OP='UPDATE' THEN permission:='inventory.manage'; END IF;
 IF runtime AND NOT (__NO_MACHINE__
 AND clinic_app.has_permission(permission,NEW.clinic_id,NULL)) THEN
 RAISE EXCEPTION 'inventory_denied' USING ERRCODE='42501'; END IF;
 -- Every foreign binding is checked under the resolver, never around RLS
 -- from Python. Optional absent fields do not weaken populated bindings.
 FOR field_name,parent_table IN SELECT * FROM (VALUES
 ('item_id','inventory_item'),('supplier_id','inventory_supplier'),
 ('location_id','inventory_location'),('budget_id','inventory_purchasebudget'),
 ('equipment_id','inventory_equipment')) AS bindings(field_name,parent_table)
 LOOP
  IF row_data->>field_name IS NOT NULL THEN
   SELECT CASE parent_table
    WHEN 'inventory_item' THEN (SELECT to_jsonb(p) FROM clinic_app.inventory_item p
      WHERE p.id=(row_data->>field_name)::uuid)
    WHEN 'inventory_supplier' THEN (SELECT to_jsonb(p) FROM
 clinic_app.inventory_supplier p
      WHERE p.id=(row_data->>field_name)::uuid)
    WHEN 'inventory_location' THEN (SELECT to_jsonb(p) FROM
 clinic_app.inventory_location p
      WHERE p.id=(row_data->>field_name)::uuid)
    WHEN 'inventory_purchasebudget' THEN (SELECT to_jsonb(p)
      FROM clinic_app.inventory_purchasebudget p WHERE
 p.id=(row_data->>field_name)::uuid)
    WHEN 'inventory_equipment' THEN (SELECT to_jsonb(p) FROM
 clinic_app.inventory_equipment p
      WHERE p.id=(row_data->>field_name)::uuid)
   END INTO parent;
   IF parent IS NULL OR parent->>'organization_id'<>tenant::text
   OR parent->>'clinic_id'<>NEW.clinic_id::text THEN
    RAISE EXCEPTION 'inventory_denied' USING ERRCODE='42501';
   END IF;
  END IF;
 END LOOP;
 IF TG_TABLE_NAME='inventory_batch' THEN
  IF NEW.lot='' OR length(NEW.lot)>120 THEN
   RAISE EXCEPTION 'inventory_invalid' USING ERRCODE='23514';
  END IF;
 END IF;
 IF row_data ? 'quantity' AND
 ((row_data->>'quantity')::bigint NOT BETWEEN 1 AND 1000000000) THEN
 RAISE EXCEPTION 'inventory_quantity' USING ERRCODE='23514'; END IF;
 IF row_data ? 'amount_minor' AND
 ((row_data->>'amount_minor')::bigint NOT BETWEEN 1 AND 9000000000000000) THEN
 RAISE EXCEPTION 'inventory_amount' USING ERRCODE='23514'; END IF;

 IF TG_TABLE_NAME='inventory_purchasebudget' THEN
  IF NOT EXISTS(SELECT 1 FROM clinic_app.identity_userclinicrole r
   JOIN clinic_app.identity_user u ON u.id=r.user_id AND u.is_active
   WHERE r.user_id=NEW.approved_by_id AND r.clinic_id=NEW.clinic_id
   AND r.organization_id=tenant AND r.revoked_at IS NULL
   AND r.role IN ('clinic_manager','clinic_admin'))
  OR (runtime AND NEW.approved_by_id IS DISTINCT FROM actor) THEN
   RAISE EXCEPTION 'inventory_denied' USING ERRCODE='42501'; END IF;
  IF runtime THEN NEW.approved_at:=statement_timestamp(); END IF;
 ELSIF TG_TABLE_NAME='inventory_purchaserequest' THEN
  IF TG_OP='INSERT' THEN
   IF NEW.state<>'draft' OR NEW.approved_by_id IS NOT NULL
   OR NEW.approved_at IS NOT NULL OR NEW.last_command_id IS NOT NULL THEN
    RAISE EXCEPTION 'purchase_transition' USING ERRCODE='23514'; END IF;
  ELSE
   IF (to_jsonb(NEW)-ARRAY['state','approved_by_id','approved_at','last_command_id'])
    IS DISTINCT FROM
    (to_jsonb(OLD)-ARRAY['state','approved_by_id','approved_at','last_command_id'])
   OR NEW.last_command_id IS NULL OR NEW.last_command_id=OLD.last_command_id
   OR NOT ((OLD.state='draft' AND NEW.state='approved') OR
       (OLD.state='approved' AND NEW.state='received')) THEN
    RAISE EXCEPTION 'purchase_transition' USING ERRCODE='23514'; END IF;
   IF NEW.state='approved' THEN
    IF NEW.approved_by_id IS DISTINCT FROM actor THEN
     RAISE EXCEPTION 'inventory_denied' USING ERRCODE='42501'; END IF;
    NEW.approved_at:=statement_timestamp();
   ELSE
    IF NEW.approved_by_id IS DISTINCT FROM OLD.approved_by_id
    OR NEW.approved_at IS DISTINCT FROM OLD.approved_at OR NOT EXISTS(
     SELECT 1 FROM clinic_app.inventory_stockmovement m
     WHERE m.purchase_request_id=NEW.id AND m.kind='receipt'
      AND m.quantity=NEW.quantity AND m.command_id=NEW.last_command_id) THEN
     RAISE EXCEPTION 'purchase_not_approved' USING ERRCODE='23514'; END IF;
   END IF;
  END IF;
  IF NEW.budget_id IS NOT NULL AND
   (TG_OP='INSERT' OR NEW.state='approved') THEN
   SELECT coalesce(sum(p.amount_minor),0) INTO spent
   FROM clinic_app.inventory_purchaserequest p WHERE p.budget_id=NEW.budget_id
   AND p.state IN ('approved','received') AND p.id<>NEW.id;
   IF spent+NEW.amount_minor>(SELECT amount_minor
     FROM clinic_app.inventory_purchasebudget WHERE id=NEW.budget_id) THEN
    RAISE EXCEPTION 'budget_exceeded' USING ERRCODE='23514'; END IF;
  END IF;
 ELSIF TG_TABLE_NAME IN ('inventory_calibration','inventory_maintenance') THEN
  IF NEW.performed_on>(SELECT
   (statement_timestamp() AT TIME ZONE c.timezone)::date
   FROM clinic_app.identity_clinic c WHERE c.id=NEW.clinic_id)
   OR NEW.due_on<NEW.performed_on OR
   (TG_TABLE_NAME='inventory_calibration' AND NEW.outcome NOT IN ('passed','failed'))
   OR (TG_TABLE_NAME='inventory_maintenance'
   AND NEW.outcome NOT IN ('completed','failed')) THEN
   RAISE EXCEPTION 'inventory_invalid' USING ERRCODE='23514'; END IF;
 ELSIF TG_TABLE_NAME='inventory_commandreceipt' THEN
  IF NEW.operation NOT IN ('receive','use','return_stock','write_off','transfer',
   'create_purchase_request','propose_replenishment','approve_purchase',
   'receive_purchase','approve_budget','register_equipment','record_calibration',
   'record_maintenance','create_item','create_batch','create_location',
   'create_supplier','set_minimum_stock')
  OR NEW.object_type NOT IN ('stockmovement','purchaserequest','purchasebudget',
   'equipment','calibration','maintenance','item','batch','location','supplier',
   'minimumstock') OR NEW.recorded_by_id IS DISTINCT FROM actor THEN
   RAISE EXCEPTION 'inventory_invalid' USING ERRCODE='23514'; END IF;
  SELECT CASE NEW.object_type
   WHEN 'item' THEN (SELECT to_jsonb(p) FROM clinic_app.inventory_item p WHERE
 p.id=NEW.object_id)
   WHEN 'batch' THEN (SELECT to_jsonb(p) FROM clinic_app.inventory_batch p WHERE
 p.id=NEW.object_id)
   WHEN 'location' THEN (SELECT to_jsonb(p) FROM clinic_app.inventory_location p WHERE
 p.id=NEW.object_id)
   WHEN 'supplier' THEN (SELECT to_jsonb(p) FROM clinic_app.inventory_supplier p WHERE
 p.id=NEW.object_id)
   WHEN 'minimumstock' THEN (SELECT to_jsonb(p) FROM clinic_app.inventory_minimumstock
 p WHERE p.id=NEW.object_id)
   WHEN 'stockmovement' THEN (SELECT to_jsonb(p) FROM
 clinic_app.inventory_stockmovement p WHERE p.id=NEW.object_id)
   WHEN 'purchaserequest' THEN (SELECT to_jsonb(p) FROM
 clinic_app.inventory_purchaserequest p WHERE p.id=NEW.object_id)
   WHEN 'purchasebudget' THEN (SELECT to_jsonb(p) FROM
 clinic_app.inventory_purchasebudget p WHERE p.id=NEW.object_id)
   WHEN 'equipment' THEN (SELECT to_jsonb(p) FROM clinic_app.inventory_equipment p
 WHERE p.id=NEW.object_id)
   WHEN 'calibration' THEN (SELECT to_jsonb(p) FROM clinic_app.inventory_calibration p
 WHERE p.id=NEW.object_id)
   WHEN 'maintenance' THEN (SELECT to_jsonb(p) FROM clinic_app.inventory_maintenance p
 WHERE p.id=NEW.object_id)
  END INTO parent;
  IF parent IS NULL OR parent->>'organization_id'<>tenant::text OR
   parent->>'clinic_id'<>NEW.clinic_id::text THEN
   RAISE EXCEPTION 'inventory_denied' USING ERRCODE='42501'; END IF;
 END IF;
 IF TG_TABLE_NAME='inventory_stockmovement' THEN
  SELECT * INTO b FROM clinic_app.inventory_batch WHERE id=NEW.batch_id;
  IF b.id IS NULL OR b.organization_id<>tenant
  OR NOT clinic_app.inventory_batch_allowed(b.id,NEW.clinic_id)
  OR NEW.kind NOT IN ('receipt','use','return','write_off','transfer')
  OR NEW.recorded_by_id IS DISTINCT FROM actor
  OR (NEW.kind<>'write_off' AND NEW.reason<>'')
  OR (NEW.kind='write_off' AND NEW.reason NOT IN ('damaged','expired','lost'))
  OR (NEW.kind<>'return' AND NEW.source_movement_id IS NOT NULL)
  OR (NEW.kind<>'use' AND NEW.procedure_entry_id IS NOT NULL)
  OR (NEW.kind<>'receipt' AND NEW.purchase_request_id IS NOT NULL) THEN
   RAISE EXCEPTION 'inventory_denied' USING ERRCODE='42501'; END IF;
  IF NEW.kind IN ('use','transfer') AND
   b.expires_on<=clinic_app.inventory_today(NEW.clinic_id) THEN
   RAISE EXCEPTION 'batch_expired' USING ERRCODE='23514'; END IF;
  IF NEW.kind IN ('use','write_off','transfer') AND
   clinic_app.inventory_sum(b.id,NEW.location_id)<NEW.quantity THEN
   RAISE EXCEPTION 'insufficient_stock' USING ERRCODE='23514'; END IF;
  IF NEW.procedure_entry_id IS NOT NULL AND NOT EXISTS(
   SELECT 1 FROM clinic_app.ehr_specialtyentry p WHERE p.id=NEW.procedure_entry_id
   AND p.kind='procedure' AND p.organization_id=tenant AND p.clinic_id=NEW.clinic_id)
  THEN RAISE EXCEPTION 'inventory_denied' USING ERRCODE='42501'; END IF;
  IF NEW.kind='return' THEN
   SELECT * INTO source FROM clinic_app.inventory_stockmovement m
    WHERE m.id=NEW.source_movement_id AND m.organization_id=tenant
    AND m.clinic_id=NEW.clinic_id AND m.kind='use';
   IF source.id IS NULL OR source.batch_id<>b.id
    OR source.location_id<>NEW.location_id THEN
    RAISE EXCEPTION 'inventory_denied' USING ERRCODE='42501'; END IF;
   IF NEW.quantity+coalesce((SELECT sum(m.quantity)
    FROM clinic_app.inventory_stockmovement m
    WHERE m.source_movement_id=source.id),0)>source.quantity THEN
    RAISE EXCEPTION 'return_exceeds_use' USING ERRCODE='23514'; END IF;
  END IF;
  IF NEW.kind='transfer' THEN
   SELECT * INTO destination FROM clinic_app.inventory_location l
    WHERE l.id=NEW.destination_location_id AND l.organization_id=tenant;
   IF destination.id IS NULL OR destination.id=NEW.location_id THEN
    RAISE EXCEPTION 'inventory_denied' USING ERRCODE='42501'; END IF;
   IF destination.clinic_id<>NEW.clinic_id AND NOT (__NO_MACHINE__
    AND clinic_app.has_permission('inventory.transfer_org',NEW.clinic_id,NULL)
    AND clinic_app.has_permission('inventory.transfer_org',destination.clinic_id,NULL)
    AND clinic_app.has_permission('inventory.manage',destination.clinic_id,NULL))
   THEN RAISE EXCEPTION 'inventory_denied' USING ERRCODE='42501'; END IF;
  ELSIF NEW.destination_location_id IS NOT NULL THEN
   RAISE EXCEPTION 'inventory_invalid' USING ERRCODE='23514';
  END IF;
  IF NEW.purchase_request_id IS NOT NULL THEN
   SELECT * INTO purchase FROM clinic_app.inventory_purchaserequest p
   WHERE p.id=NEW.purchase_request_id AND p.organization_id=tenant
    AND p.clinic_id=NEW.clinic_id;
   IF purchase.id IS NULL OR purchase.state<>'approved'
    OR purchase.item_id<>b.item_id OR purchase.quantity<>NEW.quantity THEN
    RAISE EXCEPTION 'purchase_not_approved' USING ERRCODE='23514'; END IF;
  END IF;
 END IF;
 RETURN NEW;
END $f$;

CREATE FUNCTION clinic_app.inventory_received()
RETURNS trigger LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
BEGIN
 IF NEW.purchase_request_id IS NOT NULL THEN
  UPDATE clinic_app.inventory_purchaserequest SET state='received',
   last_command_id=NEW.command_id WHERE id=NEW.purchase_request_id;
 END IF;
 RETURN NEW;
END $f$;

CREATE FUNCTION clinic_app.inventory_no_truncate()
RETURNS trigger LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
BEGIN
 RAISE EXCEPTION 'inventory_immutable' USING ERRCODE='23514';
END $f$;
RESET ROLE;
""".replace("__NO_MACHINE__", NO_MACHINE_CONTEXT)


def contract_sql() -> str:
    """Publish the frozen model set with explicit minimum grants and policies."""
    sql = "".join(
        f"REVOKE ALL ON clinic_app.{table} FROM PUBLIC,clinic_app,clinic_agent;"
        f"GRANT SELECT,INSERT ON clinic_app.{table} TO clinic_app;"
        f"GRANT SELECT ON clinic_app.{table} TO clinic_resolver;"
        for table in TABLES
    )
    sql += """
GRANT UPDATE(state,approved_by_id,approved_at,last_command_id)
 ON clinic_app.inventory_purchaserequest TO clinic_app;
GRANT UPDATE(state,last_command_id) ON clinic_app.inventory_purchaserequest
 TO clinic_resolver;
GRANT SELECT ON clinic_app.ehr_specialtyentry TO clinic_resolver;
"""
    sql += FUNCTIONS
    for table in TABLES:
        # Clinic-facing policies; transferred lot identities are visible only
        # through an authorized destination's immutable incoming stock fact.
        read = "clinic_app.has_permission('inventory.read',clinic_id,NULL)"
        if table == "inventory_batch":
            read = (
                "(__READ__ OR EXISTS(SELECT 1 FROM clinic_app.inventory_location l"
                " WHERE clinic_app.inventory_batch_allowed("
                "inventory_batch.id,l.clinic_id)"
                " AND l.organization_id=inventory_batch.organization_id))"
            ).replace("__READ__", read)
        if table == "inventory_stockmovement":
            read = (
                "(__READ__ OR EXISTS(SELECT 1 FROM clinic_app.inventory_location l"
                " WHERE l.id=inventory_stockmovement.destination_location_id"
                " AND clinic_app.has_permission('inventory.read',l.clinic_id,NULL)))"
            ).replace("__READ__", read)
        write = "'inventory.manage'"
        if table == "inventory_stockmovement":
            write = (
                "CASE WHEN kind IN ('use','return') THEN 'inventory.use'"
                " ELSE 'inventory.manage' END"
            )
        elif table == "inventory_commandreceipt":
            write = (
                "CASE WHEN operation IN ('use','return_stock',"
                "'create_purchase_request','propose_replenishment')"
                " THEN 'inventory.use' ELSE 'inventory.manage' END"
            )
        elif table == "inventory_purchaserequest":
            write = "'inventory.use'"
        sql += f"""
ALTER TABLE clinic_app.{table} ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.{table} FORCE ROW LEVEL SECURITY;
CREATE POLICY inventory_owner ON clinic_app.{table} TO clinic_owner
 USING(organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid)
 WITH
 CHECK(organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid);
CREATE POLICY inventory_read ON clinic_app.{table} FOR SELECT TO clinic_app
 USING(organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid
 AND {NO_MACHINE_CONTEXT} AND {read});
CREATE POLICY inventory_insert ON clinic_app.{table} FOR INSERT TO clinic_app
 WITH CHECK(organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid
 AND {NO_MACHINE_CONTEXT} AND clinic_app.has_permission({write},clinic_id,NULL));
CREATE TRIGGER inventory_guard BEFORE INSERT OR UPDATE OR DELETE
 ON clinic_app.{table} FOR EACH ROW EXECUTE FUNCTION clinic_app.inventory_guard();
CREATE TRIGGER inventory_no_truncate BEFORE TRUNCATE ON clinic_app.{table}
 FOR EACH STATEMENT EXECUTE FUNCTION clinic_app.inventory_no_truncate();
"""
    sql += f"""
CREATE POLICY inventory_update ON clinic_app.inventory_purchaserequest
 FOR UPDATE TO clinic_app
 USING({NO_MACHINE_CONTEXT}
 AND clinic_app.has_permission('inventory.manage',clinic_id,NULL)
 AND organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid)
 WITH CHECK({NO_MACHINE_CONTEXT}
 AND clinic_app.has_permission('inventory.manage',clinic_id,NULL));
CREATE TRIGGER inventory_received AFTER INSERT ON clinic_app.inventory_stockmovement
 FOR EACH ROW EXECUTE FUNCTION clinic_app.inventory_received();
"""
    sql += "SET LOCAL ROLE clinic_resolver;"
    for name, signature in (
        ("inventory_today", "uuid"),
        ("inventory_batch_allowed", "uuid,uuid"),
        ("inventory_sum", "uuid,uuid"),
        ("inventory_balance", "uuid,uuid"),
        ("inventory_guard", ""),
        ("inventory_received", ""),
        ("inventory_no_truncate", ""),
    ):
        sql += f"REVOKE ALL ON FUNCTION clinic_app.{name}({signature}) FROM PUBLIC;"
        if name in {"inventory_today", "inventory_batch_allowed", "inventory_balance"}:
            sql += (
                f"GRANT EXECUTE ON FUNCTION clinic_app.{name}({signature})"
                " TO clinic_app;"
            )
    sql += "RESET ROLE;"
    return sql


SQL = contract_sql()

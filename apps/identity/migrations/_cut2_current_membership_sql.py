"""Keep the exact cut-1 bodies while excluding retained revoked membership."""

from typing import Final

SQL: Final = """SET LOCAL ROLE clinic_resolver;

CREATE OR REPLACE FUNCTION clinic_app.workflows_owned(clinic uuid, owner_user \
uuid, owner_role
text)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT clinic_app.has_permission('tasks.view',clinic,NULL) AND (
 COALESCE(owner_user=NULLIF(current_setting('app.current_user_id', true),
 '')::uuid, false)
 OR (owner_role<>'' AND EXISTS (SELECT 1 FROM clinic_app.identity_userclinicrole r
 WHERE r.revoked_at IS NULL AND r.clinic_id=clinic AND r.organization_id=NULLIF(
 current_setting('app.current_tenant',true),'')::uuid
 AND r.user_id=NULLIF(current_setting('app.current_user_id',true),'')::uuid
 AND r.role=owner_role)))
$f$;

CREATE OR REPLACE FUNCTION clinic_app.workflows_owner_valid(clinic uuid, \
owner_user uuid,
owner_role text)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT clinic_app.has_permission('tasks.assign',clinic,NULL) AND (
 (owner_user IS NULL AND owner_role=ANY(ARRAY['owner','physician','receptionist',
 'clinic_admin','nurse','allied_professional','scheduler','clinic_manager',
 'finance','org_admin']::text[])) OR (owner_role='' AND owner_user IS NOT NULL
 AND EXISTS (SELECT 1 FROM clinic_app.identity_userclinicrole r
 JOIN clinic_app.identity_user u ON u.id=r.user_id AND u.is_active
 WHERE r.revoked_at IS NULL AND r.clinic_id=clinic AND r.user_id=owner_user AND \
r.organization_id=NULLIF(
 current_setting('app.current_tenant',true),'')::uuid)))
$f$;

CREATE OR REPLACE FUNCTION clinic_app.workflows_staff_catalog(clinic uuid)
RETURNS TABLE(user_id uuid,display_label text) LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT DISTINCT u.id,u.username::text FROM clinic_app.identity_user u
 JOIN clinic_app.identity_userclinicrole r ON r.user_id=u.id
 WHERE u.is_active AND r.revoked_at IS NULL AND r.clinic_id=clinic AND \
r.organization_id=NULLIF(
 current_setting('app.current_tenant',true),'')::uuid
 AND clinic_app.has_permission('tasks.view',clinic,NULL) ORDER BY u.username::text,u.id
$f$;

CREATE OR REPLACE FUNCTION clinic_app.scheduling_service_practitioners(\
requested_clinic uuid)
RETURNS TABLE(user_id uuid, display_label text)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT DISTINCT u.id, u.username::text
 FROM clinic_app.identity_user u
 JOIN clinic_app.identity_userclinicrole r ON r.user_id=u.id
 WHERE u.is_active AND r.revoked_at IS NULL AND r.role IN \
('physician','nurse','allied_professional')
  AND r.clinic_id=requested_clinic
  AND r.organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid
  AND (clinic_app.has_permission('appointment.book',requested_clinic,NULL)
   OR clinic_app.has_permission('appointment.move',requested_clinic,NULL)
   OR clinic_app.has_permission('configuration.organization',requested_clinic,NULL)
   OR ((clinic_app.has_permission('appointment.book_own',requested_clinic,NULL)
     OR clinic_app.has_permission('appointment.move_own',requested_clinic,NULL))
    AND u.id=NULLIF(current_setting('app.current_user_id',true),'')::uuid))
 ORDER BY u.id
$f$;

CREATE OR REPLACE FUNCTION clinic_app.scheduling_definition_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE
 clinic_zone text;
BEGIN
 IF TG_OP='DELETE' THEN
  RAISE EXCEPTION 'scheduling history cannot be deleted' USING ERRCODE='23514';
 END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended('clinic-lock-v1:clinic:' ||
 NEW.clinic_id::text,0));
 PERFORM 1 FROM clinic_app.identity_clinic WHERE id=NEW.clinic_id FOR UPDATE;
 IF NOT (clinic_app.has_permission('appointment.book',NEW.clinic_id,NULL)
   OR clinic_app.has_permission('configuration.organization',NEW.clinic_id,NULL)) THEN
  RAISE EXCEPTION 'scheduling access denied' USING ERRCODE='42501';
 END IF;
 IF TG_OP='UPDATE' THEN
  IF (to_jsonb(NEW)-'active') IS DISTINCT FROM (to_jsonb(OLD)-'active')
   OR NOT OLD.active OR NEW.active THEN
   RAISE EXCEPTION 'scheduling definitions permit only retirement' USING
 ERRCODE='23514';
  END IF;
  IF TG_TABLE_NAME='scheduling_availabilitytemplate' AND EXISTS (
   SELECT 1 FROM clinic_app.scheduling_availabilityblock b
   JOIN clinic_app.scheduling_appointment a ON a.clinic_id=b.clinic_id
    AND (a.practitioner_id=b.practitioner_id OR b.resource_id=ANY(a.resource_ids))
   WHERE b.template_id=NEW.id
    AND a.status IN ('held','scheduled','arrived','in_progress')
    AND a.end_at>statement_timestamp()
    AND tstzrange(b.start_at,b.end_at,'[)') &&
     clinic_app.scheduling_effective_interval(a.start_at,a.end_at,
      a.buffer_before,a.buffer_after)) THEN
   RAISE EXCEPTION 'template has bookings' USING ERRCODE='23514',
    CONSTRAINT='scheduling_resource_conflict';
  END IF;
  IF TG_TABLE_NAME='scheduling_availabilitytemplate' THEN
   UPDATE clinic_app.scheduling_availabilityblock
    SET retired_at=greatest(statement_timestamp(),created_at),
        updated_at=statement_timestamp()
    WHERE template_id=NEW.id AND retired_at IS NULL;
  END IF;
  RETURN NEW;
 END IF;
 SELECT c.timezone INTO clinic_zone FROM clinic_app.identity_clinic c
 WHERE c.id=NEW.clinic_id AND c.organization_id=NEW.organization_id;
 IF clinic_zone IS NULL THEN
  RAISE EXCEPTION 'scheduling access denied' USING ERRCODE='42501';
 END IF;
 IF TG_TABLE_NAME IN ('scheduling_availabilitytemplate','scheduling_absence') THEN
  IF NEW.resource_id IS NOT NULL AND NOT EXISTS (
   SELECT 1 FROM clinic_app.scheduling_resource r WHERE r.id=NEW.resource_id
    AND r.clinic_id=NEW.clinic_id AND r.organization_id=NEW.organization_id AND
 r.active) THEN
   RAISE EXCEPTION 'scheduling access denied' USING ERRCODE='42501';
  END IF;
  IF NEW.practitioner_id IS NOT NULL AND NOT EXISTS (
   SELECT 1 FROM clinic_app.identity_userclinicrole r
   JOIN clinic_app.identity_user u ON u.id=r.user_id AND u.is_active
   WHERE r.revoked_at IS NULL AND r.user_id=NEW.practitioner_id AND \
r.clinic_id=NEW.clinic_id
    AND r.organization_id=NEW.organization_id
    AND r.role IN ('physician','nurse','allied_professional')) THEN
   RAISE EXCEPTION 'scheduling access denied' USING ERRCODE='42501';
  END IF;
 END IF;
 IF TG_TABLE_NAME='scheduling_availabilitytemplate' THEN
  IF NEW.timezone<>clinic_zone OR cardinality(NEW.weekdays) NOT BETWEEN 1 AND 7
   OR NOT NEW.weekdays <@ ARRAY[0,1,2,3,4,5,6]::smallint[]
   OR array_position(NEW.weekdays,NULL) IS NOT NULL
   OR extract(second FROM NEW.start_local)<>0 OR extract(second FROM
 NEW.end_local)<>0 THEN
   RAISE EXCEPTION 'invalid availability rule' USING ERRCODE='23514';
  END IF;
 END IF;
 IF TG_TABLE_NAME='scheduling_servicetype' THEN
  IF cardinality(NEW.required_professional_roles)=0
   OR NOT NEW.required_professional_roles <@ ARRAY['physician','nurse',
 'allied_professional']::varchar[]
   OR array_position(NEW.required_professional_roles,NULL) IS NOT NULL
   OR NOT NEW.required_resource_kinds <@ ARRAY['room','equipment','location']::varchar[]
   OR array_position(NEW.required_resource_kinds,NULL) IS NOT NULL THEN
   RAISE EXCEPTION 'invalid service requirements' USING ERRCODE='23514';
  END IF;
 END IF;
 IF TG_TABLE_NAME='scheduling_holiday' THEN
  IF EXISTS (SELECT 1 FROM clinic_app.scheduling_appointment a
   WHERE a.clinic_id=NEW.clinic_id AND a.status IN ('held','scheduled','arrived',
 'in_progress')
   AND clinic_app.scheduling_effective_interval(a.start_at,a.end_at,a.buffer_before,
 a.buffer_after)
       && tstzrange(NEW.start_at,NEW.end_at,'[)')) THEN
   RAISE EXCEPTION 'closure conflicts with bookings' USING ERRCODE='23514',
 CONSTRAINT='scheduling_resource_conflict';
  END IF;
 END IF;
 IF TG_TABLE_NAME='scheduling_absence' THEN
  IF EXISTS (SELECT 1 FROM clinic_app.scheduling_appointment a
   WHERE a.clinic_id=NEW.clinic_id AND a.status IN ('held','scheduled','arrived',
 'in_progress')
   AND (a.practitioner_id=NEW.practitioner_id OR NEW.resource_id=ANY(a.resource_ids))
   AND clinic_app.scheduling_effective_interval(a.start_at,a.end_at,a.buffer_before,
 a.buffer_after)
       && tstzrange(NEW.start_at,NEW.end_at,'[)')) THEN
   RAISE EXCEPTION 'absence conflicts with bookings' USING ERRCODE='23514',
 CONSTRAINT='scheduling_resource_conflict';
  END IF;
 END IF;
 RETURN NEW;
END $f$;

CREATE OR REPLACE FUNCTION clinic_app.scheduling_capacity_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE
 s clinic_app.scheduling_servicetype;
 r clinic_app.scheduling_resource;
 span tstzrange;
 chosen integer;
 consumes boolean;
BEGIN
 consumes := NEW.status IN ('held','scheduled','arrived','in_progress');
 IF TG_WHEN='BEFORE' THEN
  IF TG_OP='UPDATE' AND ROW(NEW.service_type_id,NEW.resource_ids,NEW.buffer_before,
 NEW.buffer_after)
   IS DISTINCT FROM ROW(OLD.service_type_id,OLD.resource_ids,OLD.buffer_before,
 OLD.buffer_after) THEN
   RAISE EXCEPTION 'booking requirements are immutable' USING ERRCODE='23514';
  END IF;
  IF TG_OP='UPDATE' AND NEW.service_type_id IS NOT NULL AND NOT (
   clinic_app.has_permission('appointment.move',NEW.clinic_id,NULL)
   OR (clinic_app.has_permission('appointment.move_own',NEW.clinic_id,NULL)
    AND NEW.practitioner_id=NULLIF(current_setting('app.current_user_id',true),'')
      ::uuid)
   OR EXISTS (SELECT 1 FROM clinic_app.patient_booking_scope() scope
    WHERE scope.clinic_id=NEW.clinic_id AND scope.organization_id=NEW.organization_id
     AND scope.patient_id=NEW.patient_id)) THEN
   RAISE EXCEPTION 'scheduling access denied' USING ERRCODE='42501';
  END IF;
  IF TG_OP='INSERT' AND NEW.service_type_id IS NOT NULL THEN
    IF NOT (clinic_app.has_permission('appointment.book',NEW.clinic_id,NULL)
     OR (clinic_app.has_permission('appointment.book_own',NEW.clinic_id,NULL)
         AND NEW.practitioner_id=NULLIF(current_setting('app.current_user_id',true),
 '')::uuid)) THEN
     RAISE EXCEPTION 'scheduling access denied' USING ERRCODE='42501';
    END IF;
  END IF;
  IF NOT consumes THEN RETURN NEW; END IF;
  PERFORM 1 FROM clinic_app.identity_clinic WHERE id=NEW.clinic_id FOR SHARE;
  IF EXISTS (SELECT 1 FROM clinic_app.scheduling_availabilityblock b
   JOIN clinic_app.scheduling_availabilitytemplate t ON t.id=b.template_id
   WHERE b.clinic_id=NEW.clinic_id AND b.practitioner_id=NEW.practitioner_id
    AND b.retired_at IS NULL AND b.start_at<=NEW.start_at AND b.end_at>=NEW.end_at
 AND NOT t.active) THEN
   RAISE EXCEPTION 'outside template' USING ERRCODE='23514',
    CONSTRAINT='scheduling_outside_template';
  END IF;
  IF NEW.service_type_id IS NULL THEN
   IF cardinality(NEW.resource_ids)<>0 OR NEW.buffer_before<>0 OR NEW.buffer_after<>0
 THEN
    RAISE EXCEPTION 'service required for resources' USING ERRCODE='23514',
 CONSTRAINT='scheduling_buffer_violation';
   END IF;
  ELSE
   SELECT * INTO s FROM clinic_app.scheduling_servicetype
    WHERE id=NEW.service_type_id AND clinic_id=NEW.clinic_id AND
 organization_id=NEW.organization_id;
   IF s.id IS NULL OR (TG_OP='INSERT' AND NOT s.active) THEN
    RAISE EXCEPTION 'scheduling access denied' USING ERRCODE='42501';
   END IF;
   IF TG_OP='INSERT' THEN
    NEW.buffer_before:=s.buffer_before; NEW.buffer_after:=s.buffer_after;
   END IF;
   IF NEW.end_at-NEW.start_at<>s.duration_min*interval '1 minute' THEN
    RAISE EXCEPTION 'service duration mismatch' USING ERRCODE='23514',
 CONSTRAINT='scheduling_buffer_violation';
   END IF;
   IF NOT EXISTS (SELECT 1 FROM clinic_app.identity_userclinicrole role
    JOIN clinic_app.identity_user u ON u.id=role.user_id AND u.is_active
    WHERE role.revoked_at IS NULL AND role.user_id=NEW.practitioner_id AND \
role.clinic_id=NEW.clinic_id
     AND role.organization_id=NEW.organization_id AND
 role.role=ANY(s.required_professional_roles)) THEN
    RAISE EXCEPTION 'professional role unavailable' USING ERRCODE='23514',
 CONSTRAINT='scheduling_buffer_violation';
   END IF;
   IF cardinality(NEW.resource_ids)>16 OR array_position(NEW.resource_ids,NULL) IS
 NOT NULL
    OR cardinality(NEW.resource_ids)<>(SELECT count(DISTINCT id) FROM
 unnest(NEW.resource_ids) id)
    OR cardinality(NEW.resource_ids)<>(SELECT count(*) FROM
 clinic_app.scheduling_resource resource
      WHERE resource.id=ANY(NEW.resource_ids) AND resource.clinic_id=NEW.clinic_id
       AND resource.organization_id=NEW.organization_id AND resource.active)
    OR EXISTS (SELECT 1 FROM unnest(s.required_resource_kinds) kind WHERE NOT EXISTS (
      SELECT 1 FROM clinic_app.scheduling_resource resource WHERE
 resource.id=ANY(NEW.resource_ids) AND resource.kind=kind)) THEN
    RAISE EXCEPTION 'resource selection unavailable' USING ERRCODE='23514',
 CONSTRAINT='scheduling_resource_conflict';
   END IF;
  END IF;
  span:=clinic_app.scheduling_effective_interval(NEW.start_at,NEW.end_at,
 NEW.buffer_before,NEW.buffer_after);
  IF EXISTS (SELECT 1 FROM clinic_app.scheduling_holiday h WHERE
 h.clinic_id=NEW.clinic_id
   AND h.organization_id=NEW.organization_id AND h.active AND tstzrange(h.start_at,
 h.end_at,'[)') && span)
   OR EXISTS (SELECT 1 FROM clinic_app.scheduling_absence a WHERE
 a.clinic_id=NEW.clinic_id
    AND a.organization_id=NEW.organization_id AND a.active
    AND (a.practitioner_id=NEW.practitioner_id OR a.resource_id=ANY(NEW.resource_ids))
    AND tstzrange(a.start_at,a.end_at,'[)') && span) THEN
   RAISE EXCEPTION 'holiday or absence' USING ERRCODE='23514',
 CONSTRAINT='scheduling_holiday';
  END IF;
  IF NEW.service_type_id IS NOT NULL AND NOT EXISTS (
   SELECT 1 FROM clinic_app.scheduling_availabilityblock a
   WHERE a.clinic_id=NEW.clinic_id AND a.organization_id=NEW.organization_id
    AND a.practitioner_id=NEW.practitioner_id AND a.retired_at IS NULL
    AND a.start_at<=lower(span) AND a.end_at>=upper(span)
    AND (a.template_id IS NULL OR EXISTS (SELECT 1 FROM
 clinic_app.scheduling_availabilitytemplate t WHERE t.id=a.template_id AND
 t.active))) THEN
   RAISE EXCEPTION 'buffer outside availability' USING ERRCODE='23514',
 CONSTRAINT='scheduling_buffer_violation';
  END IF;
  FOR r IN SELECT * FROM clinic_app.scheduling_resource WHERE
 id=ANY(NEW.resource_ids) ORDER BY id FOR UPDATE LOOP
   IF NOT EXISTS (SELECT 1 FROM clinic_app.scheduling_availabilityblock a
    WHERE a.resource_id=r.id AND a.clinic_id=NEW.clinic_id AND
 a.organization_id=NEW.organization_id
     AND a.retired_at IS NULL AND a.start_at<=lower(span) AND a.end_at>=upper(span)
     AND (a.template_id IS NULL OR EXISTS (SELECT 1 FROM
 clinic_app.scheduling_availabilitytemplate t WHERE t.id=a.template_id AND
 t.active))) THEN
    RAISE EXCEPTION 'resource outside template' USING ERRCODE='23514',
 CONSTRAINT='scheduling_outside_template';
   END IF;
  END LOOP;
  RETURN NEW;
 END IF;
 span:=clinic_app.scheduling_effective_interval(NEW.start_at,NEW.end_at,
 NEW.buffer_before,NEW.buffer_after);
 IF NOT consumes THEN
  UPDATE clinic_app.scheduling_appointmentresource SET occupied=false WHERE
 appointment_id=NEW.id;
  RETURN NEW;
 END IF;
 FOR r IN SELECT * FROM clinic_app.scheduling_resource WHERE id=ANY(NEW.resource_ids)
 ORDER BY id LOOP
  SELECT slots.unit INTO chosen FROM generate_series(1,r.capacity) slots(unit) WHERE
 NOT EXISTS (
   SELECT 1 FROM clinic_app.scheduling_appointmentresource ar WHERE ar.resource_id=r.id
    AND ar.unit=slots.unit AND ar.occupied AND ar.appointment_id<>NEW.id
    AND tstzrange(ar.start_at,ar.end_at,'[)') && span) ORDER BY slots.unit LIMIT 1;
  IF chosen IS NULL THEN
   RAISE EXCEPTION 'resource capacity unavailable' USING ERRCODE='23514',
 CONSTRAINT='scheduling_resource_conflict';
  END IF;
  INSERT INTO clinic_app.scheduling_appointmentresource
   (id,organization_id,clinic_id,appointment_id,resource_id,unit,start_at,end_at,
 occupied)
  VALUES (gen_random_uuid(),NEW.organization_id,NEW.clinic_id,NEW.id,r.id,chosen,
 lower(span),upper(span),true)
  ON CONFLICT (appointment_id,resource_id) DO UPDATE SET unit=excluded.unit,
   start_at=excluded.start_at,end_at=excluded.end_at,occupied=excluded.occupied;
 END LOOP;
 RETURN NEW;
END $f$;

RESET ROLE;
"""

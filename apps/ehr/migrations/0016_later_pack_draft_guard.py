"""Apply pack first-use guards to replacement drafts after discarded versions."""

from typing import ClassVar

from django.db import migrations
from django.db.migrations.operations.base import Operation

SQL = """
SET LOCAL ROLE clinic_resolver;
CREATE OR REPLACE FUNCTION clinic_app.ehr_pack_use_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE p clinic_app.ehr_specialtypack;
BEGIN
 IF TG_TABLE_NAME='ehr_clinicaldocumentversion' THEN
   -- The clinical binding guard proves a genuine finalized base, same document,
   -- template and content for amendments. A later version alone proves nothing:
   -- a fresh initial draft can follow any number of discarded drafts.
   IF NEW.amendment_of_id IS NOT NULL THEN RETURN NEW; END IF;
   SELECT * INTO p FROM clinic_app.ehr_specialtypack
    WHERE template_id=NEW.template_id;
   IF p.key='psychiatry' AND NOT EXISTS (
     SELECT 1 FROM clinic_app.ehr_restrictednote n
      WHERE n.document_id=NEW.document_id AND n.clinic_id=p.clinic_id
      AND n.organization_id=NEW.organization_id
      AND n.restricted_by_id=NEW.author_id) THEN
     RAISE EXCEPTION 'psychiatry requires a restricted note' USING ERRCODE='23514';
   END IF;
 ELSE
   SELECT * INTO p FROM clinic_app.ehr_specialtypack
    WHERE questionnaire_id=NEW.template_id;
 END IF;
 IF p.id IS NOT NULL THEN
   PERFORM pg_advisory_xact_lock_shared(hashtextextended(
     'ehr-pack-use:'||p.id::text,0));
   SELECT * INTO p FROM clinic_app.ehr_specialtypack WHERE id=p.id;
 END IF;
 IF p.id IS NOT NULL AND p.uninstalled_at IS NOT NULL THEN
   RAISE EXCEPTION 'specialty pack unavailable' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $f$;
RESET ROLE;
"""


class Migration(migrations.Migration):
    """Correct the guard without reopening the bypass on rollback."""

    dependencies: ClassVar[list[tuple[str, str]]] = [
        ("ehr", "0015_specialty_packs"),
    ]
    operations: ClassVar[list[Operation]] = [migrations.RunSQL(SQL)]

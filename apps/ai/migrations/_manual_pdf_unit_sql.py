"""Only accept a separate clinician unit when filing a PDF decimal draft."""

from apps.ai.migrations._care_sql import _edit, _resolver
from apps.ai.migrations._retained_effect_sql import _REVIEW

_SOURCE_TYPE = """ IF j.attachment_id IS NULL THEN
  source_type:=v.content_type;
 ELSE
  SELECT a.detected_type INTO source_type
   FROM clinic_app.ehr_clinicalattachment a WHERE a.id=j.attachment_id;
 END IF;
 IF source_type IS NULL THEN
  RAISE EXCEPTION 'extraction filing denied' USING ERRCODE='23514'; END IF;
 """

_PDF_DECIMAL = """IF source_type='application/pdf' THEN
    supplied:=convert_from(clinic_app.tenant_decrypt(
     kek,'orders.observation.source',source),'UTF8')::jsonb;
    filed_unit:=supplied->>'unit';
    IF p->>'unit'<>''
    OR p->>'raw_text' IS DISTINCT FROM (
       (p->>'field')||': '||(p->>'value')||CASE
        WHEN p->>'reference_interval'='' THEN ''
        ELSE ' ['||(p->>'reference_interval')||']' END)
    OR supplied->>'unit_entry' IS DISTINCT FROM 'clinician'
    OR jsonb_typeof(supplied->'unit') IS DISTINCT FROM 'string'
    OR length(filed_unit) NOT BETWEEN 1 AND 64
    OR filed_unit!~'^[[:print:]]+$'
    OR filed_unit~'^[[:space:]]|[[:space:]]$' THEN
     RAISE EXCEPTION 'extraction manual unit required' USING ERRCODE='23514'; END IF;
   ELSIF"""

# Keep each intermediate single-assignment for the finite RunSQL decoder.
_REVIEW_FIELDS = _edit(
    _REVIEW,
    " v clinic_app.orders_resultversion; p jsonb; expected jsonb; parsed text;",
    " v clinic_app.orders_resultversion; p jsonb; expected jsonb; supplied jsonb;\n"
    " parsed text; source_type text; filed_unit text;",
)
_REVIEW_SOURCE = _edit(
    _REVIEW_FIELDS, " p:=convert_from", _SOURCE_TYPE + "p:=convert_from"
)
_REVIEW_UNIT_DEFAULT = _edit(
    _REVIEW_SOURCE,
    " IF p->>'kind'='decimal' THEN",
    " filed_unit:=p->>'unit';\n IF p->>'kind'='decimal' THEN",
)
_REVIEW_PDF_DECIMAL = _edit(
    _REVIEW_UNIT_DEFAULT,
    " OR p->>'unit'<>ALL",
    " THEN\n"
    "   RAISE EXCEPTION 'extraction value unresolved' USING ERRCODE='23514'; END IF;\n"
    f"   {_PDF_DECIMAL} p->>'unit'<>ALL",
)
_REVIEW_PAYLOAD = _edit(_REVIEW_PDF_DECIMAL, "'unit',p->>'unit'", "'unit',filed_unit")
_REVIEW_MANUAL_UNIT = _edit(
    _REVIEW_PAYLOAD,
    " IF convert_from(clinic_app.tenant_decrypt(\n"
    "  kek,'orders.observation.source',source),'UTF8')::jsonb\n"
    "  IS DISTINCT FROM expected THEN",
    """ IF source_type='application/pdf' AND p->>'kind'='decimal' THEN
  expected:=expected||'{"unit_entry":"clinician"}'::jsonb;
 ELSE
  supplied:=convert_from(clinic_app.tenant_decrypt(
   kek,'orders.observation.source',source),'UTF8')::jsonb;
 END IF;
 IF supplied IS DISTINCT FROM expected THEN""",
)

SQL = _resolver((_REVIEW_MANUAL_UNIT,))
REVERSE_SQL = _resolver((_REVIEW,))

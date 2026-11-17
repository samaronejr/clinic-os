"""Keep native recognition provenance inside its original ASR permission."""

from apps.ai.migrations._care_sql import _edit, _function, _resolver
from apps.scribe.migrations._transcript_sql import SQL as TRANSCRIPT

_PRIOR = _function(TRANSCRIPT, "scribe_transcript_guard")
_VERSION = _edit(
    _PRIOR,
    "   THEN RAISE EXCEPTION 'invalid recognition successor' "
    "USING ERRCODE='23514'; END IF;",
    """   THEN RAISE EXCEPTION 'invalid recognition successor'
    USING ERRCODE='23514'; END IF;
   IF NOT clinic_app.ai_effect_current(q.invocation_id,s.clinic_id) THEN
    RAISE EXCEPTION 'transcription invocation permission required'
     USING ERRCODE='42501';
   END IF;""",
)
_GUARD = _edit(
    _VERSION,
    " THEN RAISE EXCEPTION 'invalid transcript segment' "
    "USING ERRCODE='23514'; END IF;\n RETURN NEW;",
    """ THEN RAISE EXCEPTION 'invalid transcript segment'
  USING ERRCODE='23514'; END IF;
 IF v.mutation='recognized' THEN
  SELECT * INTO q FROM clinic_app.scribe_chunktranscription WHERE id=v.request_id;
  IF NOT clinic_app.ai_effect_current(q.invocation_id,s.clinic_id) THEN
   RAISE EXCEPTION 'transcription invocation permission required'
    USING ERRCODE='42501';
  END IF;
 END IF;
 RETURN NEW;""",
)

SQL = _resolver((_GUARD,))
REVERSE_SQL = _resolver((_PRIOR,))

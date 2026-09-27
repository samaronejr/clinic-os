"""Canonical, OID-independent catalog bytes for restoration proofs."""

import hashlib
import json

from django.db import connection

CATALOG_ROWS = """
WITH namespaces AS (
 SELECT oid,nspname FROM pg_namespace
 WHERE nspname !~ '^pg_(temp|toast_temp)_'
), objects AS (
 SELECT 'function' AS kind, n.nspname || '.' || p.proname AS name,
 jsonb_build_array(oidvectortypes(p.proargtypes),p.prorettype::regtype::text,
 p.prokind,p.provolatile,p.prosrc,p.probin,p.proconfig,p.prosecdef,
 p.proowner::regrole::text,p.proacl::text,l.lanname,
 CASE WHEN p.prokind IN ('f','p') THEN pg_get_functiondef(p.oid) ELSE '' END) AS value
 FROM pg_proc p JOIN namespaces n ON n.oid=p.pronamespace
 JOIN pg_language l ON l.oid=p.prolang
 UNION ALL
 SELECT 'relation',n.nspname || '.' || c.relname,
 jsonb_build_array(c.relkind,c.relrowsecurity,c.relforcerowsecurity,c.reloptions,
 c.relowner::regrole::text,c.relacl::text,c.relispartition,
 pg_get_expr(c.relpartbound,c.oid))
 FROM pg_class c JOIN namespaces n ON n.oid=c.relnamespace
 UNION ALL
 SELECT 'attribute',n.nspname || '.' || c.relname || '.' || a.attname,
 jsonb_build_array(a.attnum,format_type(a.atttypid,a.atttypmod),a.attnotnull,
 a.attidentity,a.attgenerated,pg_get_expr(d.adbin,d.adrelid))
 FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
 JOIN namespaces n ON n.oid=c.relnamespace
 LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
 WHERE a.attnum>0 AND NOT a.attisdropped
 UNION ALL
 SELECT 'policy',p.polrelid::regclass::text || '.' || p.polname,
 jsonb_build_array(p.polcmd,p.polpermissive,p.polroles::text,
 pg_get_expr(p.polqual,p.polrelid),pg_get_expr(p.polwithcheck,p.polrelid))
 FROM pg_policy p JOIN pg_class c ON c.oid=p.polrelid
 JOIN namespaces n ON n.oid=c.relnamespace
 UNION ALL
 SELECT 'constraint',n.nspname || '.' || c.conname,
 jsonb_build_array(c.conrelid::regclass::text,c.contypid::regtype::text,
 c.contype,c.convalidated,pg_get_constraintdef(c.oid))
 FROM pg_constraint c JOIN namespaces n ON n.oid=c.connamespace
 UNION ALL
 SELECT 'rule',r.ev_class::regclass::text || '.' || r.rulename,
 jsonb_build_array(r.ev_enabled,pg_get_ruledef(r.oid))
 FROM pg_rewrite r JOIN pg_class c ON c.oid=r.ev_class
 JOIN namespaces n ON n.oid=c.relnamespace
 UNION ALL
 SELECT 'trigger',t.tgrelid::regclass::text || '.' || t.tgname,
 jsonb_build_array(t.tgenabled,pg_get_triggerdef(t.oid))
 FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
 JOIN namespaces n ON n.oid=c.relnamespace
 UNION ALL
 SELECT 'event-trigger',e.evtname,
 jsonb_build_array(e.evtevent,e.evtenabled,e.evtfoid::regprocedure::text,e.evttags)
 FROM pg_event_trigger e
 UNION ALL
 SELECT 'type',n.nspname || '.' || t.typname,
 jsonb_build_array(t.typtype,t.typbasetype::regtype::text,t.typinput::regproc::text,
 t.typtypmod,t.typdefault,t.typnotnull,t.typelem::regtype::text,
 t.typrelid::regclass::text,pg_get_expr(t.typdefaultbin,0))
 FROM pg_type t JOIN namespaces n ON n.oid=t.typnamespace
 UNION ALL
 SELECT 'range',r.rngtypid::regtype::text,
 jsonb_build_array(r.rngsubtype::regtype::text,r.rngmultitypid::regtype::text,
 r.rngcollation::regcollation::text,o.opcnamespace::regnamespace::text,o.opcname,
 r.rngcanonical::regprocedure::text,r.rngsubdiff::regprocedure::text)
 FROM pg_range r JOIN pg_opclass o ON o.oid=r.rngsubopc
 JOIN pg_type t ON t.oid=r.rngtypid JOIN namespaces n ON n.oid=t.typnamespace
 UNION ALL
 SELECT 'inheritance',i.inhrelid::regclass::text,
 jsonb_build_array(i.inhparent::regclass::text,i.inhseqno,i.inhdetachpending)
 FROM pg_inherits i JOIN pg_class c ON c.oid=i.inhrelid
 JOIN namespaces n ON n.oid=c.relnamespace
 UNION ALL
 SELECT 'namespace',n.nspname,
 jsonb_build_array(p.nspowner::regrole::text,p.nspacl::text)
 FROM namespaces n JOIN pg_namespace p ON p.oid=n.oid
 UNION ALL
 SELECT 'extension',e.extname,jsonb_build_array(e.extversion,
 (SELECT count(*) FROM pg_depend d WHERE d.refclassid='pg_extension'::regclass
 AND d.refobjid=e.oid AND d.deptype='e')) FROM pg_extension e
)
SELECT kind,name,value::text FROM objects ORDER BY kind,name,value::text
"""


def catalog_sha256() -> str:
    with connection.cursor() as cursor:
        cursor.execute(CATALOG_ROWS)
        rows = cursor.fetchall()
    return hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()

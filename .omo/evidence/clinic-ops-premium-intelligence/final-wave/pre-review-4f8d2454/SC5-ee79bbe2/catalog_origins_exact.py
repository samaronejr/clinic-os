"""Observe committed PG catalog transitions after each real Django migration.

Run only on a bootstrapped, empty, private test database. No permissions are
learned by the verifier; this offline witness validates an independently
reviewed, fixed policy. Never retain function bodies or migration arguments.
"""
import hashlib
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit, urlunsplit

import django
import psycopg
from django.core.management import call_command
from django.core.management.commands import migrate

QUERY = """
SELECT p.oid,p.oid::regprocedure::text,pg_get_function_identity_arguments(p.oid),
       pg_get_userbyid(p.proowner),encode(sha256(convert_to(pg_get_functiondef(p.oid),'UTF8')),'hex'),
       ARRAY(SELECT pg_get_userbyid(a.grantee)::text
             FROM aclexplode(p.proacl) a WHERE a.privilege_type='EXECUTE' ORDER BY 1),
       ARRAY(SELECT pg_get_userbyid(a.grantee)::text
             FROM aclexplode(p.proacl) a WHERE a.privilege_type='EXECUTE' AND a.is_grantable ORDER BY 1)
FROM pg_proc p JOIN pg_namespace n ON p.pronamespace=n.oid
WHERE n.nspname='clinic_app' AND p.prokind='f'
 AND NOT EXISTS(SELECT FROM pg_depend d WHERE d.classid='pg_proc'::regclass
                AND d.objid=p.oid AND d.deptype='e' AND d.refclassid='pg_extension'::regclass)
ORDER BY 2
"""


def main():
    testname=os.environ['TEST_DATABASE_NAME']
    old=urlsplit(os.environ['MIGRATION_DATABASE_URL'])
    assert old.hostname=='127.0.0.1' and old.username=='clinic_owner' and old.port not in (None,5432)
    assert testname=='test_clinic' and old.path=='/clinic'
    assert os.environ['POSTGRES_CONTAINER'].startswith('clinic_renewal_db_origin_')
    url=urlunsplit(old._replace(path='/'+testname))
    os.environ['MIGRATION_DATABASE_URL']=os.environ['APP_DATABASE_URL']=url
    django.setup()
    history={};previous={};steps=[]
    with psycopg.connect(url,autocommit=True) as witness:
        witness.execute('SET search_path=pg_catalog')
        assert witness.execute("SELECT current_database(),to_regclass('clinic_app.django_migrations')").fetchone()==(testname,None)
        assert not witness.execute(QUERY).fetchall()

        class Record(migrate.MigrationExecutor):
            def apply_migration(self,state,migration,**kwargs):
                nonlocal previous
                state=super().apply_migration(state,migration,**kwargs)
                key=f'{migration.app_label}/{migration.name}'
                assert key not in steps;steps.append(key)
                now={row[0]:row[1:] for row in witness.execute(QUERY)}
                for oid,row in now.items():
                    if oid not in previous:
                        history[oid]=dict(created=key,named=key,defined=key)
                    else:
                        if row[:2]!=previous[oid][:2]:history[oid]['named']=key
                        if row[3]!=previous[oid][3]:history[oid]['defined']=key
                previous=now
                return state

        with patch.object(migrate,'MigrationExecutor',Record):
            call_command('migrate',interactive=False,verbosity=0)
        assert steps and {r[0]:r[1:] for r in witness.execute(QUERY)}==previous
    rows=[dict(signature=sig,identity_arguments=args,owner=owner,definition_sha256=sha,
               grantees=roles,grant_options=delegations,**history[oid])
          for oid,(sig,args,owner,sha,roles,delegations) in previous.items()]
    rows.sort(key=lambda r:r['signature'])
    assert len(rows)==len({r['signature'] for r in rows})==608
    payload=dict(sha=os.environ["R4E_SHA"],run_id=os.environ["INVOCATION_ID"],steps=steps,routines=rows)
    with Path(sys.argv[1]).open('x') as out:json.dump(payload,out,sort_keys=True,separators=(',',':'));out.write('\n')
    print('catalog provenance replay:',len(rows),'routines;',len(steps),'migrations')

if __name__=='__main__':main()

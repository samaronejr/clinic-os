"""Fresh, owned exact-commit catalog observation; never changes the policy file."""
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4
from ops.testing import renewal_runner

root=Path(__file__).parent
repo=Path.cwd()
sha=subprocess.check_output(['git','rev-parse','HEAD'],cwd=repo,text=True).strip()
assert sha==os.environ['R4E_SHA']
with renewal_runner._provision_database(repo,'origin_'+uuid4().hex) as lease:
    renewal_runner._create_test_database(repo,lease)
    environment={**os.environ, 'MIGRATION_DATABASE_URL':lease.owner_dsn,'APP_DATABASE_URL':lease.app_dsn,
                 'POSTGRES_CONTAINER':lease.container, 'TEST_DATABASE_NAME':'test_'+lease.database}
    subprocess.run([sys.executable,'-B',str(root/'catalog_origins_exact.py'),str(root/'catalog-origins.json')],
                   cwd=repo,env=environment,check=True)
print('Fresh origin-replay lease released',flush=True)

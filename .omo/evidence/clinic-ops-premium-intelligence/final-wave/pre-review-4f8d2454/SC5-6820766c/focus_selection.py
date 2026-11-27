"""Select exactly the 74 prior focused public IDs and the twelve r4f additions."""
import os
import xml.etree.ElementTree as ET
from pathlib import Path
from sc5_report import public_id

FILES=('tests/infra/test_object_owner_posture.py','tests/infra/test_posture_group_identity.py')
orig={t.get('name') for t in ET.parse(os.environ['R4F_OLD_FOCUS']).iter('testcase')}
assert len(orig)==74
def pytest_collection_modifyitems(config,items):
    keep=[];omit=[]
    for item in items:
        (keep if public_id(item.nodeid) in orig or item.nodeid.partition('::')[0] in FILES else omit).append(item)
    assert len(keep)==86 and orig<={public_id(i.nodeid) for i in keep}
    config.hook.pytest_deselected(items=omit);items[:]=keep
if __name__=='__main__': print(*sorted({s.partition('::')[0] for s in orig}|set(FILES)),sep='\n')

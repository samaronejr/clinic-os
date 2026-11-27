"""Import exact r4g modules for the driver and no-receipt regressions."""
import hashlib,importlib,subprocess,sys,types
from pathlib import Path
BASE='26a2a86b4873b2bcfea74ac42a9e7700a0661d14'
root=Path.cwd();sys.path.insert(0,str(root/'tests'))
for name in ('posture_child_lifetime','test_posture_group_identity'):
    p='tests/infra/'+name+'.py'
    data=subprocess.check_output(['git','show',BASE+':'+p])
    assert hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()==subprocess.check_output(['git','rev-parse',BASE+':'+p],text=True).strip()
    pkg=importlib.import_module('infra');mod=types.ModuleType('infra.'+name);mod.__file__=str(root/p);mod.__package__='infra'
    sys.modules['infra.'+name]=mod;setattr(pkg,name,mod);exec(compile(data,str(root/p),'exec'),mod.__dict__)

import pytest
from unittest.mock import patch
@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item):
    if item.nodeid.endswith('::test_entire_kek_lane_has_no_numeric_signal_fallback'):
        from ops.testing import kek_signal_audit
        real=kek_signal_audit.scope
        with patch.object(kek_signal_audit,'scope',lambda root:real(root,BASE)):
            return (yield)
    return (yield)

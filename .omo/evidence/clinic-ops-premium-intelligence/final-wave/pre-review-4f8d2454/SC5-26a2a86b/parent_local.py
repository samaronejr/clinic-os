"""Load exactly r4f's worker/observation; new tests supply only the inert syscall model."""
import hashlib,importlib,subprocess,sys,types
from pathlib import Path
BASE='6820766c6d02ae9baaca01e09facd0a14167ce0c'
repo=Path.cwd();sys.path.insert(0,str(repo/'tests'))
for name in ('posture_observation','posture_trace_worker'):
    path='tests/infra/'+name+'.py';data=subprocess.check_output(['git','show',BASE+':'+path])
    assert hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()==subprocess.check_output(['git','rev-parse',BASE+':'+path],text=True).strip()
    pkg=importlib.import_module('infra');mod=types.ModuleType('infra.'+name);mod.__file__=str(repo/path);mod.__package__='infra'
    sys.modules['infra.'+name]=mod;setattr(pkg,name,mod);exec(compile(data,str(repo/path),'exec'),mod.__dict__)

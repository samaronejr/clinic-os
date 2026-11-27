"""Import the exact reviewed parent modules before collecting the new controls."""
import hashlib,importlib,os,subprocess,sys,types
from pathlib import Path
SHA='a3013b5b2fe360e1502f459f325ceb6be0de4469'
root=Path.cwd();sys.path.insert(0,str(root/'tests'))
for name,p in [('ops.db.posture','ops/db/posture.py'),('infra.posture_trace_worker','tests/infra/posture_trace_worker.py')]:
    data=subprocess.check_output(['git','show',SHA+':'+p])
    assert hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()==subprocess.check_output(['git','rev-parse',SHA+':'+p],text=True).strip()
    parent,_,child=name.rpartition('.');pkg=importlib.import_module(parent);mod=types.ModuleType(name)
    mod.__package__=parent;mod.__file__=str(root/p);sys.modules[name]=mod;setattr(pkg,child,mod)
    exec(compile(data,str(root/p), 'exec'),mod.__dict__)

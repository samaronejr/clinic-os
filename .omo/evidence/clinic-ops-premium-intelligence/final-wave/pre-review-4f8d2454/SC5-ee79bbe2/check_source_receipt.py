"""Independently rebuild the committed lane source inventory from Git objects."""
import hashlib,json,os,re,subprocess
from pathlib import Path

BASE='4f8d2454987ca1d083ead53a7501126d647a89bc'
def git(*args):return subprocess.check_output(['git',*args])
def decode(raw):return set(os.fsdecode(x) for x in raw.split(b'\0') if x)
def expected(rev):
    git('merge-base','--is-ancestor',BASE,rev)
    affected=decode(git('log','--format=','--no-renames','--name-only','-z',BASE+'..'+rev))
    present=decode(git('ls-tree','-r','-z','--name-only',rev)); files={}
    for n in sorted(affected&present):
        content=git('cat-file','blob',rev+':'+n);p=Path(n)
        line=content.partition(b'\n')[0]
        commands=set(w.rsplit(b'/',1)[-1] for w in line[2:].split()) if line.startswith(b'#!') else set()
        include=p.suffix in {'.py','.sh','.bash','.mk'} or p.name in {'Makefile','GNUmakefile'} or p.name.startswith('Makefile.')
        include|=bool(commands&{b'sh',b'dash',b'ksh',b'bash'}) or any(re.fullmatch(rb'python(?:\d+(?:\.\d+)*)?',w) for w in commands)
        if include:files[n]=hashlib.sha256(content).hexdigest()
    return files
def check(folder,sha,parent):
    findings={}
    for name,rev,red in [('signal-audit.json',sha,False),('parent-signal-audit.json',parent,True)]:
        doc=json.loads((Path(folder)/name).read_bytes());files=expected(rev)
        assert files and doc['base']==BASE and doc['revision']==rev and doc['files']==files
        for item in doc['findings']:
            assert set(item)=={'path','line','rule'} and item['path'] in files and type(item['line'])==int and item['line']>0
            assert item['rule'] in {'numeric-signal','implicit-timeout-signal','signal-command'}
        if red:
            assert any(r['path']=='tests/infra/test_posture_group_identity.py' and r['rule']=='implicit-timeout-signal' for r in doc['findings'])
            assert any(r['path']=='ops/testing/renewal_runner.py' and r['rule']=='numeric-signal' for r in doc['findings'])
        else:assert doc['findings']==[]
        findings['parent' if red else 'successor']={'files':len(files),'findings':len(doc['findings'])}
    return findings

"""Static, fail-closed assertion ledger from immutable Git blobs (stdlib)."""
import ast
import collections
import hashlib
import json
import subprocess
import sys
from pathlib import Path

WAVE = "4f8d2454987ca1d083ead53a7501126d647a89bc"
BASE = "de424fa482adc1346392049fbf2d556cb05c957d"
HEAD = "31938a30ab9bfc8a5cb9da9568d834719fbab975"
EVENT = "tests/teleconsult/test_event_migration.py"
MODE = "tests/teleconsult/test_mode_migration.py"
FNTYPES = (ast.FunctionDef,ast.AsyncFunctionDef)
NAMES = {
    "_exercise_event_migration":"test_event_authority_reverse_is_exact_with_populated_v2_events",
    "_exercise_mode_migration":"test_populated_mode_migration_reverses_and_reapplies_exact_catalog",
}
REC = "frozenset(MigrationExecutor(connection).loader.applied_migrations) == installed"

def git(*args):
    return subprocess.check_output(["git",*args],text=True).rstrip("\n")

def src(sha,path):
    return git("show",sha+":"+path) if git("ls-tree","--name-only",sha,"--",path) else ""

def same(a,b):
    assert ast.dump(a,include_attributes=False)==ast.dump(b,include_attributes=False)

def items(text):
    tree=ast.parse(text); parent={c:n for n in ast.walk(tree) for c in ast.iter_child_nodes(n)}
    out=collections.defaultdict(list)
    for n in ast.walk(tree):
        if isinstance(n,ast.Assert): kind="assert"; expr=ast.unparse(n.test)
        elif isinstance(n,ast.Call) and ast.unparse(n.func) in ("pytest.raises","pytest.fail"):
            kind="expectation";expr=ast.unparse(n)
        elif isinstance(n,ast.Constant) and isinstance(n.value,str) and "; assert " in n.value:
            kind="embedded Python";expr=n.value
        else:continue
        p=n
        while p in parent and not isinstance(p,FNTYPES):p=parent[p]
        fn=NAMES.get(getattr(p,'name',''),getattr(p,'name','<module>'))
        out[(fn,kind)].append((n.lineno,expr))
    return {k:sorted(rows) for k,rows in out.items()}

def migration_body(path,old,new):
    helper="_exercise_event_migration" if path==EVENT else "_exercise_mode_migration"
    def get(text,name):
        return next(n for n in ast.parse(text).body if isinstance(n,ast.FunctionDef) and n.name==name)
    a=get(old,NAMES[helper]); b=get(new,helper); wrapper=get(new,NAMES[helper])
    assert [ast.unparse(e) for e in wrapper.body]==[f"with committed_migration_world as rbac_graph:\n    {helper}(rbac_graph)"]
    oldlines=[x for x in ast.walk(a) if isinstance(x,ast.Assert) and ast.unparse(x.test)==REC]
    newlines=sorted([x for x in ast.walk(b) if isinstance(x,ast.Assert) and ast.unparse(x.test)==REC],key=lambda n:n.lineno)
    assert len(oldlines)==1 and len(newlines)==2
    # Restore only the pre-existing shape for comparison: no test/call/try/finally
    # may disappear, change its order, or move across a context-manager boundary.
    class UndoAdditions(ast.NodeTransformer):
        def visit_Assert(self,n):
            return None if n is newlines[0] else n
        def visit_Tuple(self,n):
            n=self.generic_visit(n)
            if path==EVENT and any(ast.unparse(e)=="('teleconsult', '0005_event_authority')" for e in n.elts):
                assert [ast.literal_eval(e) for e in n.elts]==[
                    ("identity","0051_enterprise_governance"),("scribe","0002_ai_disclosure_capture"),
                    ("teleconsult","0007_consented_capture"),("teleconsult","0006_mode_audit"),("teleconsult","0005_event_authority")]
                n.elts=[e for e in n.elts if ast.unparse(e)!="('scribe', '0002_ai_disclosure_capture')"]
            return n
        def visit_Assign(self,n):
            if path==MODE and len(n.targets)==1 and ast.unparse(n.targets[0])=="(original, new_catalog)":
                assert ast.unparse(n.value)=="(history_hash(w.organization), catalog_hash())"
                return [ast.Assign(targets=[t],value=v) for t,v in zip(n.targets[0].elts,n.value.elts,strict=True)]
            return n
    b=UndoAdditions().visit(b)
    same(ast.Module(body=a.body,type_ignores=[]),ast.Module(body=b.body,type_ignores=[]))
    assert "set_rollback" not in new and "transaction.atomic(" not in new
    leaf=[i for i,line in enumerate(new.splitlines(),1) if 'MigrationExecutor(connection).migrate(' in line]
    assert len(leaf)==1 and leaf[0]<newlines[0].lineno<newlines[-1].lineno
    if path==MODE:
        prior=get(old,"catalog_hash"); after=get(new,"catalog_hash")
        text=ast.unparse(after).replace(",'scribe_room_capture_allowed'","")
        same(prior,ast.parse(text).body[0]); assert ",'scribe_room_capture_allowed'" in ast.unparse(after)
    return {"full_original_rehearsal_body_equivalent":True,"leaf_reinstall_line":leaf[0],
            "new_post_reinstall_equality":newlines[0].lineno,"retained_final_live_equality":newlines[-1].lineno,
            "external_disposal_only":True}

def reason(path,fn,kind,before,after):
    if after is None:raise AssertionError(("removed check",path,fn))
    if before is None:
        if path in (MODE,EVENT):
            assert after==REC
            return "extra exact recorder-set check on committed state; original final equality retained"
        if path=="tests/infra/test_db_posture_url.py":
            return "additional real bad-password denial, redaction and valid-credential recovery; original positive unchanged"
        if path in ("tests/patients/test_dsar.py","tests/patients/delegate_support.py"):
            return "additional clock body admission or exact installed definition restoration; frozen instant and boundary assertions unchanged"
        if path=="tests/teleconsult/conftest.py":
            return "additional check that the real seeded RBAC fixture is returned on the disposable database"
        if path=="tests/infra/test_bound_parameters.py":
            return "new native database binding/parameter control; no wave check replaced"
        if path=="tests/infra/test_role_family_posture.py":
            return "additional real grant, graph, inspection or recovery oracle; no wave check replaced"
        if path=="tests/infra/test_https_role_bootstrap.py":
            return "additional exact count=3 or exact logging defaults equality on existing unchanged role grants"
        if path=="tests/realtime/test_pooling.py" and "server_side_binding" in after:
            return "additional binding=True requirement; existing loop expanded from default/locks to include agent"
        raise AssertionError(("unclassified addition",path,fn,kind,after))
    if path=="tests/infra/test_agent_database.py":
        a,b=[ast.parse(s,mode='eval').body for s in (before,after)]
        assert a.ops[0].__class__==b.ops[0].__class__==ast.Eq
        d=b.comparators[0]; keys=[ast.literal_eval(k) for k in d.keys]
        ix=keys.index("server_side_binding"); assert ast.literal_eval(d.values[ix]) is True
        del d.keys[ix],d.values[ix];same(a,b)
        return "exact options dictionary; deleting only the required new binding=True field recovers original equality, no arbitrary key accepted"
    if path=="tests/infra/test_database_options.py" and kind=="embedded Python":
        needle="assert all(c['OPTIONS']['server_side_binding'] is True for c in d.values()); "
        assert after.count(needle)==1 and after.replace(needle,"")==before
        return "all original subprocess assertions byte-identical plus binding=True on every configured alias"
    if path=="tests/realtime/test_pooling.py" and before.startswith("effective =="):
        a,b=[ast.parse(s,mode='eval').body for s in (before,after)]
        assert ast.literal_eval(b.comparators[0])==ast.literal_eval(a.comparators[0])+("terse","0","0")
        return "exact effective tuple; all old columns unchanged plus three required logging values"
    if path=="tests/realtime/test_pooling.py" and before.startswith("sorted("):
        a,b=[ast.parse(s,mode='eval').body for s in (before,after)]
        same(a.left,b.left)
        assert type(a.ops[0])==type(b.ops[0])==ast.Eq
        assert ast.unparse(a.comparators[0])=="[('clinic_app', False, RUNTIME_DEFAULTS)]"
        assert ast.unparse(b.comparators[0])=="[('clinic_agent', False, LOG_DEFAULTS), ('clinic_app', False, RUNTIME_DEFAULTS), ('clinic_owner', False, LOG_DEFAULTS)]"
        old=src(WAVE,path);new=src(HEAD,path)
        get=lambda text,n:next(v.value for v in ast.parse(text).body if isinstance(v,ast.Assign) and ast.unparse(v.targets[0])==n)
        assert ast.literal_eval(get(new,"LOG_DEFAULTS"))==["log_error_verbosity=terse","log_parameter_max_length=0","log_parameter_max_length_on_error=0"]
        v=get(new,"RUNTIME_DEFAULTS"); stars=[x for x in v.elts if isinstance(x,ast.Starred)]
        assert len(stars)==1 and ast.unparse(stars[0])=="*LOG_DEFAULTS"; v.elts.remove(stars[0]);same(get(old,"RUNTIME_DEFAULTS"),v)
        return "exact complete row list under the required new product contract: historical app settings unchanged, exclusively three new logging defaults on app/agent/owner; no arbitrary row/GUC or scoped override"
    if path=="tests/scribe/test_draft_authority.py":
        assert before=="pytest.raises(DatabaseError)" and after=="pytest.raises(IntegrityError, match='invalid draft source')"
        return "specific IntegrityError and expected domain message; a generic DatabaseError no longer passes"
    raise AssertionError(("unclassified replacement",path,fn,kind,before,after))

DOMAINS={
    "tests/identity/test_identity_acl_migration.py":"Parameterized set_config(false) preserves session scope for exactly the same GUC names/values; all tests unchanged.",
    "tests/patients/delegate_support.py":"No date/role/authorization predicate changed. Save and restore the installed definition and require the expected statement-time expression.",
    "tests/patients/test_dsar.py":"Original DDL during the freeze and all overdue/date equality tests retained. Post-fixture stored definition must be byte-identical.",
    "tests/teleconsult/conftest.py":"Private PG clone with real RBAC and provider fixtures. All aliases preserve role/binding; cleanup closes the clone only after assertions and real actions, without rollback or forced drop.",
    "tests/isolated_pg_fixtures.py":"Real migrated, dedicated cluster; no intercepted database enumeration, privilege check or return value. Test-specific environment only. Required for cluster-wide posture.",
    "tests/conftest.py":"Register independent test-only fixture providers; existing environments unaffected unless requested.",
    "tests/isolation/makefile_process_probe.py":"Original credentials, argument-protection and environment-delivery checks retained; add independent posture credential to both censuses.",
    "tests/isolation/test_makefile_process_security.py":"Same literal assertions, expanded credential-input set includes posture credential.",
    "tests/infra/test_db_posture_url.py":"All legacy URL rejection, credential redaction and supplied-URL obligations retained; assert a refusal and return to success in the positive case too.",
    "tests/infra/test_role_family_posture.py":"For six full-cluster cases only the fixture selection changed since de424fa4. It still runs the same production verifier with the complete PG inventory.",
    EVENT:"Explicit leaf reinstall, original try/finally, all originally selected reverse/forward operations and assertions retained, no outer transaction. Extra reversible disclosure layer.",
    MODE:"Same committed, populated repeated reverse/forward exercise and all original post-replay effects and assertions. Catalog hash covers an additional routine.",
}

def audit(base):
    changed=git("diff","--name-only",base,HEAD).splitlines()
    if base==BASE: assert all(p.startswith('tests/') for p in changed)
    out={"baseline":base,"head":HEAD,"changed_paths":changed,"files":{}}
    for path in changed:
        if not path.startswith("tests/") or not path.endswith(".py"):continue
        old,new=src(base,path),src(HEAD,path)
        oa,na=items(old),items(new)
        rec={"same":0,"changed":[],"context":DOMAINS.get(path,"See classified assertions and SHA-bound source diff.")}
        if path in (EVENT,MODE):rec["migration_proof"]=migration_body(path,old,new)
        for fn,kind in sorted(oa.keys()|na.keys()):
            orig=list(oa.get((fn,kind),[])); final=list(na.get((fn,kind),[]))
            # Keep the old last equality matched to the new last, so the added
            # immediate post-leaf check is classified separately.
            if path in (EVENT,MODE):
                orig.reverse();final.reverse()
            old_rest=[]
            for l,x in orig:
                i=next((i for i,(_,y) in enumerate(final) if x==y),None)
                if i is None:old_rest.append((l,x))
                else:final.pop(i);rec["same"]+=1
            if path.endswith("test_pooling.py") and fn=="test_runtime_role_defaults_are_pinned_for_transaction_pooling":
                old_rest.sort(key=lambda t:t[1]);final.sort(key=lambda t:t[1])
            assert len(old_rest)<=len(final),(path,fn)
            for i,(line,expr) in enumerate(final):
                ol,oe=old_rest[i] if i<len(old_rest) else (None,None)
                why=reason(path,fn,kind,oe,expr)
                # Keep public test names and source locations, not data or SQL text.
                rec["changed"].append({"test_or_helper":fn,"kind":kind,"old_line":ol,"new_line":line,
                    "old_predicate_sha256":hashlib.sha256(oe.encode()).hexdigest() if oe else None,
                    "new_predicate_sha256":hashlib.sha256(expr.encode()).hexdigest(),"classification":why})
        out["files"][path]=rec
    return out

if __name__=="__main__":
    assert __debug__ and git("rev-parse",HEAD+"^")==BASE
    # This new sibling changes only the test lease lifetime vs the timed-out candidate.
    # Reconstruct the rejected, unpublished tree without requiring its commit
    # object to be fetched. Only the fixture's scope and docstring can differ.
    def oid(kind, data):
        return hashlib.sha1(kind.encode()+b" "+str(len(data)).encode()+b"\0"+data).digest()
    def replace_tree_entry(rev, name, replacement):
        data=subprocess.check_output(["git","cat-file","tree",rev])
        prefix=b" "+name.encode()+b"\0"
        assert data.count(prefix)==1 and len(replacement)==20
        index=data.index(prefix)+len(prefix)
        return oid("tree",data[:index]+replacement+data[index+20:])
    new_fixture=subprocess.check_output(["git","cat-file","blob",HEAD+":tests/isolated_pg_fixtures.py"])
    scope=b'@pytest.fixture(scope="module")'
    doc=b'    """Each consuming module starts with its own freshly migrated cluster."""\n'
    assert new_fixture.count(scope)==new_fixture.count(doc)==1
    original=oid("blob",new_fixture.replace(scope,b'@pytest.fixture(scope="session")').replace(doc,b''))
    assert original.hex()=="a206a9e25003e002409c21e85bfdc13d1c09da56"
    restored=replace_tree_entry(HEAD+":tests","isolated_pg_fixtures.py",original)
    assert replace_tree_entry(HEAD+"^{tree}","tests",restored).hex()=="e63f9382cfab08b289f87430e803dd5c49f63727"
    for former in (WAVE,BASE):
        s=src(former,'tests/infra/test_db_posture_url.py'); t=src(HEAD,'tests/infra/test_db_posture_url.py')
        extract=lambda text:next(v.value.value for v in ast.parse(text).body if isinstance(v,ast.AnnAssign) and ast.unparse(v.target)=='MAKE_TIMEOUT_SECONDS')
        assert extract(s)==extract(t)==15
        pick=lambda text:next(v for v in ast.parse(text).body if isinstance(v,ast.FunctionDef) and v.name=='_run_posture')
        same(pick(s),pick(t))
    report={"comparisons":[audit(BASE),audit(WAVE)]}
    txt=json.dumps(report,sort_keys=True,indent=2)+"\n"
    if len(sys.argv)==2:
        with Path(sys.argv[1]).open("x") as f:f.write(txt)
    else:sys.stdout.write(txt)

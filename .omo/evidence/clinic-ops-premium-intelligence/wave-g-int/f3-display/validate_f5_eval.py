"""In-memory E1 packet, generator wiring and refusal regressions; no evaluation runs."""

import copy
import json
import sys
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

import f5_eval_entry as export
import f5_model_bindings
from artifacts import require


def validate_f5_eval(settings, refused):
    head, tree = settings["reviewed_head"], settings["tree"]
    root = Path("/in-memory/f5/packet")
    historical = export.E.parent.parent / "task-80/fresh-eval-repair"
    started = datetime.fromisoformat(settings["source_committed_at"]) + timedelta(days=1)
    chronology = export.preregistration(settings, started)
    files, models = {}, {}
    for purpose, capability in export.packet.REPORTS.items():
        stem = f"{capability}-{purpose}"
        value = json.loads((historical / (stem + ".json")).read_bytes())
        value["source_commit"] = head
        value["threshold_registration_commit"] = settings["threshold_registration_commit"]
        value["threshold_registration_committed_at"] = settings["threshold_registration_committed_at"]
        value["run_started_at"] = (started + timedelta(seconds=1)).isoformat()
        value["run_finished_at"] = (started + timedelta(seconds=4)).isoformat()
        value["report"]["registered_at"] = (started + timedelta(seconds=2)).isoformat()
        value["report"]["scored_at"] = (started + timedelta(seconds=3)).isoformat()
        value["report"]["versions"].update(settings["source_versions"][purpose])
        # This is an explicitly in-memory positive fixture, never an upgrade
        # of exported historical evidence. The actual exporter checks each
        # generated report's independent fields, without supplying defaults.
        value["report"].update(settings["report_contract"])
        models[purpose] = value["report"]["versions"]
        files[root / (stem + ".json")] = json.dumps(value).encode()
        files[root / (stem + ".html")] = (historical / (stem + ".html")).read_bytes()
    identity = {"commit": head, "tree": tree, "status": ""}
    files.update({
        root / "junit.xml": (historical / "junit.xml").read_bytes(),
        root / "run.log": b"explicitly in-memory generator validation fixture",
        root / "command.json": json.dumps({"argv": export.command(root)}).encode(),
        root / "exit-code.json": b'{"exit_code": 0}',
        root / "preregistration.json": json.dumps(chronology).encode(),
        root / "model-bindings.json": json.dumps({"source_commit": head, "versions": models}).encode(),
        root / "provenance.json": json.dumps({
            "source_before": identity, "source_after": identity, "preparation": settings,
            "generator_runtime_removed": True,
        }).encode(),
    })
    real_read = Path.read_bytes
    real_stat = Path.stat
    real_is_file = Path.is_file

    def read_bytes(path):
        if path.parent == root:
            require(path in files, "Missing in-memory packet file")
            return files[path]
        return real_read(path)

    def write_json(path, value):
        require(path not in files, "An exclusive packet write overwrote evidence")
        files[path] = json.dumps(value).encode()

    with patch.object(Path, "read_bytes", autospec=True, side_effect=read_bytes), \
            patch.object(Path, "write_text", autospec=True, side_effect=lambda path, text:
                         files.__setitem__(path, text.encode())), \
            patch.object(Path, "write_bytes", autospec=True, side_effect=lambda path, data:
                         files.__setitem__(path, data)), \
            patch.object(Path, "chmod"), \
            patch.object(Path, "stat", autospec=True, side_effect=lambda path, **kw:
                         SimpleNamespace(st_size=len(files[path]))
                         if path in files else real_stat(path, **kw)), \
            patch.object(Path, "is_file", autospec=True, side_effect=lambda path:
                         path in files if path.parent == root else real_is_file(path)), \
            patch.object(Path, "is_symlink", return_value=False), \
            patch.object(Path, "iterdir", return_value=iter([])), \
            patch.object(export, "write_json", side_effect=write_json):
        # The real finalizer adds exact head/tree, all file hashes, and verifies the packet.
        with patch.object(Path, "iterdir", side_effect=lambda: iter(list(files))):
            export.finalize(root.parent, {
                "head": head, "cleanup": {"postgres_removed": True},
            })
        require(len([path for path in files if path.name.startswith("AI-")
                     and path.suffix == ".json"]) == 9
                and len([path for path in files if path.name.startswith("AI-")
                         and path.suffix == ".html"]) == 9,
                "The real finalizer lost a purpose or HTML report")
        baseline = copy.deepcopy(files)

        def refresh():
            manifest = json.loads(files[root / "MANIFEST.json"])
            manifest["files"] = [{
                "path": path.name, "bytes": len(content), "sha256": export.sha(content),
            } for path, content in files.items() if path.name != "MANIFEST.json"]
            read = lambda name: read_bytes(root / name)
            manifest["source_bindings"] = export.packet.inspect_reports(read, settings, chronology)
            files[root / "MANIFEST.json"] = json.dumps(manifest).encode()

        def mutate(name, change, *, rehash=True):
            files.clear()
            files.update(copy.deepcopy(baseline))
            value = json.loads(files[root / name])
            change(value)
            files[root / name] = json.dumps(value).encode()
            if rehash:
                refresh()
            refused(lambda: export.verify_packet(root, settings))

        for key in ("prompt", "retrieval", "schema", "model"):
            mutate("AI-04-front_desk.json",
                   lambda value, key=key: value["report"]["versions"].update({key: "0" * 64}))
        mutate("AI-04-front_desk.json", lambda value: value.update(source_tree="0" * 40))
        mutate("provenance.json",
               lambda value: value["source_after"].update(commit="0" * 40))
        mutate("cleanup.json", lambda value: value.update(owned_postgres_removed=False))
        mutate("model-bindings.json", lambda value: value.update(source_commit="0" * 40))
        mutate("MANIFEST.json", lambda value: value.update(source_tree="0" * 40), rehash=False)
        mutate("MANIFEST.json",
               lambda value: value["files"][0].update(bytes=0), rehash=False)
        for field, wrong in (
            ("domain_protocol_sha256", "0"*64), ("review_kind", "interactive_engineering"),
            ("vote_workflow_complete", False), ("human_review_complete", True),
        ):
            mutate("AI-04-front_desk.json",
                   lambda value, field=field, wrong=wrong: value["report"].update({field:wrong}))
            mutate("AI-04-front_desk.json", lambda value, field=field: value["report"].pop(field))
        for field in ("domain_registration_commit","domain_protocol_sha256"):
            mutate("preregistration.json", lambda value, field=field: value.update({field:"0"*64}))
            mutate("MANIFEST.json", lambda value, field=field: value.pop(field), rehash=False)
        for path in baseline:
            files.clear()
            files.update(copy.deepcopy(baseline))
            del files[path]
            refused(lambda: export.verify_packet(root, settings))
        files.clear()
        files.update(copy.deepcopy(baseline))
        for name, content in (
            ("exit-code.json", b'{"exit_code": 1}'),
            ("AI-04-front_desk.html", b""),
            ("junit.xml", baseline[root / "junit.xml"].replace(
                b"<testcase ", b"<testcase ><failure/></testcase><testcase ", 1,
            )),
        ):
            saved = files[root / name]
            files[root / name] = content
            refused(lambda: export.verify_packet(root, settings))
            files[root / name] = saved

    # The plugin reads the real evaluated model while its fixture DB still exists.
    versions_module = ModuleType("apps.ai.evaluation.versions")
    versions_module.candidate_versions = Mock(return_value=models["extraction"])
    models_module = ModuleType("apps.ai.models")
    configuration = SimpleNamespace(purpose="extraction")
    manager = Mock()
    manager.filter.return_value.latest.return_value = configuration
    models_module.ModelConfiguration = SimpleNamespace(objects=manager)
    item = SimpleNamespace(callspec=SimpleNamespace(
        params={"purpose": SimpleNamespace(value="extraction")},
    ))
    with patch.dict(sys.modules, {
        "apps.ai.evaluation.versions": versions_module, "apps.ai.models": models_module,
    }), patch.object(f5_model_bindings, "BINDINGS", {}):
        hook = f5_model_bindings.pytest_runtest_makereport(item, None)
        next(hook)
        try:
            hook.send(SimpleNamespace(get_result=lambda:
                                      SimpleNamespace(when="call", passed=True)))
        except StopIteration:
            pass
        manager.filter.assert_called_once_with(purpose="extraction")
        manager.filter.return_value.latest.assert_called_once_with("version")
        versions_module.candidate_versions.assert_called_once_with(configuration)
        require(f5_model_bindings.BINDINGS["extraction"] == models["extraction"],
                "The plugin did not capture the actual model/prompt/retrieval/schema identity")

    # Exercise the actual generator command/env wiring without spawning or writing anything.
    generated = {}
    runtime = Path("/in-memory/runtime/f5-private")
    private_module = ModuleType("ops.testing.runtime_paths")
    released = []

    @contextmanager
    def runtime_directory(*args, **kwargs):
        require(kwargs["purpose"] == "f5-eval", "The export uses another runtime")
        try:
            yield runtime
        finally:
            released.append(True)

    private_module.runtime_directory = runtime_directory
    current = {"reviewed_head": head, "f5_eval": settings}
    request = {
        "head": head, "f5_eval_sha256": export.sha(json.dumps(settings, sort_keys=True).encode()),
        "preparation_manifest_sha256": "fixture-only",
    }
    runs = Mock(return_value={"exit_code": 0})
    original_read = Path.read_bytes
    # Bind the mock bytes to the same pinned generation path that generate()
    # would actually read, not to the obsolete public symlink name.
    bound_manifest = export.root_manifest(export.E)
    with patch.dict(sys.modules, {"ops.testing.runtime_paths": private_module}), \
            patch.object(export, "root_manifest", return_value=bound_manifest), \
            patch.object(Path, "read_bytes", autospec=True, side_effect=lambda path:
                         json.dumps(current).encode() if path == bound_manifest
                         else original_read(path)), \
            patch.object(Path, "mkdir"), patch.object(Path, "exists", return_value=False), \
            patch.object(export, "source_identity", return_value=identity), \
            patch.object(export, "write_json", side_effect=lambda path, value:
                         generated.__setitem__(path.name, value)), \
            patch.object(export, "run", runs):
        export.generate({"RUNNER_TEMP": "/in-memory/runtime"}, root.parent, request,
                        datetime.now(UTC) + timedelta(hours=1), [])
        require(generated["preregistration.json"]["domain_registration_commit"] == export.DOMAIN_COMMIT
                and generated["preregistration.json"]["domain_protocol_sha256"]
                == settings["domain_protocol_sha256"],
                "The real generator omitted the v2 preregistration")
        argv, env = runs.call_args.args[:2]
        require(argv == export.command(root) and "-p" in argv and "f5_model_bindings" in argv
                and env["CPI80_FRESH_EVAL_DIR"] == str(root)
                and env["CPI80_SOURCE_COMMIT"] == head
                and env["CPI80_THRESHOLD_COMMIT"] == settings["threshold_registration_commit"]
                and env["CPI80_THRESHOLD_COMMITTED_AT"]
                == settings["threshold_registration_committed_at"]
                and env["RUNNER_TEMP"] == str(runtime) and released,
                "The real generator lost export environment, head or private runtime cleanup")
        runs.return_value = {"exit_code": 139}
        refused(lambda: export.generate({"RUNNER_TEMP": "/in-memory/runtime"}, root.parent,
                                       request, datetime.now(UTC) + timedelta(hours=1), []))
        require(generated["exit-code.json"]["exit_code"] == 139 and len(released) == 2,
                "Generator failure lost exit evidence or runtime cleanup")
    return {
        "database_required_by_actual_generator": True,
        "nine_JSON_nine_HTML_head_tree_manifest_hashes": "passed",
        "independent_model_prompt_retrieval_schema_bindings": "passed",
        "missing_each_artifact_and_tampered_head_tree_versions_hashes_refused": "passed",
        "actual_export_command_environment_and_failure_cleanup": "passed",
        "evaluations_run": 0, "databases_started": 0,
    }

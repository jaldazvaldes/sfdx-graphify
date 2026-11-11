"""Member declarations remain traceable after building and loading a snapshot."""
from copy import deepcopy

import pytest

from graphify.salesforce import query
from graphify.salesforce.cli import _text
from graphify.salesforce.schema import DataError, validate_bundle
from graphify.salesforce.service import build_project
from graphify.salesforce.snapshot import load
from graphify.salesforce.viewer import SnapshotView


def test_member_relationships_expose_valid_declaration_evidence(tmp_path):
    project = tmp_path / "project"
    classes = project / "force-app" / "classes"
    classes.mkdir(parents=True)
    (project / "sfdx-project.json").write_text('{"packageDirectories":[{"path":"force-app"}]}', encoding="utf-8")
    (classes / "Worker.cls").write_text("public class Worker { public void run() {} }", encoding="utf-8")
    (classes / "Counter.cls").write_text("public class Counter { public static Integer count; }", encoding="utf-8")
    (classes / "Caller.cls").write_text(
        "public class Caller {\n"
        "    private Worker worker;\n"
        "    public void start() {\n"
        "        this.worker.run();\n"
        "        Counter.count = 0;\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    output = tmp_path / "snapshot"
    build_project(project, out=output)
    bundle = load(output)

    for component, verb, evidence_line, declaration_file, declaration_line in (
        ("Worker.run", "calls", 4, "Caller.cls", 2),
        ("Counter", "references", 5, "Counter.cls", 1),
    ):
        result = query.refs(bundle, component, relation=verb)
        relationship, = result["results"]
        occurrence, = relationship["evidence"]
        assert occurrence["line"] == evidence_line
        location = occurrence["resolution"]["member_declaration"]
        assert (location["path"], location["line"]) == (f"force-app/classes/{declaration_file}", declaration_line)
        assert f"Member declaration: {location['path']}:{declaration_line}" in _text(result)
        viewer = SnapshotView(bundle).evidence(relationship["id"])
        assert viewer["results"][0]["resolution"]["member_declaration"] == location

    tampered = deepcopy(bundle)
    evidence = next(item for item in tampered["facts"]["evidence"] if item["rule"] == "apex.declared_member_call")
    evidence["resolution"]["member_declaration"]["content_hash"] = "0" * 64
    with pytest.raises(DataError, match="Provenance content hash"):
        validate_bundle(tampered)

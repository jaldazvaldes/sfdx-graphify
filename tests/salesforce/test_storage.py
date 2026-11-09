"""Synthetic discovery, provenance state, and atomic storage contracts."""

import copy
import hashlib
import json
import os
import subprocess
import tracemalloc
from pathlib import Path

import pytest

from graphify.salesforce.discovery import discover
from graphify.salesforce.schema import DataError, FactStore, SCHEMA_VERSION, stable_id
from graphify.salesforce.snapshot import default_output, load, publish, source_root, validate_output


FLOW = "force-app/main/default/flows/Example.flow-meta.xml"


def project(path: Path, *, flow=True):
    path.mkdir(parents=True)
    (path / "sfdx-project.json").write_text(
        '{"packageDirectories":[{"path":"force-app"}],"sourceApiVersion":"64.0"}', encoding="utf-8")
    (path / "force-app").mkdir()
    if flow:
        put(path, FLOW, '<Flow xmlns="http://soap.sforce.com/2006/04/metadata"><apiVersion>64.0</apiVersion></Flow>')
    return path


def put(root, name, text):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")
    return path


def git(root, *args):
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, shell=False,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    return result.stdout


def commit(root):
    git(root, "init", "-q")
    git(root, "config", "core.autocrlf", "false")
    git(root, "add", ".")
    git(root, "-c", "user.name=Synthetic Test", "-c", "user.email=fixture@example.invalid",
        "-c", "commit.gpgsign=false", "commit", "-qm", "Synthetic fixture")


def bundle(scan):
    facts = FactStore()
    if FLOW in scan.files:
        facts.ensure_component("flow", "Example")
    value = {"manifest": {"schema_version": SCHEMA_VERSION, "inputs": scan.inputs,
                          "scope": scan.scope, "source_revision": scan.revision,
                          "coverage": {"partial": True}},
             "facts": facts.finish(), "diagnostics": scan.diagnostics}
    value["manifest"]["snapshot_id"] = stable_id("snapshot", value)
    return value


def test_filesystem_scope_and_unsupported_coverage(tmp_path):
    root = project(tmp_path / "project")
    put(root, "force-app/main/default/classes/Example.cls", "public class Example {}")
    put(root, "force-app/main/default/layouts/Account.layout-meta.xml", "<Layout/>")
    put(root, "force-app/node_modules/secret.cls", "excluded")
    put(root, "force-app/.env", "credential")
    put(root, "force-app/key.pem", "credential")
    scan = discover(root)
    assert set(scan.files) == {FLOW, "force-app/main/default/classes/Example.cls"}
    assert "force-app/main/default/layouts/Account.layout-meta.xml" in scan.inputs
    assert all("secret" not in path and ".env" not in path and "pem" not in path for path in scan.inputs)
    assert [d["code"] for d in scan.diagnostics] == ["unsupported_metadata"]
    assert scan.revision is None and scan.dirty is None


def test_reproducible_across_checkout_locations(tmp_path):
    left = discover(project(tmp_path / "left"))
    right = discover(project(tmp_path / "right"))
    assert bundle(left) == bundle(right)
    left_generation = publish(bundle(left), tmp_path / "out-left", left.root)
    right_generation = publish(bundle(right), tmp_path / "out-right", right.root)
    assert {p.name: p.read_bytes() for p in left_generation.iterdir()} == {
        p.name: p.read_bytes() for p in right_generation.iterdir()}
    assert source_root(tmp_path / "out-left") == left.root
    assert str(left.root) not in (left_generation / "facts.json").read_text()


def test_git_current_bytes_dirty_staged_and_untracked(tmp_path):
    root = project(tmp_path / "source")
    commit(root)
    before = discover(root)
    assert before.revision and before.dirty is False
    original = (root / FLOW).read_bytes()
    (root / FLOW).write_bytes(original + b"\n")
    git(root, "add", FLOW)
    (root / FLOW).write_bytes(original)
    # A staged edit is not analyzed when the working file equals HEAD bytes.
    assert discover(root).dirty is False
    (root / FLOW).write_bytes(original + b"\n\n")
    modified = discover(root)
    assert modified.dirty is True
    assert modified.scope["changes_from_head"]["modified"] == 1
    assert modified.files[FLOW] == original + b"\n\n"
    new = "force-app/main/default/classes/NewClass.cls"
    put(root, new, "public class NewClass {}")
    assert new not in discover(root).files
    assert new in discover(root, include_untracked=True).files
    put(root, ".gitignore", "Ignored.cls\n")
    put(root, "force-app/main/default/classes/Ignored.cls", "public class Ignored {}")
    assert not any("Ignored" in name for name in discover(root, include_untracked=True).files)


def test_git_deletion_and_empty_rebuild_remove_obsolete_facts(tmp_path):
    root = project(tmp_path / "source")
    commit(root)
    out = tmp_path / "output"
    publish(bundle(discover(root)), out, root)
    assert load(out)["facts"]["components"]
    (root / FLOW).unlink()
    scan = discover(root)
    assert scan.dirty and scan.scope["changes_from_head"]["deleted"] == 1
    assert scan.files == {}
    publish(bundle(scan), out, root, source_guard=scan.assert_unchanged)
    assert load(out)["facts"]["components"] == []


@pytest.mark.parametrize("change", ["file", "configuration", "new_file"])
def test_mutation_guard_covers_inputs_and_scope(tmp_path, change):
    root = project(tmp_path / "source")
    scan = discover(root)
    if change == "file":
        (root / FLOW).write_bytes(b"changed")
    elif change == "configuration":
        put(root, "sfdx-project.json", '{"packageDirectories":[{"path":"force-app"}],"sourceApiVersion":"63.0"}')
    else:
        put(root, "force-app/main/default/classes/Added.cls", "class Added {}")
    with pytest.raises(DataError, match="changed during"):
        scan.assert_unchanged()


def test_forceignore_applies_and_changes_invalidate_scan(tmp_path):
    root = project(tmp_path / "source")
    put(root, ".forceignore", "# empty policy\n\n")
    scan = discover(root)
    assert scan.files
    put(root, ".forceignore", "**/flows/*\n")
    assert discover(root).files == {}
    with pytest.raises(DataError, match="changed during"):
        scan.assert_unchanged()


@pytest.mark.parametrize("rules,names,expected", [
    ("**/Skip*.cls\n", ["a/SkipOne.cls", "a/Keep.cls", "SkipTwo.cls"], ["a/Keep.cls"]),
    ("ignored/\n", ["ignored/Hidden.cls", "a/ignored/Hidden.cls", "a/Keep.cls"], ["a/Keep.cls"]),
    ("Keep.cls/\n", ["a/Keep.cls", "a/Other.cls"], ["a/Keep.cls", "a/Other.cls"]),
    ("**/*.cls\n!**/Keep.cls\n", ["a/Keep.cls", "a/Drop.cls"], ["a/Keep.cls"]),
    ("force-app/blocked/\n!force-app/blocked/Keep.cls\n",
     ["blocked/Keep.cls", "open/Keep.cls"], ["open/Keep.cls"]),
    ("force-app/*\n!force-app/open/\nforce-app/open/*\n!force-app/open/Keep.cls\n",
     ["blocked/Keep.cls", "open/Keep.cls", "open/Drop.cls"], ["open/Keep.cls"]),
    ("\\#Hash.cls\n\\!Bang.cls\n", ["a/#Hash.cls", "a/!Bang.cls", "a/Keep.cls"], ["a/Keep.cls"]),
    ("/force-app/Root.cls\n", ["Root.cls", "nested/Root.cls"], ["nested/Root.cls"]),
    ("\ufeff# .forceignore v2\r\n**/Drop.cls\r\n", ["Drop.cls", "a/Drop.cls", "a/Keep.cls"], ["a/Keep.cls"]),
    ("!**/node_modules/**\n!**/.env\n", ["node_modules/Hidden.cls", "a/Keep.cls"], ["a/Keep.cls"]),
])
def test_forceignore_common_git_patterns(tmp_path, rules, names, expected):
    root = project(tmp_path / "source", flow=False)
    for name in names:
        put(root, "force-app/" + name, "class Synthetic {}")
    put(root, ".forceignore", rules)
    scan = discover(root)
    assert set(scan.files) == {"force-app/" + name for name in expected}
    assert scan.scope["forceignore"]["salesforce_cli_parity"] == "not_asserted"


def test_forceignore_applies_to_tracked_and_untracked_without_false_deletions(tmp_path):
    root = project(tmp_path / "source")
    tracked = "force-app/main/default/classes/Hidden.cls"
    put(root, tracked, "class Hidden {}")
    put(root, ".forceignore", "Hidden.cls\nNewHidden.cls\n")
    commit(root)
    put(root, "force-app/main/default/classes/NewHidden.cls", "class NewHidden {}")
    scan = discover(root, include_untracked=True)
    assert set(scan.files) == {FLOW}
    assert scan.dirty is False
    assert scan.scope["changes_from_head"] == {"added": 0, "modified": 0, "deleted": 0}
    (root / tracked).unlink()
    assert discover(root).dirty is False


def test_forceignore_whole_package_and_nested_package_cannot_escape_ignored_parent(tmp_path):
    root = project(tmp_path / "source")
    put(root, "sfdx-project.json", json.dumps({"packageDirectories": [{"path": "force-app/main"}]}))
    put(root, ".forceignore", "force-app/\n!force-app/main/\n")
    scan = discover(root)
    assert scan.files == {}
    assert scan.scope["excluded_paths"] == ["force-app/main/"]


def test_legacy_forceignore_directive_rejected(tmp_path):
    root = project(tmp_path / "source")
    put(root, ".forceignore", "# .forceignore v1\n")
    with pytest.raises(DataError, match="Legacy .forceignore v1"):
        discover(root)


@pytest.mark.parametrize("version", [64, "64", " 64.0", "64.0x", "-1.0", True])
def test_invalid_source_api_versions_rejected(tmp_path, version):
    root = project(tmp_path / "source")
    put(root, "sfdx-project.json", json.dumps({"packageDirectories": [{"path": "force-app"}],
                                              "sourceApiVersion": version}))
    with pytest.raises(DataError, match="sourceApiVersion"):
        discover(root)


def test_project_api_version_recorded_without_claiming_coverage(tmp_path):
    root = project(tmp_path / "source")
    assert discover(root).scope["source_api_version"] == "64.0"
    put(root, "sfdx-project.json", '{"packageDirectories":[{"path":"force-app"}],"sourceApiVersion":"65.0"}')
    assert discover(root).scope["source_api_version"] == "65.0"
    put(root, "sfdx-project.json", '{"packageDirectories":[{"path":"force-app"}]}')
    assert discover(root).scope["source_api_version"] is None


@pytest.mark.parametrize("directory", ["../external", "/absolute", "C:/outside"])
def test_package_path_escape_is_rejected(tmp_path, directory):
    root = project(tmp_path / "source")
    put(root, "sfdx-project.json", json.dumps({"packageDirectories": [{"path": directory}]}))
    with pytest.raises(DataError):
        discover(root)


def test_source_link_rejected(tmp_path):
    root = project(tmp_path / "source")
    target = tmp_path / "elsewhere"
    target.mkdir()
    link = root / "force-app" / "linked"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("Symlink creation is unavailable on this host")
    with pytest.raises(DataError, match="links and junctions"):
        discover(root)


@pytest.mark.skipif(os.name != "nt", reason="Windows junction contract")
def test_windows_junctions_rejected_for_source_and_output(tmp_path):
    import _winapi

    root = project(tmp_path / "source")
    target = tmp_path / "external"
    target.mkdir()
    link = root / "force-app" / "junction"
    _winapi.CreateJunction(str(target), str(link))
    with pytest.raises(DataError, match="links and junctions"):
        discover(root)
    with pytest.raises(DataError, match="links or junctions"):
        validate_output(link / "generated")
    source_link = tmp_path / "source-link"
    _winapi.CreateJunction(str(root), str(source_link))
    with pytest.raises(DataError, match="links or junctions"):
        discover(source_link)


def test_output_containment_and_default_policy(tmp_path, monkeypatch):
    root = project(tmp_path / "source")
    with pytest.raises(DataError, match="outside"):
        validate_output(root / "generated", root)
    with pytest.raises(DataError, match="outside"):
        validate_output(tmp_path, root)
    tool_root = Path(__file__).resolve().parents[2]
    with pytest.raises(DataError, match="outside"):
        validate_output(tool_root / "example-output")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    default = default_output(root)
    assert default.is_relative_to(tmp_path / "cache")
    assert not default.is_relative_to(root)


def test_failed_guard_cannot_switch_current(tmp_path):
    root = project(tmp_path / "source")
    out = tmp_path / "output"
    previous = bundle(discover(root))
    publish(previous, out, root)
    pointer = (out / "CURRENT.json").read_bytes()
    put(root, FLOW, "<Flow/>")
    next_bundle = bundle(discover(root))

    def fail():
        raise DataError("Synthetic mutation during publication")

    with pytest.raises(DataError, match="Synthetic mutation"):
        publish(next_bundle, out, root, source_guard=fail)
    assert (out / "CURRENT.json").read_bytes() == pointer
    assert load(out) == previous


def test_failed_validation_and_cross_project_reuse_preserve_current(tmp_path):
    root = project(tmp_path / "source")
    out = tmp_path / "output"
    previous = bundle(discover(root))
    publish(previous, out, root)
    bad = copy.deepcopy(previous)
    bad["facts"]["components"][0]["id"] = "invalid"
    with pytest.raises(DataError):
        publish(bad, out, root)
    other = project(tmp_path / "other")
    with pytest.raises(DataError, match="different source"):
        publish(bundle(discover(other)), out, other)
    assert load(out) == previous


def test_artifact_tampering_and_pointer_traversal_fail(tmp_path):
    root = project(tmp_path / "source")
    out = tmp_path / "output"
    generation = publish(bundle(discover(root)), out, root)
    pointer = json.loads((out / "CURRENT.json").read_text())
    pointer["generation"] = "../../outside"
    (out / "CURRENT.json").write_text(json.dumps(pointer))
    with pytest.raises(DataError, match="generation"):
        load(out)
    publish(bundle(discover(root)), out, root)
    (generation / "references.tsv").write_text("tampered")
    with pytest.raises(DataError, match="integrity"):
        load(out)


def test_repeat_publication_preserves_generation_bytes(tmp_path):
    root = project(tmp_path / "source")
    out = tmp_path / "output"
    scan = discover(root)
    first = publish(bundle(scan), out, root)
    pointer = (out / "CURRENT.json").read_bytes()
    second = publish(bundle(scan), out, root)
    assert first == second
    assert (out / "CURRENT.json").read_bytes() == pointer
    assert len(list((out / "generations").iterdir())) == 1


@pytest.mark.parametrize("kind", ["pointer", "artifact", "source_mapping"])
def test_snapshot_read_size_limits(tmp_path, monkeypatch, kind):
    from graphify.salesforce import snapshot

    root = project(tmp_path / "source")
    out = tmp_path / "output"
    publish(bundle(discover(root)), out, root)
    monkeypatch.setattr(snapshot, "_MAX_ARTIFACT_BYTES" if kind == "artifact" else "_MAX_POINTER_BYTES", 16)
    with pytest.raises(DataError, match="size limit"):
        (source_root if kind == "source_mapping" else load)(out)


def test_artifact_cap_override_retains_every_byte_and_failed_publish_preserves_current(tmp_path, monkeypatch):
    root = project(tmp_path / "source")
    out = tmp_path / "output"
    previous = bundle(discover(root))
    monkeypatch.setenv("SFDX_GRAPHIFY_MAX_ARTIFACT_MIB", "1")
    publish(previous, out, root)
    pointer = (out / "CURRENT.json").read_bytes()

    enlarged = copy.deepcopy(previous)
    enlarged["diagnostics"].append({
        "code": "synthetic_detail", "message": "x" * (1024 * 1024),
        "location": None, "state": "unsupported", "severity": "warning",
    })
    enlarged["manifest"].pop("snapshot_id")
    enlarged["manifest"]["snapshot_id"] = stable_id("snapshot", enlarged)
    with pytest.raises(DataError, match=r"1 MiB: diagnostics.json.*SFDX_GRAPHIFY_MAX_ARTIFACT_MIB"):
        publish(enlarged, out, root)
    assert (out / "CURRENT.json").read_bytes() == pointer
    assert load(out) == previous

    monkeypatch.setenv("SFDX_GRAPHIFY_MAX_ARTIFACT_MIB", "2")
    generation = publish(enlarged, out, root)
    assert load(out) == enlarged
    contents = {path.name: path.read_bytes() for path in generation.iterdir()}
    monkeypatch.setenv("SFDX_GRAPHIFY_MAX_ARTIFACT_MIB", "3")
    copied_generation = publish(enlarged, tmp_path / "second-output", root)
    assert contents == {path.name: path.read_bytes() for path in copied_generation.iterdir()}

    monkeypatch.setenv("SFDX_GRAPHIFY_MAX_ARTIFACT_MIB", "1")
    with pytest.raises(DataError, match=r"1 MiB: diagnostics.json.*SFDX_GRAPHIFY_MAX_ARTIFACT_MIB"):
        load(out)
    assert contents == {path.name: path.read_bytes() for path in generation.iterdir()}
    monkeypatch.setenv("SFDX_GRAPHIFY_MAX_ARTIFACT_MIB", "2")
    assert load(out) == enlarged
    # Raising the resource guard does not bypass integrity validation.
    diagnostics = generation / "diagnostics.json"
    diagnostics.write_bytes(diagnostics.read_bytes().replace(b"synthetic_detail", b"tampered_detail"))
    with pytest.raises(DataError, match="integrity"):
        load(out)


@pytest.mark.parametrize("value", ["", "0", "-1", "1.5", "unlimited", "512MiB", "1e3", "9" * 30])
def test_invalid_artifact_limit_is_rejected_before_validation_or_filesystem_access(tmp_path, monkeypatch, value):
    from graphify.salesforce import snapshot

    monkeypatch.setenv("SFDX_GRAPHIFY_MAX_ARTIFACT_MIB", value)

    def unexpected(*args, **kwargs):
        raise AssertionError("Configuration must be validated before expensive validation or I/O")

    monkeypatch.setattr(snapshot, "validate_bundle", unexpected)
    monkeypatch.setattr(snapshot, "_no_links", unexpected)
    with pytest.raises(DataError, match="SFDX_GRAPHIFY_MAX_ARTIFACT_MIB must be a positive integer"):
        publish({}, tmp_path / "output", tmp_path / "source")
    with pytest.raises(DataError, match="SFDX_GRAPHIFY_MAX_ARTIFACT_MIB must be a positive integer"):
        load(tmp_path / "output")


def test_discovery_preserves_v01_normalized_hash(tmp_path):
    root = project(tmp_path / "source", flow=False)
    contents = {
        ".forceignore": "**/Ignored.cls\n",
        "force-app/main/default/classes/Keep.cls": "public class Keep {}\r\n",
        "force-app/main/default/classes/Ignored.cls": "public class Ignored {}",
        "force-app/main/default/layouts/Account.layout-meta.xml": "<Layout/>\n",
        FLOW: "<Flow/>\n", "force-app/node_modules/Hidden.cls": "hidden",
    }
    for name, content in contents.items():
        put(root, name, content)
    scan = discover(root)
    normalized = {"files": {path: data.hex() for path, data in scan.files.items()},
                  "inputs": scan.inputs, "revision": scan.revision, "dirty": scan.dirty,
                  "scope": scan.scope, "diagnostics": scan.diagnostics}
    # Captured from the v0.1 implementation on this synthetic input before the
    # streaming change; neither checkout roots nor directory traversal order enter it.
    assert stable_id("scan-baseline", normalized) == (
        "scan-baseline:9a7f1a19e61fbb57fb61529e190d6b179cec105eb2219cca3cefa219eb4a72a0")


def test_unsupported_input_hashing_has_bounded_memory(tmp_path):
    root = project(tmp_path / "source")
    path = root / "force-app" / "binary.resource"
    payload = b"synthetic binary payload\0" * 400_000
    expected = hashlib.sha256(payload).hexdigest()
    path.write_bytes(payload)
    size = len(payload)
    del payload
    tracemalloc.start()
    try:
        scan = discover(root)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert scan.inputs["force-app/binary.resource"] == expected
    assert "force-app/binary.resource" not in scan.files
    assert peak < size // 2


@pytest.mark.parametrize("existing_generation", [False, True])
def test_publication_checks_source_once_after_artifacts_are_staged(tmp_path, existing_generation):
    root = project(tmp_path / "source")
    out = tmp_path / "output"
    scan = discover(root)
    value = bundle(scan)
    if existing_generation:
        publish(value, out, root)
    before = (out / "CURRENT.json").read_bytes() if existing_generation else None
    calls = []

    def guard():
        calls.append(True)
        generations = list((out / "generations").iterdir())
        assert len(generations) == 1
        assert {path.name for path in generations[0].iterdir()} == {
            "manifest.json", "facts.json", "diagnostics.json", "references.tsv", "SUMMARY.txt"}
        assert ((out / "CURRENT.json").read_bytes() if (out / "CURRENT.json").exists() else None) == before
        scan.assert_unchanged()

    publish(value, out, root, source_guard=guard)
    assert calls == [True]
    assert load(out) == value


@pytest.mark.parametrize("previous_build", [False, True])
def test_mutation_during_staging_cannot_publish_or_create_source_mapping(tmp_path, monkeypatch, previous_build):
    from graphify.salesforce import snapshot

    root = project(tmp_path / "source")
    out = tmp_path / "output"
    old_bundle = bundle(discover(root))
    if previous_build:
        publish(old_bundle, out, root)
    old_pointer = (out / "CURRENT.json").read_bytes() if previous_build else None
    old_source = (out / "SOURCE.json").read_bytes() if previous_build else None
    put(root, FLOW, "<Flow>candidate</Flow>")
    scan = discover(root)
    original_write = snapshot._write

    def mutate_after_stage(path, content):
        original_write(path, content)
        if path.name == "SUMMARY.txt":
            put(root, FLOW, "<Flow>changed during staging</Flow>")

    monkeypatch.setattr(snapshot, "_write", mutate_after_stage)
    with pytest.raises(DataError, match="changed during"):
        publish(bundle(scan), out, root, source_guard=scan.assert_unchanged)
    assert ((out / "CURRENT.json").read_bytes() if (out / "CURRENT.json").exists() else None) == old_pointer
    assert ((out / "SOURCE.json").read_bytes() if (out / "SOURCE.json").exists() else None) == old_source
    assert len(list((out / "generations").iterdir())) == (2 if previous_build else 1)
    if previous_build:
        assert load(out) == old_bundle
    else:
        assert {path.name for path in out.iterdir()} == {"generations"}


@pytest.mark.parametrize("legacy_version", [1, 2, 3, 4])
def test_legacy_snapshot_remains_readable_and_preserves_pointer_version(tmp_path, legacy_version):
    root = project(tmp_path / "source")
    out = tmp_path / "output"
    legacy = bundle(discover(root))
    legacy["manifest"].pop("snapshot_id")
    legacy["manifest"]["schema_version"] = legacy_version
    legacy["manifest"]["snapshot_id"] = stable_id("snapshot", legacy)
    publish(legacy, out, root)
    assert json.loads((out / "CURRENT.json").read_text())["schema_version"] == legacy_version
    assert load(out) == legacy
    current = bundle(discover(root))
    publish(current, out, root)
    assert json.loads((out / "CURRENT.json").read_text())["schema_version"] == SCHEMA_VERSION
    assert load(out) == current


def test_pointer_schema_must_agree_with_its_generation(tmp_path):
    root = project(tmp_path / "source")
    out = tmp_path / "output"
    publish(bundle(discover(root)), out, root)
    pointer = json.loads((out / "CURRENT.json").read_text())
    pointer["schema_version"] = 1 if pointer["schema_version"] == 2 else 2
    (out / "CURRENT.json").write_text(json.dumps(pointer))
    with pytest.raises(DataError, match="manifest does not match"):
        load(out)

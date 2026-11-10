"""Install only the bundled Salesforce skill in the requested scope."""

from importlib.resources import files
import os
from pathlib import Path

import pytest

from graphify.salesforce import cli


@pytest.fixture
def installation_roots(tmp_path, monkeypatch):
    home = tmp_path / "user profile"
    project = tmp_path / "project folder"
    home.mkdir()
    project.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.chdir(project)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    return home, project


@pytest.mark.parametrize(("args", "scope", "config"), [
    ([], 0, ".claude"),
    (["--platform", "claude", "--project"], 1, ".claude"),
    (["--platform", "codex"], 0, ".agents"),
    (["--platform", "codex", "--project"], 1, ".agents"),
])
def test_installs_one_skill_without_changing_other_files(installation_roots, capsys, args, scope, config):
    home, project = installation_roots
    root = installation_roots[scope] / config
    target = root / "skills" / "sfdx-graphify" / "SKILL.md"
    bundled = files("graphify.salesforce").joinpath("skills").joinpath("sfdx-graphify").joinpath("SKILL.md").read_bytes()
    sentinels = {
        root / "skills" / "graphify" / "SKILL.md": b"Existing Graphify skill\n",
        root / "settings.json": b'{"hooks":{}}\n',
        project / "AGENTS.md": b"Existing instructions\n",
        project / "CLAUDE.md": b"Existing instructions\n",
    }
    for path, content in sentinels.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    assert cli.main(["install", *args]) == 0
    assert str(target) in capsys.readouterr().out
    assert target.read_bytes() == bundled
    timestamp = target.stat().st_mtime_ns
    assert cli.main(["install", *args]) == 0
    assert target.stat().st_mtime_ns == timestamp
    assert {path for base in (home, project) for path in base.rglob("*") if path.is_file()} == {
        *sentinels, target,
    }
    assert all(path.read_bytes() == content for path, content in sentinels.items())


def test_claude_profile_override_only_applies_to_user_scope(installation_roots, monkeypatch):
    home, project = installation_roots
    custom = home / "custom Claude configuration"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(custom))
    assert cli.main(["install", "--platform", "claude"]) == 0
    assert (custom / "skills" / "sfdx-graphify" / "SKILL.md").is_file()
    assert not (home / ".claude").exists()
    assert cli.main(["install", "--project", "--platform", "claude"]) == 0
    assert (project / ".claude" / "skills" / "sfdx-graphify" / "SKILL.md").is_file()


def test_existing_custom_skill_is_preserved(installation_roots, capsys):
    _, project = installation_roots
    target = project / ".agents" / "skills" / "sfdx-graphify" / "SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"My customized skill\n")
    assert cli.main(["install", "--project", "--platform", "codex"]) == 1
    assert target.read_bytes() == b"My customized skill\n"
    assert list(target.parent.iterdir()) == [target]
    error = capsys.readouterr().err
    assert str(target) in error
    assert "--replace" in error


def test_replaces_existing_skill_on_request(installation_roots):
    home, _ = installation_roots
    skills = home / ".claude" / "skills"
    target = skills / "sfdx-graphify" / "SKILL.md"
    sibling = skills / "graphify" / "SKILL.md"
    for path in (target, sibling):
        path.parent.mkdir(parents=True)
        path.write_bytes(b"Previously installed skill\n")

    assert cli.main(["install", "--platform", "claude", "--replace"]) == 0
    bundled = files("graphify.salesforce").joinpath("skills").joinpath("sfdx-graphify").joinpath("SKILL.md").read_bytes()
    assert target.read_bytes() == bundled
    assert sibling.read_bytes() == b"Previously installed skill\n"
    assert list(target.parent.iterdir()) == [target]


@pytest.mark.parametrize("identical", [False, True])
def test_concurrent_install_cannot_replace_existing_skill(installation_roots, monkeypatch, identical):
    _, project = installation_roots
    target = project / ".claude" / "skills" / "sfdx-graphify" / "SKILL.md"
    operation = "rename" if os.name == "nt" else "link"
    original = getattr(os, operation)
    previous = b"A concurrent local skill\n"
    timestamp = None

    def concurrent_write(source, destination):
        nonlocal previous, timestamp
        assert destination == target
        if identical:
            previous = source.read_bytes()
        destination.write_bytes(previous)
        timestamp = destination.stat().st_mtime_ns
        return original(source, destination)

    monkeypatch.setattr(os, operation, concurrent_write)
    assert cli.main(["install", "--project"]) == (0 if identical else 1)
    assert target.read_bytes() == previous
    assert target.stat().st_mtime_ns == timestamp
    assert list(target.parent.iterdir()) == [target]


def test_install_does_not_follow_link_into_existing_graphify_skill(installation_roots):
    home, project = installation_roots
    graphify = home / ".claude" / "skills" / "graphify"
    graphify.mkdir(parents=True)
    existing = graphify / "SKILL.md"
    existing.write_bytes(b"Existing Graphify skill\n")
    link = project / ".claude" / "skills" / "sfdx-graphify"
    link.parent.mkdir(parents=True)
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(graphify), str(link))
    else:
        link.symlink_to(graphify, target_is_directory=True)
    assert cli.main(["install", "--project"]) == 1
    assert cli.main(["install", "--project", "--replace"]) == 1
    assert existing.read_bytes() == b"Existing Graphify skill\n"

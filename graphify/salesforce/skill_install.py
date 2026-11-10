"""Explicit installation of the bundled Salesforce assistant skill."""

from __future__ import annotations

from importlib.resources import files
import os
from pathlib import Path
import tempfile

from .discovery import _linked
from .schema import DataError


_SKILL_NAME = "sfdx-graphify"


def _check_existing(target: Path, content: bytes, *, replace: bool = False) -> bool:
    if _linked(target):
        raise DataError(f"Skill installation paths cannot contain links or junctions: {target}")
    if target.read_bytes() == content:
        return True
    if not replace:
        raise DataError(f"A different skill already exists at {target}. Use --replace to overwrite it.")
    return False


def install_skill(platform: str, *, project: bool, replace: bool = False) -> Path:
    """Copy the bundled skill without changing other assistant configuration."""
    config = ".claude" if platform == "claude" else ".agents"
    root = (Path.cwd() if project else Path.home()) / config
    override = os.environ.get("CLAUDE_CONFIG_DIR") if platform == "claude" and not project else None
    if override:
        root = Path(override).expanduser().absolute()
    target = root / "skills" / _SKILL_NAME / "SKILL.md"
    for path in reversed((target, *target.parents)):
        try:
            if _linked(path):
                raise DataError(f"Skill installation paths cannot contain links or junctions: {path}")
        except FileNotFoundError:
            continue

    content = files("graphify.salesforce").joinpath("skills").joinpath(_SKILL_NAME).joinpath("SKILL.md").read_bytes()
    existing = target.exists()
    if existing and _check_existing(target, content, replace=replace):
        return target

    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=target.parent, prefix=".skill-", suffix=".tmp")
    staged = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
        if existing:
            os.replace(staged, target)
        else:
            # Windows rename refuses an existing destination. POSIX rename would
            # replace it, so use exclusive hard-link creation there.
            publish = os.rename if os.name == "nt" else os.link
            try:
                publish(staged, target)
            except FileExistsError:
                _check_existing(target, content)
    finally:
        staged.unlink(missing_ok=True)
    return target

"""Read a bounded Salesforce DX working tree without executing project code."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, overload

from pathspec import GitIgnoreSpec

from .schema import DataError, stable_id


EXCLUDED_DIRS = frozenset({
    ".git", ".sf", ".sfdx", ".aws", ".azure", ".ssh", ".codex",
    ".claude", ".agents", ".graphify", "graphify-out", "node_modules",
    ".venv", "venv", "__pycache__", ".pytest_cache", "dist", "build",
    "sfdx-graphify-out", "coverage", "logs",
})
_CONFIG_FILES = ("sfdx-project.json", ".forceignore", ".gitignore")
_SUPPORTED = (".flow-meta.xml", ".object-meta.xml", ".field-meta.xml", ".validationrule-meta.xml",
              ".recordtype-meta.xml", ".workflow-meta.xml",
              ".cls", ".trigger", ".trigger-meta.xml")
_MAX_FILE_BYTES = 32 * 1024 * 1024


def lwc_bundle(relative: str) -> tuple[str, str] | None:
    """Select only direct JavaScript modules in an LWC bundle, excluding tests."""
    path = PurePosixPath(relative)
    if (path.suffix.lower() != ".js" or len(path.parts) < 3
            or path.parts[-3] != "lwc" or path.name.lower().endswith((".test.js", ".spec.js"))
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", path.parts[-2])):
        return None
    return path.parts[-2], path.parent.as_posix()


def supported_source(relative: str) -> bool:
    return relative.lower().endswith(_SUPPORTED) or lwc_bundle(relative) is not None


def _linked(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _safe_path(root: Path, relative: str) -> Path:
    """Reject links and traversal before resolving a discovered path."""
    rel = PurePosixPath(relative)
    if rel.is_absolute() or ".." in rel.parts or "\\" in relative or ":" in relative:
        raise DataError(f"Unsafe source path: {relative!r}")
    current = root
    for part in rel.parts:
        current /= part
        try:
            if _linked(current):
                raise DataError(f"Source links and junctions are unsupported: {relative}")
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise DataError(f"Cannot inspect source path: {relative}") from exc
    resolved = current.resolve()
    if not resolved.is_relative_to(root):
        raise DataError(f"Source path escapes the project: {relative}")
    return current


def _bytes(path: Path) -> bytes:
    try:
        if path.stat().st_size > _MAX_FILE_BYTES:
            raise DataError(f"Input exceeds the 32 MiB per-file limit: {path.name}")
        return path.read_bytes()
    except OSError as exc:
        raise DataError(f"Cannot read input {path.name}: {exc.strerror}") from exc


def _capture(path: Path, *, keep_bytes: bool, git_algorithm: str | None = None) -> tuple[bytes | None, str, str | None]:
    """Hash checked input once, retaining only content used by an extractor."""
    chunks: list[bytes] | None = [] if keep_bytes else None
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            if before.st_size > _MAX_FILE_BYTES:
                raise DataError(f"Input exceeds the 32 MiB per-file limit: {path.name}")
            blob = (hashlib.new(git_algorithm, b"blob " + str(before.st_size).encode() + b"\0")
                    if git_algorithm else None)
            size = 0
            while chunk := stream.read(1024 * 1024):
                size += len(chunk)
                if size > _MAX_FILE_BYTES:
                    raise DataError(f"Input exceeds the 32 MiB per-file limit: {path.name}")
                digest.update(chunk)
                if blob is not None:
                    blob.update(chunk)
                if chunks is not None:
                    chunks.append(chunk)
            after = os.fstat(stream.fileno())
            if (size != before.st_size or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                    != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                raise DataError("Source changed while reading an input; retry with a stable working tree")
        return b"".join(chunks) if chunks is not None else None, digest.hexdigest(), blob.hexdigest() if blob else None
    except OSError as exc:
        raise DataError(f"Cannot read input {path.name}: {exc.strerror}") from exc


@overload
def _git(root: Path, args: list[str], *, optional: Literal[False] = False,
         input_data: bytes | None = None, env: dict[str, str] | None = None) -> bytes: ...


@overload
def _git(root: Path, args: list[str], *, optional: Literal[True],
         input_data: bytes | None = None, env: dict[str, str] | None = None) -> bytes | None: ...


def _git(root: Path, args: list[str], *, optional: bool = False,
         input_data: bytes | None = None, env: dict[str, str] | None = None) -> bytes | None:
    try:
        result = subprocess.run(
            ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-C", str(root), *args],
            capture_output=True, check=False, input=input_data, env=env,
            shell=False, timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except FileNotFoundError:
        if optional:
            return None
        raise DataError("Git became unavailable while reading the source") from None
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DataError("Cannot inspect Git source state") from exc
    if result.returncode:
        if optional:
            return None
        raise DataError(f"Cannot inspect Git source state ({args[0]})")
    return result.stdout


def _normalized_changes(root: Path, revision: str, package_dirs: list[str],
                        paths: set[str]) -> tuple[set[str], set[str]]:
    """Compare working bytes with HEAD using Git's built-in normalization.

    A fresh, private index avoids trusting source-index stat/assume-unchanged
    flags and never refreshes or writes the user's index. Attribute entries are
    copied from the source index so missing working .gitattributes files retain
    Git's index fallback. Numstat forces content
    comparison (name-only would report every fresh-index entry as modified).
    External filters, diff drivers, textconv and fsmonitor must never execute.
    Filtered files are returned separately for a conservative raw-byte fallback.
    """
    keys = _git(root, ["config", "--null", "--name-only", "--list"])
    settings = ["-c", "core.sparseCheckout=false", "-c", "index.sparse=false",
                "-c", "core.splitIndex=false"]
    for key in sorted(set(keys.decode("utf-8").split("\0"))):
        if re.fullmatch(r"filter\..*\.(clean|process|required)", key, flags=re.IGNORECASE):
            settings.extend(["-c", key + ("=false" if key.lower().endswith(".required") else "=")])
    filtered: set[str] = set()
    if paths:
        attributes = _git(root, ["check-attr", "-z", "--stdin", "filter"],
                          input_data=b"".join(path.encode("utf-8") + b"\0" for path in sorted(paths)))
        values = attributes.decode("utf-8").split("\0")
        for pos in range(0, len(values) - 1, 3):
            if values[pos + 2] not in {"unspecified", "unset"}:
                filtered.add(values[pos])
    pathspecs = [":(literal)" + path for path in sorted(set(package_dirs) | set(_CONFIG_FILES))]
    attribute_args = ["ls-files", "--stage", "--full-name", "-z", "--",
                      ":(top,glob)**/.gitattributes"]
    source_attributes = _git(root, attribute_args)
    git_root = Path(_git(root, ["rev-parse", "--show-toplevel"]).decode("utf-8").strip())
    with tempfile.TemporaryDirectory(prefix="sfdx-graphify-git-") as temporary:
        hooks = Path(temporary) / "empty-hooks"
        hooks.mkdir()
        # read-tree writes only our private index, but would otherwise still run
        # the source repository's post-index-change hook.
        settings.extend(["-c", "core.hooksPath=" + str(hooks)])
        environment = {**os.environ, "GIT_INDEX_FILE": str(Path(temporary) / "index")}
        _git(root, [*settings, "read-tree", revision], env=environment)
        head_attributes = _git(root, [*settings, *attribute_args], env=environment)
        attribute_paths = {entry.split(b"\t", 1)[1]
                           for entry in head_attributes.split(b"\0") if entry}
        if attribute_paths or source_attributes:
            removals = b"".join(b"0 " + b"0" * len(revision) + b"\t" + path + b"\0"
                                for path in sorted(attribute_paths))
            _git(git_root, [*settings, "update-index", "-z", "--index-info"],
                 env=environment, input_data=removals + source_attributes)
        output = _git(root, [*settings, "diff", revision, "--numstat", "-z", "--relative",
                             "--no-renames", "--no-ext-diff", "--no-textconv", "--", *pathspecs],
                      env=environment)
    changed = {entry.split(b"\t", 2)[2].decode("utf-8")
               for entry in output.split(b"\0") if entry}
    return changed, filtered


def _excluded(relative: str) -> bool:
    path = PurePosixPath(relative)
    if any(part.lower() in EXCLUDED_DIRS for part in path.parts):
        return True
    name = path.name.lower()
    return (
        name == ".env" or name.startswith(".env.")
        or name.endswith((".pem", ".key", ".p12", ".pfx", ".jks"))
        or name in {"auth.json", "credentials.json", "credentials", "id_rsa", "id_ed25519"}
    )


def _in_scope(relative: str, package_dirs: list[str]) -> bool:
    return any(directory == "." or relative.startswith(directory + "/") for directory in package_dirs)


def _force_ignored(spec: GitIgnoreSpec, relative: str, *, directory: bool = False) -> bool:
    """Apply root-relative patterns without re-including pruned parent directories."""
    parts = PurePosixPath(relative).parts
    for count in range(1, len(parts)):
        if spec.match_file("/".join(parts[:count]) + "/"):
            return True
    return spec.match_file(relative + ("/" if directory else ""))


@dataclass
class Scan:
    root: Path
    files: dict[str, bytes]
    inputs: dict[str, str]
    revision: str | None
    dirty: bool | None
    scope: dict
    diagnostics: list[dict]

    def assert_unchanged(self) -> None:
        current = discover(self.root, include_untracked=self.scope["include_untracked"])
        if (self.inputs, self.scope, self.revision) != (current.inputs, current.scope, current.revision):
            raise DataError("Source changed during the build; retry with a stable working tree")


def discover(root: Path, include_untracked: bool = False) -> Scan:
    """Capture current bytes; Git projects select tracked files by default.

    Configuration is always read because it defines selection. Git ignore rules
    apply to optional untracked input through Git itself. .forceignore uses
    tested common Git patterns for all source inputs, including tracked files;
    this is an explicit policy, not a claim of full Salesforce CLI parity.
    """
    original = Path(root).expanduser().absolute()
    if not original.is_dir():
        raise DataError("Source root must be an existing directory")
    for ancestor in (original, *original.parents):
        if _linked(ancestor):
            raise DataError("Source root cannot contain symbolic links or junctions")
    root = original.resolve()
    config: dict[str, bytes] = {}
    for relative in _CONFIG_FILES:
        candidate = _safe_path(root, relative)
        if candidate.is_file():
            config[relative] = _bytes(candidate)
    if "sfdx-project.json" not in config:
        raise DataError("Source root must contain sfdx-project.json")
    try:
        project = json.loads(config["sfdx-project.json"].decode("utf-8-sig"))
        source_api_version = project.get("sourceApiVersion")
        if source_api_version is not None and (
            not isinstance(source_api_version, str)
            or not re.fullmatch(r"[1-9][0-9]*\.[0-9]+", source_api_version)
        ):
            raise ValueError("sourceApiVersion must be a version string such as 64.0")
        directories = project["packageDirectories"]
        if not isinstance(directories, list) or not directories:
            raise ValueError("packageDirectories must be a nonempty array")
        package_dirs = []
        for item in directories:
            value = item["path"]
            if not isinstance(value, str) or not value or value.startswith("/"):
                raise ValueError("invalid package directory")
            value = value.replace("\\", "/").rstrip("/") or "."
            candidate = _safe_path(root, value)
            if candidate.exists() and not candidate.is_dir():
                raise ValueError("package directory is not a directory")
            package_dirs.append(PurePosixPath(value).as_posix())
        package_dirs = sorted(set(package_dirs))
    except (UnicodeError, ValueError, TypeError, KeyError) as exc:
        raise DataError(f"Invalid sfdx-project.json: {exc}") from exc
    try:
        force_lines = config.get(".forceignore", b"").decode("utf-8-sig").splitlines()
    except UnicodeError as exc:
        raise DataError(".forceignore must be UTF-8") from exc
    if any(re.fullmatch(r"\s*#\s*\.forceignore\s+v1\s*", line, flags=re.IGNORECASE) for line in force_lines):
        raise DataError("Legacy .forceignore v1 directives are unsupported; v0.1 uses common Git ignore patterns")
    try:
        force_spec = GitIgnoreSpec.from_lines(force_lines)
    except (ValueError, TypeError) as exc:
        raise DataError(f"Invalid .forceignore pattern: {exc}") from exc

    git_probe = _git(root, ["rev-parse", "--is-inside-work-tree"], optional=True)
    if git_probe is None and any((parent / ".git").exists() for parent in (root, *root.parents)):
        raise DataError("Git source state is unavailable; refusing to broaden discovery to untracked files")
    is_git = git_probe is not None and git_probe.strip() == b"true"
    revision = None
    tracked: set[str] = set()
    selected: set[str] | None = None
    head_files: dict[str, str] = {}
    if is_git:
        head = _git(root, ["rev-parse", "--verify", "HEAD"], optional=True)
        revision = head.decode("ascii").strip() if head else None
        tracked = set(_git(root, ["ls-files", "-z", "--cached"]).decode("utf-8").split("\0")) - {""}
        selected = set(tracked)
        if include_untracked:
            selected.update(_git(root, ["ls-files", "-z", "--others", "--exclude-standard"]).decode("utf-8").split("\0"))
        if revision:
            prefix = _git(root, ["rev-parse", "--show-prefix"]).decode("utf-8").strip()
            for entry in _git(root, ["ls-tree", "-r", "-z", "--full-tree", "HEAD"]).split(b"\0"):
                if not entry:
                    continue
                meta, name = entry.split(b"\t", 1)
                path = name.decode("utf-8")
                if path.startswith(prefix):
                    head_files[path[len(prefix):]] = meta.split()[2].decode("ascii")

    paths: set[str] = set()
    excluded: set[str] = set()
    supported: dict[str, bytes] = {}
    inputs = {relative: hashlib.sha256(content).hexdigest() for relative, content in config.items()}
    blob_hashes: dict[str, str] = {}
    for relative, content in config.items():
        if prior := head_files.get(relative):
            algorithm = "sha256" if len(prior) == 64 else "sha1"
            blob_hashes[relative] = hashlib.new(
                algorithm, b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()

    def walk_error(exc: OSError) -> None:
        raise DataError("Cannot enumerate all selected source directories") from exc

    for directory in package_dirs:
        base = _safe_path(root, directory)
        if directory != "." and (_excluded(directory) or _force_ignored(force_spec, directory, directory=True)):
            excluded.add(directory + "/")
            continue
        if not base.exists():
            continue
        for current, dirs, names in os.walk(base, followlinks=False, onerror=walk_error):
            for name in list(dirs):
                path = Path(current) / name
                relative = path.relative_to(root).as_posix()
                if _excluded(relative) or _force_ignored(force_spec, relative, directory=True):
                    dirs.remove(name)
                    excluded.add(relative + "/")
                else:
                    _safe_path(root, relative)
            for name in names:
                relative = (Path(current) / name).relative_to(root).as_posix()
                if relative in _CONFIG_FILES:
                    continue
                if _excluded(relative) or _force_ignored(force_spec, relative):
                    excluded.add(relative)
                    continue
                if selected is not None and relative not in selected:
                    continue
                if relative in paths:
                    continue
                path = _safe_path(root, relative)
                keep_bytes = supported_source(relative)
                prior = head_files.get(relative)
                algorithm = ("sha256" if len(prior) == 64 else "sha1") if prior else None
                content, digest, blob_digest = _capture(path, keep_bytes=keep_bytes, git_algorithm=algorithm)
                inputs[relative] = digest
                if content is not None:
                    supported[relative] = content
                if blob_digest is not None:
                    blob_hashes[relative] = blob_digest
                paths.add(relative)

    inputs = dict(sorted(inputs.items()))
    supported = dict(sorted(supported.items()))
    unsupported = sorted(paths - supported.keys())
    normalized_changes, filtered_paths = (_normalized_changes(root, revision, package_dirs,
                                                               inputs.keys() & head_files.keys())
                                          if revision else (set(), set()))
    diagnostics: list[dict] = [{
        "code": "unsupported_metadata", "severity": "warning", "path": relative,
        "construct": "metadata_file", "resolution_state": "unsupported",
        "message": "This source file is inventoried for freshness but is outside v0.1 extraction coverage.",
    } for relative in unsupported]
    diagnostics.extend({
        "code": "git_filter_dirty_raw_fallback", "severity": "warning", "path": relative,
        "resolution_state": "unsupported",
        "message": "Custom Git filters are not executed. Dirty state for this file compares raw working-tree bytes with HEAD; filter-normalized equality is not asserted.",
    } for relative in sorted(filtered_paths))
    if not is_git and ".gitignore" in config:
        diagnostics.append({"code": "gitignore_not_applied", "severity": "warning", "path": ".gitignore",
                            "message": "Git ignore semantics require a Git working tree; only built-in exclusions apply."})
    for diagnostic in diagnostics:
        diagnostic["state"] = diagnostic.pop("resolution_state", "unsupported")
        diagnostic["location"] = {"path": diagnostic.pop("path"), "locator": None,
                                  "line": None, "column": None}
        diagnostic["id"] = stable_id("diag", diagnostic)
    counts = {"modified": 0, "deleted": 0, "added": 0}
    dirty = None
    if is_git:
        comparable_head = {p: oid for p, oid in head_files.items()
                           if (p in _CONFIG_FILES or (_in_scope(p, package_dirs)
                                                     and not _force_ignored(force_spec, p))) and not _excluded(p)}
        for relative in inputs:
            prior = comparable_head.get(relative)
            if prior is None:
                counts["added"] += 1
            else:
                counts["modified"] += (blob_hashes[relative] != prior if relative in filtered_paths
                                       else relative in normalized_changes)
        counts["deleted"] = len(set(comparable_head) - inputs.keys())
        dirty = any(counts.values())
    scope = {
        "package_dirs": package_dirs, "include_untracked": bool(include_untracked),
        "source_api_version": source_api_version,
        "input_mode": "working_tree", "selection": "git_tracked" if is_git else "filesystem",
        "configuration_always_included": list(_CONFIG_FILES),
        "forceignore": {"syntax": "common_gitignore_patterns", "engine": "pathspec.GitIgnoreSpec",
                        "base": "project_root", "prune_ignored_directories": True,
                        "salesforce_cli_parity": "not_asserted"},
        "gitignore": "git_untracked" if is_git else "not_applied",
        "excluded_directory_names": sorted(EXCLUDED_DIRS), "excluded_paths": sorted(excluded),
        "unsupported_paths": unsupported, "selected_file_count": len(paths),
        "supported_file_count": len(supported), "changes_from_head": counts,
        "dirty_basis": "git_normalized_working_tree_vs_head_with_raw_filter_fallback" if is_git else "not_available",
    }
    return Scan(root, supported, inputs, revision, dirty, scope, diagnostics)

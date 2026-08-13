from __future__ import annotations

import os
from pathlib import Path
import subprocess
from typing import Iterable

from .models import ResolvedRoot, SessionRecord, ProjectGroup


def parse_path_mapping(value: str) -> tuple[str, str]:
    if "=" not in value:
        raise ValueError("path mapping must use OLD=NEW")
    old, new = value.split("=", 1)
    if not old.strip() or not new.strip():
        raise ValueError("both OLD and NEW paths are required")
    return _lexical_absolute(old), _lexical_absolute(new)


def _lexical_absolute(value: str) -> str:
    expanded = os.path.expanduser(value.strip())
    if not os.path.isabs(expanded):
        raise ValueError(f"path is not absolute: {value}")
    return os.path.normpath(expanded)


def apply_path_mappings(path: str, mappings: Iterable[tuple[str, str]]) -> tuple[str, bool]:
    normalized = _lexical_absolute(path)
    candidates = sorted(mappings, key=lambda item: len(item[0]), reverse=True)
    for old, new in candidates:
        if normalized == old:
            return new, True
        prefix = old.rstrip(os.sep) + os.sep
        if normalized.startswith(prefix):
            suffix = normalized[len(prefix) :]
            return os.path.normpath(os.path.join(new, suffix)), True
    return normalized, False


def _git_root(path: Path) -> Path | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        result = None
    if result is not None and result.returncode == 0 and result.stdout.strip():
        candidate = Path(result.stdout.strip())
        try:
            return candidate.resolve(strict=True)
        except OSError:
            pass

    # Supports normal repositories, worktrees, and submodules (.git may be a
    # file). A mere empty directory named .git is not enough: sandbox mounts,
    # abandoned scaffolds, and corrupted workspaces otherwise create false
    # ancestor matches when git itself is unavailable.
    current = path
    while True:
        marker = current / ".git"
        if marker.is_dir() and (marker / "HEAD").is_file():
            return current
        if marker.is_file():
            try:
                declaration = marker.read_text(encoding="utf-8", errors="strict").strip()
            except (OSError, UnicodeError):
                declaration = ""
            if declaration.startswith("gitdir:") and declaration[7:].strip():
                return current
        if current.parent == current:
            return None
        current = current.parent


def git_root_for_path(path: Path) -> Path | None:
    """Return the enclosing Git root for an existing directory, if any."""

    try:
        canonical = path.resolve(strict=True)
    except OSError:
        return None
    if not canonical.is_dir():
        return None
    return _git_root(canonical)


def _ambiguous_roots(home: Path) -> set[str]:
    roots = {Path("/"), home, home / "Desktop", home / "Documents", home / "Downloads"}
    return {os.path.normcase(os.path.normpath(str(path))) for path in roots}


def resolve_project_root(
    cwd: str,
    *,
    mappings: Iterable[tuple[str, str]] = (),
    home: Path | None = None,
) -> ResolvedRoot:
    try:
        mapped_path, mapped = apply_path_mappings(cwd, mappings)
    except ValueError as exc:
        return ResolvedRoot(cwd, "unresolved", False, reason=str(exc))

    candidate = Path(mapped_path)
    if not candidate.exists() or not candidate.is_dir():
        return ResolvedRoot(
            mapped_path,
            "missing",
            False,
            mapped=mapped,
            reason="directory does not exist; use --map OLD=NEW if it moved",
        )

    try:
        canonical = candidate.resolve(strict=True)
    except OSError as exc:
        return ResolvedRoot(mapped_path, "unresolved", False, mapped=mapped, reason=str(exc))

    root = _git_root(canonical)
    kind = "git" if root is not None else "workspace"
    resolved = root or canonical
    actual_home = (home or Path.home()).resolve()
    normalized = os.path.normcase(os.path.normpath(str(resolved)))
    if normalized in _ambiguous_roots(actual_home):
        return ResolvedRoot(
            str(resolved),
            "ambiguous",
            False,
            mapped=mapped,
            reason="root is too broad for automatic native project creation",
        )
    return ResolvedRoot(str(resolved), kind, True, mapped=mapped)


def group_sessions(
    sessions: Iterable[SessionRecord],
    *,
    mappings: Iterable[tuple[str, str]] = (),
    home: Path | None = None,
) -> list[ProjectGroup]:
    groups: dict[tuple[str, str], ProjectGroup] = {}
    for session in sessions:
        resolved = resolve_project_root(session.cwd, mappings=mappings, home=home)
        key = (resolved.kind, resolved.root)
        group = groups.setdefault(key, ProjectGroup(root=resolved))
        group.sessions.append(session)
        group.cwd_aliases.add(session.cwd)
    return list(groups.values())

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat
import sys
from typing import Any, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import __version__
from .catalog import CatalogError, load_thread_catalog
from .models import Diagnostic, ProjectGroup
from .native_state import (
    NativeStateError,
    NativeStateStore,
    classify_compatibility,
    detect_desktop_version,
    is_desktop_running,
    load_snapshot,
)
from .planner import ImportPlan, PlannerError, UNLINKED_PROJECT_NAME, build_import_plan
from .ranking import DEFAULT_TIMEZONE, rank_projects
from .roots import git_root_for_path, group_sessions, parse_path_mapping, resolve_project_root
from .sessions import scan_sessions


class CliError(RuntimeError):
    pass


@dataclass(frozen=True)
class Layout:
    codex_home: Path
    sessions_dir: Path
    archived_sessions_dir: Path
    state_file: Path
    state_database: Path
    backup_root: Path


def _path(value: str) -> Path:
    return Path(value).expanduser().absolute()


def _layout(args: argparse.Namespace) -> Layout:
    default_home = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser().absolute()
    codex_home = _path(args.codex_home) if args.codex_home else default_home
    return Layout(
        codex_home=codex_home,
        sessions_dir=_path(args.sessions_dir) if args.sessions_dir else codex_home / "sessions",
        archived_sessions_dir=(
            _path(args.archived_sessions_dir)
            if args.archived_sessions_dir
            else codex_home / "archived_sessions"
        ),
        state_file=(
            _path(args.state_file)
            if args.state_file
            else codex_home / ".codex-global-state.json"
        ),
        state_database=(
            _path(args.state_database)
            if args.state_database
            else codex_home / "state_5.sqlite"
        ),
        backup_root=(
            _path(args.backup_root)
            if args.backup_root
            else codex_home / "backups" / "codex-linux-repo-import"
        ),
    )


def _parse_as_of(value: str | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise CliError("--as-of must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _validate_timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise CliError(f"unknown timezone: {value}") from exc
    return value


def _mappings(values: list[str] | None) -> list[tuple[str, str]]:
    try:
        return [parse_path_mapping(value) for value in values or []]
    except ValueError as exc:
        raise CliError(str(exc)) from exc


def _scan_ranked(
    layout: Layout,
    *,
    as_of: datetime,
    timezone_name: str,
    mapping_values: list[str] | None,
) -> tuple[list[ProjectGroup], list[Diagnostic]]:
    sessions, diagnostics = scan_sessions(layout.sessions_dir, layout.archived_sessions_dir)
    groups = group_sessions(sessions, mappings=_mappings(mapping_values))
    return rank_projects(groups, as_of=as_of, timezone_name=timezone_name), diagnostics


def _paths_overlap(first: Path, second: Path) -> bool:
    return first == second or first in second.parents or second in first.parents


def _unlinked_project_root(
    value: str | None,
    *,
    layout: Layout,
    groups: list[ProjectGroup],
    state: dict[str, Any],
) -> str | None:
    if value is None:
        return None
    raw = Path(value).expanduser()
    if not raw.is_absolute():
        raise CliError("--unlinked-project-root must be an absolute path")
    if raw.is_symlink():
        raise CliError("--unlinked-project-root must not be a symlink")

    home_path = Path.home()
    try:
        home = home_path.resolve(strict=True)
    except OSError as exc:
        raise CliError("the unlinked Project requires a real user home directory") from exc
    if home_path != home or home_path.is_symlink():
        raise CliError("the unlinked Project requires a canonical, non-symlink user home")
    desktop_path = home / "Desktop"
    try:
        desktop = desktop_path.resolve(strict=True)
    except OSError as exc:
        raise CliError("the unlinked Project requires an existing ~/Desktop") from exc
    if not desktop.is_dir() or desktop_path.is_symlink():
        raise CliError("the unlinked Project requires a real ~/Desktop directory")

    exists = raw.exists()
    if exists:
        if not raw.is_dir():
            raise CliError("--unlinked-project-root must be a directory or a missing leaf")
        try:
            target = raw.resolve(strict=True)
        except OSError as exc:
            raise CliError(f"cannot resolve --unlinked-project-root: {exc}") from exc
        resolved = resolve_project_root(str(target))
        if resolved.kind == "git":
            raise CliError("--unlinked-project-root must not be inside a Git repository")
    else:
        parent = raw.parent
        if not parent.exists() or not parent.is_dir() or parent.is_symlink():
            raise CliError(
                "--unlinked-project-root may create only one missing leaf under an existing directory"
            )
        try:
            target = parent.resolve(strict=True) / raw.name
        except OSError as exc:
            raise CliError(f"cannot resolve --unlinked-project-root parent: {exc}") from exc
        if target.parent != desktop:
            raise CliError(
                "a missing --unlinked-project-root must be a direct child of ~/Desktop"
            )

    try:
        target.relative_to(desktop)
    except ValueError as exc:
        raise CliError("--unlinked-project-root must be under ~/Desktop") from exc
    if target.parent != desktop:
        raise CliError("--unlinked-project-root must be a direct child of ~/Desktop")
    if git_root_for_path(target.parent) is not None:
        raise CliError("--unlinked-project-root must not be inside a Git repository")

    for label, directory in (
        ("home", home),
        ("Desktop", desktop),
        ("target parent", target.parent),
    ):
        try:
            metadata = directory.stat()
        except OSError as exc:
            raise CliError(f"cannot inspect unlinked {label} directory: {exc}") from exc
        if metadata.st_uid != os.geteuid():
            raise CliError(f"the unlinked {label} directory must be owned by the current user")
        if metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise CliError(f"the unlinked {label} directory must not be group/world writable")
    if exists:
        target_metadata = target.stat()
        if target_metadata.st_uid != os.geteuid():
            raise CliError("the existing unlinked target must be owned by the current user")
        if target_metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise CliError("the existing unlinked target must not be group/world writable")
        if (
            target_metadata.st_mode & stat.S_IWUSR == 0
            or target_metadata.st_mode & stat.S_IXUSR == 0
        ):
            raise CliError("the existing unlinked target must be writable and searchable")

    codex_home = layout.codex_home.resolve(strict=False)
    if _paths_overlap(target, codex_home):
        raise CliError("--unlinked-project-root must not overlap the Codex data directory")

    for group in groups:
        if not group.root.eligible_for_apply:
            continue
        group_root = Path(group.root.root).resolve(strict=False)
        if _paths_overlap(target, group_root):
            raise CliError("--unlinked-project-root overlaps a resolved repository or workspace")

    exact_native_fallback = False
    fallback_named_projects = 0
    target_owner_projects = 0
    projects = state.get("local-projects")
    if isinstance(projects, dict):
        for project in projects.values():
            if not isinstance(project, dict):
                continue
            name = project.get("name")
            roots = project.get("rootPaths")
            canonical_roots = (
                [
                    Path(root).expanduser().resolve(strict=False)
                    for root in roots
                    if isinstance(root, str)
                ]
                if isinstance(roots, list)
                else []
            )
            fallback_name_collision = (
                isinstance(name, str)
                and name.casefold() == UNLINKED_PROJECT_NAME.casefold()
            )
            owns_target = target in canonical_roots
            if fallback_name_collision:
                fallback_named_projects += 1
                if name != UNLINKED_PROJECT_NAME or canonical_roots != [target]:
                    raise CliError(
                        f"the native {UNLINKED_PROJECT_NAME!r} Project must use exactly this one root"
                    )
            if owns_target:
                target_owner_projects += 1
                if name != UNLINKED_PROJECT_NAME or canonical_roots != [target]:
                    raise CliError(
                        "--unlinked-project-root is already owned by a different native Project"
                    )
                exact_native_fallback = True
            for native_root in canonical_roots:
                if native_root == target:
                    continue
                if _paths_overlap(target, native_root):
                    raise CliError("--unlinked-project-root overlaps an existing native Project")
    if fallback_named_projects > 1 or target_owner_projects > 1:
        raise CliError("native state contains duplicate Unlinked Codex Chats Projects")

    if exists and not exact_native_fallback:
        try:
            if any(target.iterdir()):
                raise CliError(
                    "an existing --unlinked-project-root must be empty before first use"
                )
        except OSError as exc:
            raise CliError(f"cannot inspect --unlinked-project-root: {exc}") from exc
    return str(target)


def _project_rows(groups: list[ProjectGroup]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, group in enumerate(groups, 1):
        rows.append(
            {
                "rank": index,
                "score": round(group.score, 2),
                "active_chats": len(group.active_sessions),
                "archived_chats": len(group.archived_sessions),
                "last_chat": group.newest_started_at.isoformat().replace("+00:00", "Z"),
                "kind": group.root.kind,
                "eligible": group.root.eligible_for_apply,
                "reason": group.root.reason,
                "root": group.root.root,
                "metrics": group.metrics,
            }
        )
    return rows


def _print_projects(groups: list[ProjectGroup]) -> None:
    if not groups:
        print("No VS Code Codex extension chats found.")
        return
    print("Rank  Score  Chats  Archived  Kind       Apply  Project root")
    for row in _project_rows(groups):
        apply = "yes" if row["eligible"] else "no"
        print(
            f"{row['rank']:>4}  {row['score']:>5.1f}  {row['active_chats']:>5}  "
            f"{row['archived_chats']:>8}  {row['kind']:<9}  {apply:<5}  {row['root']}"
        )
        if row["reason"]:
            print(f"      reason: {row['reason']}")


def _diagnostic_payload(diagnostics: list[Diagnostic]) -> list[dict[str, Any]]:
    return [item.to_dict() for item in diagnostics]


def _print_unlinked_plan(plan: ImportPlan) -> None:
    root = plan.manifest.get("options", {}).get("unlinked_project_root")
    if not isinstance(root, str):
        return
    changes = plan.manifest.get("native_changes", {})
    creates = changes.get("create_projects", [])
    reuses = changes.get("reuse_projects", [])
    if any(isinstance(item, dict) and item.get("root") == root for item in creates):
        project_action = "create"
    elif any(isinstance(item, dict) and item.get("root") == root for item in reuses):
        project_action = "reuse"
    else:
        project_action = "none (no assignable chats)"

    managed = plan.manifest.get("external_changes", {}).get("managed_directories", [])
    if any(isinstance(item, dict) and item.get("path") == root for item in managed):
        directory_action = "create during apply"
    elif project_action != "none (no assignable chats)" and Path(root).is_dir():
        directory_action = "reuse existing directory"
    else:
        directory_action = "none"

    summary = plan.manifest["summary"]
    print("\nUnlinked fallback")
    print(f"  Target: {root}")
    print(f"  Native Project: {project_action}")
    print(f"  Directory: {directory_action}")
    print(f"  Candidate chats: {summary['unlinked_chats_candidates']}")
    print(f"  Accepted chats: {summary['unlinked_chats_assignable']}")


def _load_catalog(
    layout: Layout,
    diagnostics: list[Diagnostic],
    *,
    strict: bool,
) -> set[str] | None:
    try:
        return load_thread_catalog(layout.state_database)
    except CatalogError as exc:
        if strict:
            raise CliError(str(exc)) from exc
        diagnostics.append(Diagnostic("native_catalog_unavailable", str(exc), str(layout.state_database)))
        return None


def _build_plan(
    args: argparse.Namespace,
    *,
    strict_catalog: bool,
) -> tuple[ImportPlan, list[ProjectGroup]]:
    layout = _layout(args)
    as_of = _parse_as_of(args.as_of)
    timezone_name = _validate_timezone(args.timezone)
    groups, diagnostics = _scan_ranked(
        layout,
        as_of=as_of,
        timezone_name=timezone_name,
        mapping_values=args.map,
    )
    if not groups:
        raise CliError("no VS Code Codex extension chats were found")
    snapshot = load_snapshot(layout.state_file)
    unlinked_root = _unlinked_project_root(
        args.unlinked_project_root,
        layout=layout,
        groups=groups,
        state=snapshot.state,
    )
    known_ids = _load_catalog(
        layout,
        diagnostics,
        strict=strict_catalog or unlinked_root is not None,
    )
    plan = build_import_plan(
        groups,
        snapshot.state,
        before_sha256=snapshot.sha256,
        as_of=as_of,
        timezone_name=timezone_name,
        include_archived=args.include_archived,
        reassign=args.reassign,
        activate_weighted_sort=not args.preserve_native_sort,
        known_thread_ids=known_ids,
        diagnostics=diagnostics,
        unlinked_project_root=unlinked_root,
    )
    return plan, groups


def _command_scan(args: argparse.Namespace) -> int:
    layout = _layout(args)
    as_of = _parse_as_of(args.as_of)
    timezone_name = _validate_timezone(args.timezone)
    groups, diagnostics = _scan_ranked(
        layout,
        as_of=as_of,
        timezone_name=timezone_name,
        mapping_values=args.map,
    )
    if args.json:
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "privacy": {
                        "only_first_session_meta_record_read": True,
                        "message_records_read": False,
                    },
                    "projects": _project_rows(groups),
                    "diagnostics": _diagnostic_payload(diagnostics),
                },
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        _print_projects(groups)
        print(f"\nDiagnostics: {len(diagnostics)}")
    return 0


def _command_plan(args: argparse.Namespace) -> int:
    plan, groups = _build_plan(args, strict_catalog=False)
    if args.json:
        print(json.dumps(plan.manifest, indent=2, ensure_ascii=False))
        return 0
    _print_projects(groups)
    summary = plan.manifest["summary"]
    print("\nNative change plan")
    print(f"  Projects to create: {summary['projects_to_create']}")
    print(f"  Existing projects reused: {summary['projects_to_reuse']}")
    print(f"  Thread assignments to write: {summary['thread_assignments_to_write']}")
    print(f"  Assignment conflicts preserved: {summary['thread_assignment_conflicts']}")
    catalog_unavailable = any(
        item.get("code") == "native_catalog_unavailable" for item in plan.manifest["diagnostics"]
    )
    if catalog_unavailable:
        print("  Native catalog validation: unavailable (see diagnostics)")
    else:
        print(f"  Threads missing from native catalog: {summary['threads_missing_from_native_catalog']}")
    print(f"  Diagnostics: {len(plan.manifest['diagnostics'])}")
    _print_unlinked_plan(plan)
    print("\nDry-run only. No files were changed.")
    return 0


def _command_apply(args: argparse.Namespace) -> int:
    if not args.yes:
        raise CliError("apply requires --yes after you review `plan`")
    layout = _layout(args)
    plan, groups = _build_plan(args, strict_catalog=True)
    missing_catalog = plan.manifest["summary"]["threads_missing_from_native_catalog"]
    if missing_catalog:
        raise CliError(
            f"{missing_catalog} selected chats are absent from the native thread catalog; "
            "apply was stopped"
        )
    managed_directories = plan.manifest.get("external_changes", {}).get(
        "managed_directories", []
    )
    if plan.after_sha256 == plan.before_sha256 and not managed_directories:
        print("No native state changes are needed.")
        return 0
    _print_projects(groups)
    _print_unlinked_plan(plan)
    store = NativeStateStore(layout.state_file, backup_root=layout.backup_root)
    backup = store.apply(plan, allow_untested=args.allow_untested)
    summary = plan.manifest["summary"]
    print(
        f"\nApplied {summary['thread_assignments_to_write']} thread assignments; "
        f"backup: {backup.name}"
    )
    print("Start Codex Desktop and inspect the Projects panel.")
    return 0


def _command_doctor(args: argparse.Namespace) -> int:
    layout = _layout(args)
    findings: dict[str, Any] = {
        "tool_version": __version__,
        "codex_home": str(layout.codex_home),
        "desktop_running": is_desktop_running(),
        "desktop_version": detect_desktop_version(),
    }
    healthy = True
    tested_for_write = False
    try:
        snapshot = load_snapshot(layout.state_file)
        compatibility = classify_compatibility(snapshot.state, findings["desktop_version"])
        findings["native_state"] = {
            "path": str(layout.state_file),
            "sha256": snapshot.sha256,
            "compatibility": compatibility.level,
            "reason": compatibility.reason,
        }
        tested_for_write = compatibility.tested
        healthy = healthy and compatibility.level != "incompatible"
        backup_file = layout.state_file.with_name(layout.state_file.name + ".bak")
        if backup_file.exists():
            backup_snapshot = load_snapshot(backup_file)
            findings["native_state"]["backup_matches"] = backup_snapshot.raw == snapshot.raw
            healthy = healthy and backup_snapshot.raw == snapshot.raw
        else:
            findings["native_state"]["backup_matches"] = None
    except NativeStateError as exc:
        findings["native_state"] = {"path": str(layout.state_file), "error": str(exc)}
        healthy = False

    sessions, diagnostics = scan_sessions(layout.sessions_dir, layout.archived_sessions_dir)
    findings["extension_sessions"] = {
        "active": sum(not item.archived for item in sessions),
        "archived": sum(item.archived for item in sessions),
        "diagnostics": len(diagnostics),
    }
    try:
        findings["native_thread_catalog"] = {
            "path": str(layout.state_database),
            "thread_count": len(load_thread_catalog(layout.state_database)),
        }
    except CatalogError as exc:
        findings["native_thread_catalog"] = {"path": str(layout.state_database), "error": str(exc)}
        healthy = False
    findings["ready_for_offline_apply"] = (
        healthy and tested_for_write and not findings["desktop_running"]
    )

    if args.json:
        print(json.dumps(findings, indent=2, ensure_ascii=False))
    else:
        print(f"Tool: {findings['tool_version']}")
        print(f"Desktop version: {findings['desktop_version'] or 'not detected'}")
        print(f"Desktop running: {'yes' if findings['desktop_running'] else 'no'}")
        state = findings["native_state"]
        if "error" in state:
            print(f"Native state: ERROR — {state['error']}")
        else:
            print(f"Native state: {state['compatibility']} — {state['reason']}")
            print(f"Main/.bak match: {state['backup_matches']}")
        extension = findings["extension_sessions"]
        print(
            f"Extension chats: {extension['active']} active, "
            f"{extension['archived']} archived"
        )
        catalog = findings["native_thread_catalog"]
        print(
            f"Native catalog: {catalog.get('thread_count', 'ERROR')}"
            + (f" — {catalog['error']}" if "error" in catalog else " threads")
        )
        print(f"Ready for offline apply: {'yes' if findings['ready_for_offline_apply'] else 'no'}")
    return 0 if healthy else 2


def _command_backups(args: argparse.Namespace) -> int:
    layout = _layout(args)
    store = NativeStateStore(layout.state_file, backup_root=layout.backup_root)
    entries: list[dict[str, Any]] = []
    for backup in store.backups():
        try:
            manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
            entries.append(
                {
                    "backup_id": backup.name,
                    "created_at": manifest.get("created_at"),
                    "kind": manifest.get("kind"),
                    "tool_version": manifest.get("tool_version"),
                }
            )
        except (OSError, UnicodeError, json.JSONDecodeError):
            entries.append({"backup_id": backup.name, "error": "invalid manifest"})
    if args.json:
        print(json.dumps({"backups": entries}, indent=2, ensure_ascii=False))
    elif not entries:
        print("No importer backups found.")
    else:
        print("Backup id                                  Kind          Created")
        for entry in entries:
            print(
                f"{entry['backup_id']:<42}  {entry.get('kind', 'invalid'):<12}  "
                f"{entry.get('created_at', entry.get('error', 'unknown'))}"
            )
    return 0


def _command_rollback(args: argparse.Namespace) -> int:
    if not args.yes:
        raise CliError("rollback requires --yes")
    layout = _layout(args)
    store = NativeStateStore(layout.state_file, backup_root=layout.backup_root)
    restored, safety = store.rollback(
        args.backup_id,
        allow_untested=args.allow_untested,
    )
    print(f"Rolled back {restored.name}; pre-rollback safety backup: {safety.name}")
    return 0


def _shared_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--codex-home", help="Codex data directory (default: $CODEX_HOME or ~/.codex)")
    parser.add_argument("--sessions-dir", help=argparse.SUPPRESS)
    parser.add_argument("--archived-sessions-dir", help=argparse.SUPPRESS)
    parser.add_argument("--state-file", help=argparse.SUPPRESS)
    parser.add_argument("--state-database", help=argparse.SUPPRESS)
    parser.add_argument("--backup-root", help=argparse.SUPPRESS)
    return parser


def _add_scan_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--map", action="append", metavar="OLD=NEW", help="map a moved absolute path")
    parser.add_argument("--timezone", default=DEFAULT_TIMEZONE, help="calendar timezone for scoring")
    parser.add_argument("--as-of", help="fixed ISO-8601 scoring time (default: now)")


def _add_plan_options(parser: argparse.ArgumentParser) -> None:
    _add_scan_options(parser)
    parser.add_argument("--include-archived", action="store_true", help="also assign archived chats")
    parser.add_argument("--reassign", action="store_true", help="move chats already assigned elsewhere")
    parser.add_argument(
        "--unlinked-project-root",
        metavar="PATH",
        help=(
            "place active chats from ambiguous or missing roots in one "
            f"{UNLINKED_PROJECT_NAME!r} Project"
        ),
    )
    parser.add_argument(
        "--preserve-native-sort",
        action="store_true",
        help="do not switch the native Projects panel to weighted manual order",
    )


def build_parser() -> argparse.ArgumentParser:
    shared = _shared_parser()
    parser = argparse.ArgumentParser(
        prog="codex-linux-repo-import",
        description="Group VS Code Codex chats into native Linux Codex Projects.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    doctor = commands.add_parser("doctor", parents=[shared], help="check compatibility and inputs")
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(handler=_command_doctor)

    scan = commands.add_parser("scan", parents=[shared], help="read metadata and rank projects")
    _add_scan_options(scan)
    scan.add_argument("--json", action="store_true")
    scan.set_defaults(handler=_command_scan)

    plan = commands.add_parser("plan", parents=[shared], help="preview native state changes")
    _add_plan_options(plan)
    plan.add_argument("--json", action="store_true")
    plan.set_defaults(handler=_command_plan)

    apply = commands.add_parser("apply", parents=[shared], help="apply a freshly generated plan")
    _add_plan_options(apply)
    apply.add_argument("--allow-untested", action="store_true", help="allow a schema-compatible build")
    apply.add_argument("--yes", action="store_true", help="confirm offline native state mutation")
    apply.set_defaults(handler=_command_apply)

    backups = commands.add_parser("backups", parents=[shared], help="list importer backups")
    backups.add_argument("--json", action="store_true")
    backups.set_defaults(handler=_command_backups)

    rollback = commands.add_parser("rollback", parents=[shared], help="restore a pre-apply backup")
    rollback.add_argument("backup_id")
    rollback.add_argument("--allow-untested", action="store_true", help="allow a schema-compatible build")
    rollback.add_argument("--yes", action="store_true", help="confirm offline native state mutation")
    rollback.set_defaults(handler=_command_rollback)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (CliError, NativeStateError, PlannerError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
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
from .planner import ImportPlan, build_import_plan
from .ranking import DEFAULT_TIMEZONE, rank_projects
from .roots import group_sessions, parse_path_mapping
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
    known_ids = _load_catalog(layout, diagnostics, strict=strict_catalog)
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
    if plan.after_sha256 == plan.before_sha256:
        print("No native state changes are needed.")
        return 0
    _print_projects(groups)
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
    except (CliError, NativeStateError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

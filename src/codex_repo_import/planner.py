from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from .models import Diagnostic, ProjectGroup, SessionRecord


SIDEBAR_PREFERENCES_KEY = "flat-project-sidebar-preferences-v1"
UNIFIED_PROJECT_ORDER_KEY = "unified-sidebar-project-order-v1"


def encode_state(state: dict[str, Any]) -> bytes:
    return json.dumps(
        state,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _normalized_path(value: str) -> str:
    candidate = Path(value).expanduser()
    try:
        if candidate.exists():
            return os.path.normcase(str(candidate.resolve(strict=True)))
    except OSError:
        pass
    return os.path.normcase(os.path.normpath(str(candidate)))


def _existing_project_for_root(
    projects: dict[str, Any],
    root: str,
) -> tuple[str, dict[str, Any]] | None:
    normalized_root = _normalized_path(root)
    for project_id, project in projects.items():
        if not isinstance(project, dict):
            continue
        root_paths = project.get("rootPaths")
        if not isinstance(root_paths, list):
            continue
        if any(isinstance(path, str) and _normalized_path(path) == normalized_root for path in root_paths):
            return project_id, project
    return None


def _deterministic_project_id(root: str, projects: dict[str, Any]) -> str:
    salt = 0
    while True:
        suffix = "" if salt == 0 else f":{salt}"
        candidate = str(
            uuid5(
                NAMESPACE_URL,
                f"https://github.com/AdamSkutt/codex-linux-repo-import/project/{root}{suffix}",
            )
        )
        existing = projects.get(candidate)
        if existing is None:
            return candidate
        if isinstance(existing, dict) and root in existing.get("rootPaths", []):
            return candidate
        salt += 1


def _project_labels(groups: list[ProjectGroup]) -> dict[str, str]:
    bases: dict[str, list[str]] = {}
    for group in groups:
        base = Path(group.root.root).name or "Project"
        bases.setdefault(base.casefold(), []).append(group.root.root)

    labels: dict[str, str] = {}
    for group in groups:
        root = Path(group.root.root)
        base = root.name or "Project"
        if len(bases[base.casefold()]) == 1:
            labels[group.root.root] = base
            continue
        parent = root.parent.name or str(root.parent)
        labels[group.root.root] = f"{base} · {parent}"
    return labels


def _ordered_apply_sessions(group: ProjectGroup, include_archived: bool) -> list[SessionRecord]:
    sessions = group.sessions if include_archived else group.active_sessions
    return sorted(
        sessions,
        key=lambda session: (-session.started_at.timestamp(), session.thread_id),
    )


def _stable_unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _same_local_project(assignment: object, project_id: str) -> bool:
    return (
        isinstance(assignment, dict)
        and assignment.get("projectKind") == "local"
        and assignment.get("projectId") == project_id
    )


@dataclass
class ImportPlan:
    manifest: dict[str, Any]
    before_state: dict[str, Any]
    after_state: dict[str, Any]
    before_sha256: str
    after_sha256: str


def build_import_plan(
    groups: list[ProjectGroup],
    state: dict[str, Any],
    *,
    before_sha256: str,
    as_of: datetime,
    timezone_name: str,
    include_archived: bool = False,
    reassign: bool = False,
    activate_weighted_sort: bool = True,
    known_thread_ids: set[str] | None = None,
    diagnostics: list[Diagnostic] | None = None,
) -> ImportPlan:
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)
    as_of = as_of.astimezone(timezone.utc)
    now_ms = int(as_of.timestamp() * 1000)
    before = deepcopy(state)
    after = deepcopy(state)

    existing_projects = after.get("local-projects")
    if not isinstance(existing_projects, dict):
        existing_projects = {}
    projects = deepcopy(existing_projects)
    assignments = after.get("thread-project-assignments")
    if not isinstance(assignments, dict):
        assignments = {}
    assignments = deepcopy(assignments)
    projectless_ids = after.get("projectless-thread-ids")
    if not isinstance(projectless_ids, list):
        projectless_ids = []
    projectless_set = {value for value in projectless_ids if isinstance(value, str)}
    sidebar_orders = after.get("sidebar-project-thread-orders")
    if not isinstance(sidebar_orders, dict):
        sidebar_orders = {}
    sidebar_orders = deepcopy(sidebar_orders)

    def applicable_sessions(group: ProjectGroup) -> list[SessionRecord]:
        candidates = _ordered_apply_sessions(group, include_archived)
        if known_thread_ids is None:
            return candidates
        return [session for session in candidates if session.thread_id in known_thread_ids]

    missing_catalog_ids = sorted(
        {
            session.thread_id
            for group in groups
            for session in _ordered_apply_sessions(group, include_archived)
            if known_thread_ids is not None and session.thread_id not in known_thread_ids
        }
    )
    eligible_groups = [
        group for group in groups if group.root.eligible_for_apply and applicable_sessions(group)
    ]
    labels = _project_labels(eligible_groups)
    project_ids: dict[str, str] = {}
    create_projects: list[dict[str, Any]] = []
    reuse_projects: list[dict[str, Any]] = []

    for group in eligible_groups:
        match = _existing_project_for_root(projects, group.root.root)
        if match is not None:
            project_id, existing = match
            project_ids[group.root.root] = project_id
            reuse_projects.append(
                {
                    "project_id": project_id,
                    "name": existing.get("name") or labels[group.root.root],
                    "root": group.root.root,
                }
            )
            continue

        project_id = _deterministic_project_id(group.root.root, projects)
        project = {
            "id": project_id,
            "name": labels[group.root.root],
            "rootPaths": [group.root.root],
            "createdAt": now_ms,
            "updatedAt": now_ms,
        }
        projects[project_id] = project
        project_ids[group.root.root] = project_id
        create_projects.append(
            {"project_id": project_id, "name": project["name"], "root": group.root.root}
        )

    assignment_updates: list[dict[str, Any]] = []
    assignment_unchanged = 0
    assignment_conflicts: list[dict[str, Any]] = []
    accepted_sessions_by_project: dict[str, dict[str, SessionRecord]] = {}
    reassigned_ids: set[str] = set()

    for group in eligible_groups:
        project_id = project_ids[group.root.root]
        accepted_ids: list[str] = []
        for session in applicable_sessions(group):
            desired = {
                "projectKind": "local",
                "projectId": project_id,
                "pendingCoreUpdate": False,
            }
            has_current = session.thread_id in assignments
            current = assignments.get(session.thread_id)
            if _same_local_project(current, project_id):
                assignment_unchanged += 1
                accepted_ids.append(session.thread_id)
                projectless_set.discard(session.thread_id)
                continue
            if has_current and not reassign:
                assignment_conflicts.append(
                    {
                        "thread_id": session.thread_id,
                        "current_project_id": (
                            current.get("projectId") if isinstance(current, dict) else None
                        ),
                        "target_project_id": project_id,
                    }
                )
                # An already assigned thread should never remain in the native
                # projectless set, even when we decline to reassign it.
                projectless_set.discard(session.thread_id)
                continue
            assignments[session.thread_id] = desired
            assignment_updates.append(
                {
                    "thread_id": session.thread_id,
                    "project_id": project_id,
                    "reassigned": has_current,
                }
            )
            if has_current:
                reassigned_ids.add(session.thread_id)
            accepted_ids.append(session.thread_id)
            projectless_set.discard(session.thread_id)
        if accepted_ids:
            accepted = accepted_sessions_by_project.setdefault(project_id, {})
            for session in applicable_sessions(group):
                if session.thread_id in accepted_ids:
                    accepted[session.thread_id] = session

    accepted_project_ids = set(accepted_sessions_by_project)
    newly_created_ids = {item["project_id"] for item in create_projects}
    for orphan_id in newly_created_ids.difference(accepted_project_ids):
        projects.pop(orphan_id, None)
    create_projects = [
        item for item in create_projects if item["project_id"] in accepted_project_ids
    ]
    deduplicated_reuse: dict[str, dict[str, Any]] = {}
    for item in reuse_projects:
        project_id = item["project_id"]
        if project_id in accepted_project_ids:
            deduplicated_reuse.setdefault(project_id, item)
    reuse_projects = list(deduplicated_reuse.values())

    # A reassigned thread must not remain as a ghost in its previous project's
    # manual order. Preserve every unknown field on each order object.
    if reassigned_ids:
        for project_id, order in list(sidebar_orders.items()):
            if not isinstance(order, dict):
                continue
            updated_order = deepcopy(order)
            current_ids = updated_order.get("threadIds", [])
            if isinstance(current_ids, list):
                updated_order["threadIds"] = [
                    value
                    for value in current_ids
                    if isinstance(value, str) and value not in reassigned_ids
                ]
            sidebar_orders[project_id] = updated_order

    ordered_thread_ids_by_project: dict[str, list[str]] = {}
    for project_id, session_map in accepted_sessions_by_project.items():
        ordered_thread_ids_by_project[project_id] = [
            session.thread_id
            for session in sorted(
                session_map.values(),
                key=lambda session: (-session.started_at.timestamp(), session.thread_id),
            )
        ]

    for project_id, imported_ids in ordered_thread_ids_by_project.items():
        current_order = sidebar_orders.get(project_id)
        current_ids = current_order.get("threadIds", []) if isinstance(current_order, dict) else []
        current_ids = [value for value in current_ids if isinstance(value, str)]
        imported_set = set(imported_ids)
        updated_order = deepcopy(current_order) if isinstance(current_order, dict) else {}
        updated_order["threadIds"] = _stable_unique(
            imported_ids + [value for value in current_ids if value not in imported_set]
        )
        sidebar_orders[project_id] = updated_order

    previous_order = after.get("project-order")
    if not isinstance(previous_order, list):
        previous_order = list(existing_projects)
    ranked_ids = _stable_unique(
        [
            project_ids[group.root.root]
            for group in eligible_groups
            if project_ids[group.root.root] in accepted_project_ids
        ]
    )
    ranked_set = set(ranked_ids)
    all_project_ids = list(projects)
    project_order = _stable_unique(
        ranked_ids
        + [value for value in previous_order if isinstance(value, str) and value not in ranked_set]
        + [value for value in all_project_ids if value not in ranked_set]
    )

    preference_change: dict[str, Any] | None = None
    if activate_weighted_sort and ranked_ids:
        atom_state = after.get("electron-persisted-atom-state")
        if not isinstance(atom_state, dict):
            atom_state = {}
        else:
            atom_state = deepcopy(atom_state)
        preferences = atom_state.get(SIDEBAR_PREFERENCES_KEY)
        if not isinstance(preferences, dict):
            preferences = {
                "chatSortMode": "priority",
                "initialized": True,
                "mode": "project",
                "projectSortMode": "priority",
            }
        else:
            preferences = deepcopy(preferences)
        previous_mode = preferences.get("projectSortMode")
        preferences["projectSortMode"] = "manual"
        preferences["initialized"] = True
        preferences.setdefault("mode", "project")
        atom_state[SIDEBAR_PREFERENCES_KEY] = preferences
        unified_order = atom_state.get(UNIFIED_PROJECT_ORDER_KEY)
        if not isinstance(unified_order, list):
            unified_order = []
        ranked_item_keys = [f"codex:project:{project_id}" for project_id in ranked_ids]
        remaining_project_keys = [
            f"codex:project:{project_id}"
            for project_id in project_order
            if project_id not in ranked_set
        ]
        atom_state[UNIFIED_PROJECT_ORDER_KEY] = _stable_unique(
            ranked_item_keys
            + remaining_project_keys
            + [value for value in unified_order if isinstance(value, str)]
        )
        after["electron-persisted-atom-state"] = atom_state
        if previous_mode != "manual":
            preference_change = {"projectSortMode": {"before": previous_mode, "after": "manual"}}

    after["local-projects"] = projects
    after["project-order"] = project_order
    after["thread-project-assignments"] = assignments
    after["projectless-thread-ids"] = [
        value for value in projectless_ids if isinstance(value, str) and value in projectless_set
    ]
    after["sidebar-project-thread-orders"] = sidebar_orders

    after_bytes = encode_state(after)
    after_sha256 = sha256_bytes(after_bytes)
    manifest = {
        "schema_version": 1,
        "generated_at": as_of.isoformat().replace("+00:00", "Z"),
        "timezone": timezone_name,
        "privacy": {
            "message_bodies_read": False,
            "session_files_modified": False,
            "metadata_fields": ["id", "timestamp", "cwd", "originator", "source", "forked_from_id"],
        },
        "options": {
            "include_archived": include_archived,
            "reassign": reassign,
            "activate_weighted_sort": activate_weighted_sort,
        },
        "projects": [
            {
                "root": group.root.root,
                "root_kind": group.root.kind,
                "eligible_for_apply": group.root.eligible_for_apply,
                "ineligible_reason": group.root.reason,
                "metrics": group.metrics,
                "active_chat_count": len(group.active_sessions),
                "archived_chat_count": len(group.archived_sessions),
            }
            for group in groups
        ],
        "native_changes": {
            "create_projects": create_projects,
            "reuse_projects": reuse_projects,
            "assignment_updates": assignment_updates,
            "assignment_unchanged": assignment_unchanged,
            "assignment_conflicts": assignment_conflicts,
            "project_order": project_order,
            "thread_orders": {
                project_id: sidebar_orders[project_id]
                for project_id in ordered_thread_ids_by_project
            },
            "catalog_missing_thread_ids": missing_catalog_ids,
            "unified_project_order": (
                after.get("electron-persisted-atom-state", {}).get(UNIFIED_PROJECT_ORDER_KEY)
                if activate_weighted_sort and ranked_ids
                else None
            ),
            "sidebar_preference_change": preference_change,
        },
        "summary": {
            "extension_chats_found": sum(len(group.sessions) for group in groups),
            "active_extension_chats": sum(len(group.active_sessions) for group in groups),
            "archived_extension_chats": sum(len(group.archived_sessions) for group in groups),
            "ranked_projects": len(groups),
            "eligible_projects": len(ranked_ids),
            "projects_to_create": len(create_projects),
            "projects_to_reuse": len(reuse_projects),
            "thread_assignments_to_write": len(assignment_updates),
            "thread_assignment_conflicts": len(assignment_conflicts),
            "threads_missing_from_native_catalog": len(missing_catalog_ids),
        },
        "state": {"before_sha256": before_sha256, "after_sha256": after_sha256},
        "diagnostics": [item.to_dict() for item in diagnostics or []],
    }
    return ImportPlan(
        manifest=manifest,
        before_state=before,
        after_state=after,
        before_sha256=before_sha256,
        after_sha256=after_sha256,
    )

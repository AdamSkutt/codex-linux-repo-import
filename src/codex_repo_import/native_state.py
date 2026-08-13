from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import pwd
import stat
import subprocess
import tempfile
from typing import Any, Iterator

from . import __version__
from .planner import ImportPlan, UNLINKED_PROJECT_NAME, encode_state, sha256_bytes


TESTED_DESKTOP_VERSIONS = frozenset({"26.803.81509"})
REQUIRED_MIGRATION = "2026-07-13-local-projects"
TOUCHED_PATHS = (
    ("local-projects",),
    ("project-order",),
    ("thread-project-assignments",),
    ("projectless-thread-ids",),
    ("sidebar-project-thread-orders",),
    ("electron-persisted-atom-state", "flat-project-sidebar-preferences-v1"),
    ("electron-persisted-atom-state", "unified-sidebar-project-order-v1"),
)
_MISSING = object()
MANAGED_DIRECTORY_KIND = "create_unlinked_project_root"


class NativeStateError(RuntimeError):
    pass


@dataclass(frozen=True)
class StateSnapshot:
    state: dict[str, Any]
    raw: bytes
    sha256: str
    mode: int = 0o600
    uid: int | None = None
    gid: int | None = None


@dataclass(frozen=True)
class Compatibility:
    level: str
    app_version: str | None
    reason: str

    @property
    def tested(self) -> bool:
        return self.level == "tested"


@dataclass(frozen=True)
class ManagedDirectoryIntent:
    """A narrowly-scoped filesystem mutation declared by an import plan.

    This is intentionally not a callback or a generic file operation.  The
    native-state transaction only knows how to create one private, single-leaf
    workspace directory for otherwise-unlinked Desktop sessions.
    """

    kind: str
    path: Path
    mode: int = 0o700

    def to_manifest(self) -> dict[str, str]:
        return {"kind": self.kind, "path": str(self.path), "mode": "0700"}


@dataclass(frozen=True)
class _PreparedManagedDirectory:
    intent: ManagedDirectoryIntent
    parent: Path
    name: str
    parent_device: int
    parent_inode: int


@dataclass(frozen=True)
class _VerifiedManagedDirectory:
    intent: ManagedDirectoryIntent
    device: int
    inode: int
    mode: int
    require_empty: bool
    require_private_mode: bool


def detect_desktop_version() -> str | None:
    override = os.environ.get("CODEX_DESKTOP_VERSION")
    if override:
        return override.strip()
    try:
        result = subprocess.run(
            ["dpkg-query", "-W", "-f=${Version}", "chatgpt"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None


def validate_native_state(state: object) -> list[str]:
    errors: list[str] = []
    if not isinstance(state, dict):
        return ["top-level state must be a JSON object"]
    required = {
        "local-projects": dict,
        "thread-project-assignments": dict,
        "projectless-thread-ids": list,
        "electron-persisted-atom-state": dict,
        "electron-completed-local-data-migration-ids": list,
    }
    optional = {
        "project-order": list,
        "sidebar-project-thread-orders": dict,
    }
    for key, expected_type in required.items():
        if key not in state:
            errors.append(f"{key} is required")
        elif not isinstance(state[key], expected_type):
            errors.append(f"{key} must be {expected_type.__name__}")
    for key, expected_type in optional.items():
        if key in state and not isinstance(state[key], expected_type):
            errors.append(f"{key} must be {expected_type.__name__}")

    projects = state.get("local-projects", {})
    project_ids = set(projects) if isinstance(projects, dict) else set()
    normalized_roots: dict[str, str] = {}
    if isinstance(projects, dict):
        for project_id, project in projects.items():
            if not isinstance(project_id, str) or not project_id:
                errors.append("local-projects keys must be non-empty strings")
                continue
            if not isinstance(project, dict):
                errors.append(f"local-projects[{project_id!r}] must be an object")
                continue
            if project.get("id") != project_id:
                errors.append(f"local-projects[{project_id!r}].id does not match its key")
            if not isinstance(project.get("name"), str) or not project.get("name"):
                errors.append(f"local-projects[{project_id!r}].name must be a non-empty string")
            roots = project.get("rootPaths")
            if not isinstance(roots, list) or not roots:
                errors.append(f"local-projects[{project_id!r}].rootPaths must be a list")
            else:
                seen_roots: set[str] = set()
                for root in roots:
                    if not isinstance(root, str) or not os.path.isabs(root):
                        errors.append(
                            f"local-projects[{project_id!r}].rootPaths must contain absolute strings"
                        )
                        continue
                    normalized = os.path.normcase(os.path.normpath(root))
                    if normalized in seen_roots:
                        errors.append(f"local-projects[{project_id!r}] contains a duplicate root")
                    seen_roots.add(normalized)
                    previous = normalized_roots.get(normalized)
                    if previous is not None and previous != project_id:
                        errors.append(
                            f"root path {root!r} belongs to more than one local project"
                        )
                    normalized_roots[normalized] = project_id
            for field in ("createdAt", "updatedAt"):
                value = project.get(field)
                if not isinstance(value, int) or isinstance(value, bool):
                    errors.append(f"local-projects[{project_id!r}].{field} must be an integer")

    assignments = state.get("thread-project-assignments", {})
    assigned_ids: set[str] = set()
    if isinstance(assignments, dict):
        for thread_id, assignment in assignments.items():
            if not isinstance(thread_id, str) or not thread_id:
                errors.append("thread-project-assignments keys must be non-empty strings")
                continue
            assigned_ids.add(thread_id)
            if not isinstance(assignment, dict):
                errors.append(f"thread-project-assignments[{thread_id!r}] must be an object")
                continue
            if assignment.get("projectKind") != "local":
                errors.append(
                    f"thread-project-assignments[{thread_id!r}].projectKind must be 'local'"
                )
            project_id = assignment.get("projectId")
            if not isinstance(project_id, str) or project_id not in project_ids:
                errors.append(
                    f"thread-project-assignments[{thread_id!r}] references an unknown project"
                )
            if not isinstance(assignment.get("pendingCoreUpdate"), bool):
                errors.append(
                    f"thread-project-assignments[{thread_id!r}].pendingCoreUpdate must be boolean"
                )

    projectless = state.get("projectless-thread-ids", [])
    if isinstance(projectless, list):
        strings = [value for value in projectless if isinstance(value, str) and value]
        if len(strings) != len(projectless):
            errors.append("projectless-thread-ids must contain only non-empty strings")
        if len(strings) != len(set(strings)):
            errors.append("projectless-thread-ids contains duplicates")
        overlap = assigned_ids.intersection(strings)
        if overlap:
            errors.append("assigned and projectless thread ids overlap")

    project_order = state.get("project-order")
    if isinstance(project_order, list):
        values = [value for value in project_order if isinstance(value, str)]
        if len(values) != len(project_order):
            errors.append("project-order must contain only strings")
        if len(values) != len(set(values)):
            errors.append("project-order contains duplicates")
        unknown = set(values).difference(project_ids)
        if unknown:
            errors.append("project-order references unknown local projects")

    sidebar_orders = state.get("sidebar-project-thread-orders")
    if isinstance(sidebar_orders, dict):
        for project_id, order in sidebar_orders.items():
            if project_id not in project_ids:
                errors.append("sidebar-project-thread-orders references an unknown project")
            if not isinstance(order, dict):
                errors.append(f"sidebar order for {project_id!r} must be an object")
                continue
            thread_ids = order.get("threadIds")
            if not isinstance(thread_ids, list) or not all(
                isinstance(value, str) and value for value in thread_ids
            ):
                errors.append(f"sidebar order for {project_id!r} needs string threadIds")
            elif len(thread_ids) != len(set(thread_ids)):
                errors.append(f"sidebar order for {project_id!r} contains duplicates")
            sort_key = order.get("sortKey")
            if sort_key is not None and sort_key not in {"created_at", "updated_at"}:
                errors.append(f"sidebar order for {project_id!r} has an unknown sortKey")

    atoms = state.get("electron-persisted-atom-state", {})
    if isinstance(atoms, dict):
        preferences = atoms.get("flat-project-sidebar-preferences-v1")
        if preferences is not None:
            if not isinstance(preferences, dict):
                errors.append("flat-project-sidebar-preferences-v1 must be an object")
            elif preferences.get("projectSortMode") not in {"priority", "manual"}:
                errors.append("projectSortMode must be 'priority' or 'manual'")
        unified = atoms.get("unified-sidebar-project-order-v1")
        if unified is not None:
            if not isinstance(unified, list) or not all(isinstance(value, str) for value in unified):
                errors.append("unified-sidebar-project-order-v1 must be a string list")
            elif len(unified) != len(set(unified)):
                errors.append("unified-sidebar-project-order-v1 contains duplicates")
    return errors


def classify_compatibility(state: dict[str, Any], app_version: str | None = None) -> Compatibility:
    errors = validate_native_state(state)
    if errors:
        return Compatibility("incompatible", app_version, "; ".join(errors))
    migrations = state.get("electron-completed-local-data-migration-ids", [])
    version = app_version or detect_desktop_version()
    if not isinstance(migrations, list) or REQUIRED_MIGRATION not in migrations:
        return Compatibility(
            "incompatible",
            version,
            f"missing native project schema marker {REQUIRED_MIGRATION}",
        )
    if version in TESTED_DESKTOP_VERSIONS:
        return Compatibility("tested", version, f"tested desktop build {version}")
    return Compatibility(
        "schema-compatible-untested",
        version,
        "native local-projects migration is present, but this exact desktop build was not tested",
    )


def is_desktop_running(proc_root: Path = Path("/proc")) -> bool:
    own_pid = os.getpid()
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return False
    for entry in entries:
        if not entry.name.isdigit() or int(entry.name) == own_pid:
            continue
        try:
            executable = os.readlink(entry / "exe")
        except OSError:
            executable = ""
        if executable == "/usr/lib/chatgpt/ChatGPT" or Path(executable).name == "ChatGPT":
            return True
        try:
            command = (entry / "cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", "ignore")
        except OSError:
            continue
        if "/usr/lib/chatgpt/ChatGPT" in command:
            return True
    return False


def load_snapshot(state_file: Path) -> StateSnapshot:
    try:
        descriptor = os.open(state_file, os.O_RDONLY)
        with os.fdopen(descriptor, "rb") as handle:
            raw = handle.read()
            metadata = os.fstat(handle.fileno())
    except OSError as exc:
        raise NativeStateError(f"cannot read native state: {exc}") from exc
    try:
        state = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise NativeStateError(f"native state is not valid UTF-8 JSON: {exc}") from exc
    errors = validate_native_state(state)
    if errors:
        raise NativeStateError("native state validation failed: " + "; ".join(errors))
    return StateSnapshot(
        state=state,
        raw=raw,
        sha256=sha256_bytes(raw),
        mode=metadata.st_mode & 0o777,
        uid=metadata.st_uid,
        gid=metadata.st_gid,
    )


def _atomic_write(
    path: Path,
    payload: bytes,
    mode: int | None = None,
    uid: int | None = None,
    gid: int | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(temporary, mode)
        if uid is not None and gid is not None:
            os.chown(temporary, uid, gid)
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary.exists():
            temporary.unlink()


def _path_value(state: dict[str, Any], path: tuple[str, ...]) -> object:
    current: object = state
    for part in path:
        if not isinstance(current, dict) or part not in current:
            return _MISSING
        current = current[part]
    return current


def _journal_value(value: object) -> dict[str, Any]:
    if value is _MISSING:
        return {"present": False}
    return {"present": True, "value": value}


def _restore_path(state: dict[str, Any], path: tuple[str, ...], journal_value: dict[str, Any]) -> None:
    current = state
    for part in path[:-1]:
        child = current.get(part)
        if not isinstance(child, dict):
            child = {}
            current[part] = child
        current = child
    leaf = path[-1]
    if journal_value.get("present") is True:
        current[leaf] = journal_value.get("value")
    else:
        current.pop(leaf, None)


def _without_owned_paths(state: dict[str, Any]) -> dict[str, Any]:
    remainder = deepcopy(state)
    for path in TOUCHED_PATHS:
        current: object = remainder
        for part in path[:-1]:
            if not isinstance(current, dict) or part not in current:
                current = None
                break
            current = current[part]
        if isinstance(current, dict):
            current.pop(path[-1], None)
    return remainder


def _write_private_bytes(path: Path, payload: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            path.unlink()
        except OSError:
            pass
        raise


def _write_private_json(path: Path, value: dict[str, Any]) -> None:
    _write_private_bytes(
        path,
        (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
    )


def _valid_journal_slot(value: object) -> bool:
    if not isinstance(value, dict) or not isinstance(value.get("present"), bool):
        return False
    return value["present"] is False or "value" in value


def _decode_managed_directory_entries(value: object) -> list[ManagedDirectoryIntent]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise NativeStateError("managed_directories must be a list")
    if len(value) > 1:
        raise NativeStateError("only one managed directory is supported per import")

    intents: list[ManagedDirectoryIntent] = []
    for entry in value:
        if not isinstance(entry, dict) or set(entry) != {"kind", "path", "mode"}:
            raise NativeStateError(
                "managed directory entries require exactly kind, path, and mode"
            )
        if entry.get("kind") != MANAGED_DIRECTORY_KIND:
            raise NativeStateError("unsupported managed directory kind")
        raw_path = entry.get("path")
        if not isinstance(raw_path, str) or not raw_path or "\x00" in raw_path:
            raise NativeStateError("managed directory path must be a non-empty string")
        if not os.path.isabs(raw_path) or os.path.normpath(raw_path) != raw_path:
            raise NativeStateError("managed directory path must be canonical and absolute")
        if entry.get("mode") != "0700":
            raise NativeStateError("managed directory mode must be 0700")
        intents.append(
            ManagedDirectoryIntent(
                kind=MANAGED_DIRECTORY_KIND,
                path=Path(raw_path),
            )
        )
    return intents


def _managed_directory_intents(plan: ImportPlan) -> list[ManagedDirectoryIntent]:
    external = plan.manifest.get("external_changes")
    if external is None:
        return []
    if not isinstance(external, dict) or set(external) != {"managed_directories"}:
        raise NativeStateError(
            "external_changes may contain only the managed_directories list"
        )
    return _decode_managed_directory_entries(external.get("managed_directories"))


def _exact_unlinked_project_link(
    state: dict[str, Any],
    intent: ManagedDirectoryIntent,
    *,
    state_label: str,
) -> bool:
    """Require one fixed-name, single-root fallback Project or no linkage.

    A filesystem intent must never be widened by a multi-root Project, an
    ordinary Project that happens to reference the path, or two fallback
    Projects with the same semantic role.  An absent linkage is allowed here
    because ``options.unlinked_project_root`` is also present on plans that
    ultimately have no fallback chats to assign.
    """

    projects = state.get("local-projects", {})
    if not isinstance(projects, dict):
        raise NativeStateError(f"{state_label} local-projects must be an object")
    target = str(intent.path)
    normalized_target = os.path.normcase(os.path.normpath(target))
    exact_ids: list[str] = []
    for project_id, project in projects.items():
        if not isinstance(project, dict):
            continue
        roots = project.get("rootPaths")
        references_target = isinstance(roots, list) and any(
            isinstance(root, str)
            and os.path.normcase(os.path.normpath(root)) == normalized_target
            for root in roots
        )
        name = project.get("name")
        has_fallback_name = name == UNLINKED_PROJECT_NAME
        has_fallback_name_collision = (
            isinstance(name, str)
            and name.casefold() == UNLINKED_PROJECT_NAME.casefold()
        )
        if not references_target and not has_fallback_name_collision:
            continue
        if not has_fallback_name or roots != [target]:
            raise NativeStateError(
                f"{state_label} fallback Project must use the fixed name "
                f"{UNLINKED_PROJECT_NAME!r} and exactly one matching root"
            )
        if not isinstance(project_id, str) or not project_id:
            raise NativeStateError(f"{state_label} fallback Project id is invalid")
        exact_ids.append(project_id)
    if len(exact_ids) > 1:
        raise NativeStateError(f"{state_label} contains duplicate fallback Project linkages")
    return bool(exact_ids)


def _unlinked_project_root_intent(
    plan: ImportPlan,
    managed: list[ManagedDirectoryIntent],
) -> ManagedDirectoryIntent | None:
    options = plan.manifest.get("options")
    raw_path = options.get("unlinked_project_root") if isinstance(options, dict) else None
    if raw_path is None:
        if managed:
            raise NativeStateError(
                "managed directory requires the same options.unlinked_project_root"
            )
        return None
    if not isinstance(raw_path, str) or not raw_path or "\x00" in raw_path:
        raise NativeStateError("unlinked_project_root must be a non-empty absolute path")
    if not os.path.isabs(raw_path) or os.path.normpath(raw_path) != raw_path:
        raise NativeStateError("unlinked_project_root must be canonical and absolute")
    intent = ManagedDirectoryIntent(MANAGED_DIRECTORY_KIND, Path(raw_path))
    if managed and managed != [intent]:
        raise NativeStateError(
            "managed directory does not match options.unlinked_project_root"
        )

    referenced = _exact_unlinked_project_link(
        plan.after_state,
        intent,
        state_label="planned native state",
    )
    if not referenced:
        if managed:
            raise NativeStateError(
                "managed directory is not linked to exactly one planned fallback Project"
            )
        return None
    return intent


def _has_existing_native_unlinked_project(
    plan: ImportPlan,
    intent: ManagedDirectoryIntent,
) -> bool:
    return _exact_unlinked_project_link(
        plan.before_state,
        intent,
        state_label="current native state",
    )


class NativeStateStore:
    def __init__(
        self,
        state_file: Path,
        *,
        backup_root: Path | None = None,
        proc_root: Path = Path("/proc"),
        home: Path | None = None,
    ) -> None:
        self.state_file = state_file
        self.state_backup_file = state_file.with_name(state_file.name + ".bak")
        self.backup_root = backup_root or state_file.parent / "backups" / "codex-linux-repo-import"
        self.proc_root = proc_root
        self.lock_file = state_file.parent / ".codex-linux-repo-import.lock"
        # Do not trust HOME for the production mutation boundary.  Tests may
        # inject an isolated home, while real runs use the login directory of
        # the effective user that will own the new workspace.
        effective_home = home or Path(pwd.getpwuid(os.geteuid()).pw_dir)
        self.home = effective_home.expanduser().absolute()

    def snapshot(self) -> StateSnapshot:
        return load_snapshot(self.state_file)

    def compatibility(self, app_version: str | None = None) -> Compatibility:
        return classify_compatibility(self.snapshot().state, app_version)

    def _guard_apply(
        self,
        *,
        allow_untested: bool,
        app_version: str | None,
    ) -> tuple[Compatibility, StateSnapshot, StateSnapshot | None]:
        if is_desktop_running(self.proc_root):
            raise NativeStateError(
                "Codex Desktop is running. Quit it completely before apply or rollback to avoid a state race."
            )
        current = self.snapshot()
        compatibility = classify_compatibility(current.state, app_version)
        if compatibility.level == "incompatible":
            raise NativeStateError("incompatible native state: " + compatibility.reason)
        if not compatibility.tested and not allow_untested:
            raise NativeStateError(
                compatibility.reason + "; rerun with --allow-untested only after reviewing the dry-run"
            )
        backup_snapshot: StateSnapshot | None = None
        if self.state_backup_file.exists():
            backup_snapshot = load_snapshot(self.state_backup_file)
            if backup_snapshot.raw != current.raw:
                raise NativeStateError(
                    "native main state and .bak differ; start and cleanly quit Codex, then retry"
                )
        return compatibility, current, backup_snapshot

    @contextmanager
    def _lock(self) -> Iterator[None]:
        self.lock_file.parent.mkdir(parents=True, exist_ok=True)
        handle = self.lock_file.open("a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield None
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

    def _prepare_managed_directory(
        self,
        intent: ManagedDirectoryIntent,
        *,
        require_missing: bool,
    ) -> _PreparedManagedDirectory:
        uid = os.geteuid()
        if uid == 0:
            raise NativeStateError("managed directory creation is refused for root")

        try:
            home = self.home.resolve(strict=True)
            home_lstat = self.home.lstat()
        except OSError as exc:
            raise NativeStateError(f"cannot resolve the user home directory: {exc}") from exc
        if self.home != home or stat.S_ISLNK(home_lstat.st_mode):
            raise NativeStateError("the user home path must be canonical and not a symlink")
        if not stat.S_ISDIR(home_lstat.st_mode) or home_lstat.st_uid != uid:
            raise NativeStateError("the user home directory must be owned by the current user")
        if home_lstat.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise NativeStateError("the user home directory must not be group/world-writable")
        if home_lstat.st_mode & stat.S_IXUSR == 0:
            raise NativeStateError("the user home directory must be searchable")

        desktop_path = home / "Desktop"
        try:
            desktop_lstat = desktop_path.lstat()
            desktop = desktop_path.resolve(strict=True)
        except OSError as exc:
            raise NativeStateError(f"cannot resolve the Desktop directory: {exc}") from exc
        if desktop != desktop_path or stat.S_ISLNK(desktop_lstat.st_mode):
            raise NativeStateError("the Desktop directory must be canonical and not a symlink")
        if not stat.S_ISDIR(desktop_lstat.st_mode) or desktop_lstat.st_uid != uid:
            raise NativeStateError("the Desktop directory must be owned by the current user")
        if desktop_lstat.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise NativeStateError("the Desktop directory must not be group/world-writable")
        if desktop_lstat.st_mode & stat.S_IWUSR == 0 or desktop_lstat.st_mode & stat.S_IXUSR == 0:
            raise NativeStateError("the Desktop directory must be writable and searchable")

        target = intent.path
        if not target.name or target.name in {".", ".."}:
            raise NativeStateError("managed directory needs a safe single-leaf name")
        parent_path = target.parent
        try:
            parent_lstat = parent_path.lstat()
            parent = parent_path.resolve(strict=True)
        except OSError as exc:
            raise NativeStateError(f"managed directory parent is unavailable: {exc}") from exc
        if parent != parent_path or stat.S_ISLNK(parent_lstat.st_mode):
            raise NativeStateError("managed directory parent must be canonical and not a symlink")
        if not stat.S_ISDIR(parent_lstat.st_mode) or parent_lstat.st_uid != uid:
            raise NativeStateError("managed directory parent must be owned by the current user")
        if parent_lstat.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise NativeStateError("managed directory parent must not be group/world-writable")
        if parent_lstat.st_mode & stat.S_IWUSR == 0 or parent_lstat.st_mode & stat.S_IXUSR == 0:
            raise NativeStateError("managed directory parent must be writable and searchable")
        if parent != desktop:
            raise NativeStateError(
                "managed directory must be a direct single-leaf child of the user's Desktop"
            )
        if target != parent / target.name:
            raise NativeStateError("managed directory target must use its canonical parent")

        try:
            target_lstat = target.lstat()
        except FileNotFoundError:
            target_lstat = None
        except OSError as exc:
            raise NativeStateError(f"cannot inspect managed directory target: {exc}") from exc
        if require_missing and target_lstat is not None:
            if stat.S_ISLNK(target_lstat.st_mode):
                kind = "symlink"
            elif stat.S_ISDIR(target_lstat.st_mode):
                kind = "existing directory"
            else:
                kind = "non-directory filesystem entry"
            raise NativeStateError(f"managed directory target collides with a {kind}")

        return _PreparedManagedDirectory(
            intent=intent,
            parent=parent,
            name=target.name,
            parent_device=parent_lstat.st_dev,
            parent_inode=parent_lstat.st_ino,
        )

    @staticmethod
    def _directory_open_flags() -> int:
        return (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )

    def _create_managed_directory(
        self,
        prepared: _PreparedManagedDirectory,
    ) -> _VerifiedManagedDirectory:
        parent_fd = os.open(prepared.parent, self._directory_open_flags())
        try:
            opened_parent = os.fstat(parent_fd)
            if (
                opened_parent.st_dev != prepared.parent_device
                or opened_parent.st_ino != prepared.parent_inode
            ):
                raise NativeStateError("managed directory parent changed before creation")
            try:
                os.mkdir(prepared.name, prepared.intent.mode, dir_fd=parent_fd)
            except FileExistsError as exc:
                raise NativeStateError(
                    "managed directory target appeared before creation; no directory was claimed"
                ) from exc

            created_stat = os.stat(prepared.name, dir_fd=parent_fd, follow_symlinks=False)
            if not stat.S_ISDIR(created_stat.st_mode) or created_stat.st_uid != os.geteuid():
                raise NativeStateError("created managed directory failed ownership validation")
            child_fd = os.open(prepared.name, self._directory_open_flags(), dir_fd=parent_fd)
            try:
                opened_child = os.fstat(child_fd)
                if (
                    opened_child.st_dev != created_stat.st_dev
                    or opened_child.st_ino != created_stat.st_ino
                ):
                    raise NativeStateError("managed directory changed during creation")
                os.fchmod(child_fd, prepared.intent.mode)
                opened_child = os.fstat(child_fd)
                if stat.S_IMODE(opened_child.st_mode) != prepared.intent.mode:
                    raise NativeStateError("created managed directory mode is not 0700")
                if os.listdir(child_fd):
                    raise NativeStateError("created managed directory is not empty")
                os.fsync(child_fd)
            finally:
                os.close(child_fd)
            os.fsync(parent_fd)
            return _VerifiedManagedDirectory(
                intent=prepared.intent,
                device=created_stat.st_dev,
                inode=created_stat.st_ino,
                mode=prepared.intent.mode,
                require_empty=True,
                require_private_mode=True,
            )
        finally:
            os.close(parent_fd)

    def _verify_existing_directory(
        self,
        intent: ManagedDirectoryIntent,
        *,
        require_empty: bool,
    ) -> _VerifiedManagedDirectory:
        prepared = self._prepare_managed_directory(intent, require_missing=False)
        try:
            target_lstat = intent.path.lstat()
            resolved = intent.path.resolve(strict=True)
        except OSError as exc:
            raise NativeStateError(f"existing unlinked Project root is unavailable: {exc}") from exc
        if stat.S_ISLNK(target_lstat.st_mode) or resolved != intent.path:
            raise NativeStateError("existing unlinked Project root must not be a symlink")
        if not stat.S_ISDIR(target_lstat.st_mode):
            raise NativeStateError("existing unlinked Project root is not a directory")
        if target_lstat.st_uid != os.geteuid():
            raise NativeStateError("existing unlinked Project root is not owned by the current user")
        target_mode = stat.S_IMODE(target_lstat.st_mode)
        if target_lstat.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise NativeStateError(
                "existing unlinked Project root must not be group/world-writable"
            )
        if target_lstat.st_mode & stat.S_IWUSR == 0 or target_lstat.st_mode & stat.S_IXUSR == 0:
            raise NativeStateError("existing unlinked Project root is not writable and searchable")

        parent_fd = os.open(prepared.parent, self._directory_open_flags())
        try:
            parent_stat = os.fstat(parent_fd)
            if (
                parent_stat.st_dev != prepared.parent_device
                or parent_stat.st_ino != prepared.parent_inode
            ):
                raise NativeStateError("unlinked Project root parent changed during verification")
            child_stat = os.stat(prepared.name, dir_fd=parent_fd, follow_symlinks=False)
            if (
                not stat.S_ISDIR(child_stat.st_mode)
                or child_stat.st_dev != target_lstat.st_dev
                or child_stat.st_ino != target_lstat.st_ino
            ):
                raise NativeStateError("unlinked Project root changed during verification")
            child_fd = os.open(prepared.name, self._directory_open_flags(), dir_fd=parent_fd)
            try:
                opened = os.fstat(child_fd)
                if opened.st_dev != target_lstat.st_dev or opened.st_ino != target_lstat.st_ino:
                    raise NativeStateError("unlinked Project root changed while opening")
                if require_empty and os.listdir(child_fd):
                    raise NativeStateError(
                        "existing unlinked Project root must remain empty before "
                        "first native fallback creation"
                    )
            finally:
                os.close(child_fd)
        finally:
            os.close(parent_fd)
        return _VerifiedManagedDirectory(
            intent=intent,
            device=target_lstat.st_dev,
            inode=target_lstat.st_ino,
            mode=target_mode,
            require_empty=require_empty,
            require_private_mode=False,
        )

    def _reverify_managed_directory(self, verified: _VerifiedManagedDirectory) -> None:
        prepared = self._prepare_managed_directory(verified.intent, require_missing=False)
        parent_fd = os.open(prepared.parent, self._directory_open_flags())
        try:
            current = os.stat(prepared.name, dir_fd=parent_fd, follow_symlinks=False)
            if (
                not stat.S_ISDIR(current.st_mode)
                or current.st_dev != verified.device
                or current.st_ino != verified.inode
                or current.st_uid != os.geteuid()
                or stat.S_IMODE(current.st_mode) != verified.mode
            ):
                raise NativeStateError("unlinked Project root identity, owner, or mode changed")
            if current.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                raise NativeStateError("unlinked Project root became group/world-writable")
            if verified.require_private_mode and verified.mode != verified.intent.mode:
                raise NativeStateError("created managed directory mode is not 0700")
            child_fd = os.open(prepared.name, self._directory_open_flags(), dir_fd=parent_fd)
            try:
                opened = os.fstat(child_fd)
                if opened.st_dev != verified.device or opened.st_ino != verified.inode:
                    raise NativeStateError("unlinked Project root changed while opening")
                if verified.require_empty and os.listdir(child_fd):
                    raise NativeStateError(
                        "unlinked Project root must remain empty before native fallback commit"
                    )
            finally:
                os.close(child_fd)
        finally:
            os.close(parent_fd)

    def _write_mutation_journal(self, backup: Path, plan: ImportPlan) -> None:
        changes = []
        for path in TOUCHED_PATHS:
            before = _path_value(plan.before_state, path)
            after = _path_value(plan.after_state, path)
            if before == after:
                continue
            changes.append(
                {
                    "path": list(path),
                    "before": _journal_value(before),
                    "after": _journal_value(after),
                }
            )
        journal = {
            "schema_version": 1,
            "before_sha256": plan.before_sha256,
            "after_sha256": plan.after_sha256,
            "changes": changes,
        }
        _write_private_json(backup / "mutation.json", journal)

    def _backup_id(self, sha256: str) -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        return f"{stamp}-{sha256[:8]}"

    def _create_backup(
        self,
        snapshot: StateSnapshot,
        state_backup_snapshot: StateSnapshot | None,
        *,
        kind: str,
        details: dict[str, Any] | None = None,
    ) -> Path:
        backup_id = self._backup_id(snapshot.sha256)
        destination = self.backup_root / backup_id
        self.backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.backup_root, 0o700)
        destination.mkdir(mode=0o700, exist_ok=False)
        os.chmod(destination, 0o700)
        _write_private_bytes(destination / "state.json", snapshot.raw)
        backup_was_present = state_backup_snapshot is not None
        if state_backup_snapshot is not None:
            _write_private_bytes(destination / "state.json.bak", state_backup_snapshot.raw)
        manifest = {
            "schema_version": 1,
            "backup_id": backup_id,
            "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "kind": kind,
            "tool_version": __version__,
            "state_file": str(self.state_file),
            "state_sha256": snapshot.sha256,
            "state_backup_was_present": backup_was_present,
            "state_backup_sha256": (
                state_backup_snapshot.sha256 if state_backup_snapshot is not None else None
            ),
            "details": details or {},
        }
        _write_private_json(destination / "manifest.json", manifest)
        return destination

    def _assert_unchanged(
        self,
        expected_main: StateSnapshot,
        expected_backup: StateSnapshot | None,
    ) -> None:
        if is_desktop_running(self.proc_root):
            raise NativeStateError("Codex Desktop started during the operation; no state was written")
        actual_main = self.snapshot()
        if actual_main.sha256 != expected_main.sha256:
            raise NativeStateError("native state changed before commit; no state was written")
        if expected_backup is None:
            if self.state_backup_file.exists():
                raise NativeStateError("native .bak appeared before commit; no state was written")
            return
        if not self.state_backup_file.exists():
            raise NativeStateError("native .bak disappeared before commit; no state was written")
        actual_backup = load_snapshot(self.state_backup_file)
        if actual_backup.sha256 != expected_backup.sha256:
            raise NativeStateError("native .bak changed before commit; no state was written")

    def _restore_exact_pair(
        self,
        main: StateSnapshot,
        backup: StateSnapshot | None,
    ) -> None:
        if backup is None:
            if self.state_backup_file.exists():
                self.state_backup_file.unlink()
                directory_fd = os.open(self.state_backup_file.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        else:
            _atomic_write(
                self.state_backup_file,
                backup.raw,
                backup.mode,
                backup.uid,
                backup.gid,
            )
        # Main is committed last so it remains the authoritative valid copy if
        # restoring the fallback file fails.
        _atomic_write(self.state_file, main.raw, main.mode, main.uid, main.gid)

    def _validate_rollback_material(
        self,
        selected: Path,
        manifest: dict[str, Any],
        target: StateSnapshot,
    ) -> dict[str, Any]:
        if manifest.get("schema_version") != 1 or manifest.get("kind") != "pre-apply":
            raise NativeStateError("only schema-v1 pre-apply backups can be rolled back")
        if manifest.get("backup_id") != selected.name:
            raise NativeStateError("backup id does not match its manifest")
        if manifest.get("state_sha256") != target.sha256:
            raise NativeStateError("backup hash verification failed")
        backup_copy = selected / "state.json.bak"
        backup_was_present = manifest.get("state_backup_was_present") is True
        if backup_was_present != backup_copy.is_file():
            raise NativeStateError("backup .bak presence does not match its manifest")
        if backup_was_present:
            backup_snapshot = load_snapshot(backup_copy)
            if manifest.get("state_backup_sha256") != backup_snapshot.sha256:
                raise NativeStateError("backup .bak hash verification failed")
            if backup_snapshot.raw != target.raw:
                raise NativeStateError("backed-up main state and .bak differ")

        journal_path = selected / "mutation.json"
        if not journal_path.is_file():
            raise NativeStateError("backup has no trusted mutation journal")
        try:
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise NativeStateError(f"cannot read mutation journal: {exc}") from exc
        details = manifest.get("details")
        expected_after = details.get("after_sha256") if isinstance(details, dict) else None
        if (
            not isinstance(journal, dict)
            or journal.get("schema_version") != 1
            or journal.get("before_sha256") != target.sha256
            or journal.get("after_sha256") != expected_after
            or not isinstance(expected_after, str)
        ):
            raise NativeStateError("mutation journal hashes do not match the backup manifest")
        changes = journal.get("changes")
        if not isinstance(changes, list):
            raise NativeStateError("mutation journal changes must be a list")
        allowed = set(TOUCHED_PATHS)
        seen: set[tuple[str, ...]] = set()
        reconstructed = deepcopy(target.state)
        for change in changes:
            if not isinstance(change, dict):
                raise NativeStateError("mutation journal change must be an object")
            raw_path = change.get("path")
            if not isinstance(raw_path, list) or not all(isinstance(part, str) for part in raw_path):
                raise NativeStateError("mutation journal contains an invalid path")
            path = tuple(raw_path)
            if path not in allowed or path in seen:
                raise NativeStateError("mutation journal contains a non-allowlisted or duplicate path")
            seen.add(path)
            before_slot = change.get("before")
            after_slot = change.get("after")
            if not _valid_journal_slot(before_slot) or not _valid_journal_slot(after_slot):
                raise NativeStateError("mutation journal contains an invalid value slot")
            if _journal_value(_path_value(target.state, path)) != before_slot:
                raise NativeStateError("mutation journal before-value does not match the backup")
            _restore_path(reconstructed, path, after_slot)
        reconstructed_errors = validate_native_state(reconstructed)
        if reconstructed_errors or sha256_bytes(encode_state(reconstructed)) != expected_after:
            raise NativeStateError("mutation journal cannot reconstruct its recorded after-state")
        return journal

    def apply(
        self,
        plan: ImportPlan,
        *,
        allow_untested: bool = False,
        app_version: str | None = None,
    ) -> Path:
        with self._lock():
            intents = _managed_directory_intents(plan)
            unlinked_root_intent = _unlinked_project_root_intent(plan, intents)
            compatibility, current, current_backup = self._guard_apply(
                allow_untested=allow_untested,
                app_version=app_version,
            )
            if current.sha256 != plan.before_sha256:
                raise NativeStateError(
                    "native state changed after the plan was built; generate a fresh plan before applying"
                )
            if plan.before_state != current.state:
                raise NativeStateError("plan before-state does not match the current native state")
            if _without_owned_paths(plan.before_state) != _without_owned_paths(plan.after_state):
                raise NativeStateError("plan attempts to change native state outside the importer allowlist")
            payload = encode_state(plan.after_state)
            if sha256_bytes(payload) != plan.after_sha256:
                raise NativeStateError("internal error: plan after-state hash does not match")
            after_errors = validate_native_state(plan.after_state)
            if after_errors:
                raise NativeStateError(
                    "planned native state is invalid: " + "; ".join(after_errors)
                )
            existing_native_link = (
                _has_existing_native_unlinked_project(plan, unlinked_root_intent)
                if unlinked_root_intent is not None
                else False
            )
            prepared_directories = [
                self._prepare_managed_directory(intent, require_missing=True)
                for intent in intents
            ]
            verified_existing_directories = (
                [
                    self._verify_existing_directory(
                        unlinked_root_intent,
                        require_empty=not existing_native_link,
                    )
                ]
                if unlinked_root_intent is not None and not intents
                else []
            )
            backup = self._create_backup(
                current,
                current_backup,
                kind="pre-apply",
                details={
                    "compatibility": compatibility.level,
                    "app_version": compatibility.app_version,
                    "plan_summary": plan.manifest.get("summary", {}),
                    "after_sha256": plan.after_sha256,
                    "managed_directories": [intent.to_manifest() for intent in intents],
                },
            )
            self._write_mutation_journal(backup, plan)
            verified_directories = list(verified_existing_directories)
            native_write_started = False
            try:
                for prepared in prepared_directories:
                    verified_directories.append(self._create_managed_directory(prepared))

                # This second CAS happens after the outside-Codex mutation but
                # before either native copy is committed.
                self._assert_unchanged(current, current_backup)
                for verified_directory in verified_directories:
                    self._reverify_managed_directory(verified_directory)
                backup_metadata = current_backup or current
                native_write_started = True
                _atomic_write(
                    self.state_backup_file,
                    payload,
                    backup_metadata.mode,
                    backup_metadata.uid,
                    backup_metadata.gid,
                )
                _atomic_write(
                    self.state_file,
                    payload,
                    current.mode,
                    current.uid,
                    current.gid,
                )
                verified = self.snapshot()
                verified_backup = load_snapshot(self.state_backup_file)
                if (
                    verified.sha256 != plan.after_sha256
                    or verified_backup.sha256 != plan.after_sha256
                ):
                    raise NativeStateError("post-write hash verification failed")
                # The native Project must never be committed if its root was
                # swapped, loosened, or populated during the two atomic writes.
                # On failure the native pair is restored, while the directory
                # and any content are deliberately preserved.
                for verified_directory in verified_directories:
                    self._reverify_managed_directory(verified_directory)
            # KeyboardInterrupt/SystemExit are BaseException subclasses. They
            # must restore the pair too; otherwise Ctrl+C between the two
            # renames can leave main and .bak on different generations.
            except BaseException as exc:
                if native_write_started:
                    try:
                        self._restore_exact_pair(current, current_backup)
                    except BaseException as restore_exc:
                        if hasattr(exc, "add_note"):
                            exc.add_note(f"native pair restoration also failed: {restore_exc}")
                raise
            return backup

    def backups(self) -> list[Path]:
        if not self.backup_root.exists():
            return []
        return sorted(
            [path for path in self.backup_root.iterdir() if (path / "manifest.json").is_file()],
            reverse=True,
        )

    def rollback(
        self,
        backup_id: str,
        *,
        allow_untested: bool = False,
        app_version: str | None = None,
    ) -> tuple[Path, Path]:
        if Path(backup_id).name != backup_id:
            raise NativeStateError("backup id must be a single directory name")
        selected = self.backup_root / backup_id
        if selected.parent != self.backup_root or not (selected / "manifest.json").is_file():
            raise NativeStateError(f"backup not found: {backup_id}")
        try:
            manifest = json.loads((selected / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise NativeStateError(f"cannot read backup manifest: {exc}") from exc
        target = load_snapshot(selected / "state.json")
        journal = self._validate_rollback_material(selected, manifest, target)
        with self._lock():
            _, current, current_backup = self._guard_apply(
                allow_untested=allow_untested,
                app_version=app_version,
            )
            restored_state = deepcopy(current.state)
            conflicts: list[str] = []
            for change in journal["changes"]:
                path = tuple(change["path"])
                actual = _journal_value(_path_value(current.state, path))
                if actual != change["after"]:
                    conflicts.append(".".join(path))
                    continue
                _restore_path(restored_state, path, change["before"])
            if conflicts:
                raise NativeStateError(
                    "rollback stopped because Codex changed importer-owned keys after apply: "
                    + ", ".join(conflicts)
                )
            restored_errors = validate_native_state(restored_state)
            if restored_errors:
                raise NativeStateError(
                    "rollback would create invalid native state: " + "; ".join(restored_errors)
                )
            restored_payload = encode_state(restored_state)
            safety_backup = self._create_backup(
                current,
                current_backup,
                kind="pre-rollback",
                details={"restore_backup_id": backup_id},
            )
            self._assert_unchanged(current, current_backup)
            try:
                backup_metadata = current_backup or current
                _atomic_write(
                    self.state_backup_file,
                    restored_payload,
                    backup_metadata.mode,
                    backup_metadata.uid,
                    backup_metadata.gid,
                )
                _atomic_write(
                    self.state_file,
                    restored_payload,
                    current.mode,
                    current.uid,
                    current.gid,
                )
                expected_hash = sha256_bytes(restored_payload)
                verified = self.snapshot()
                verified_backup = load_snapshot(self.state_backup_file)
                if verified.sha256 != expected_hash or verified_backup.sha256 != expected_hash:
                    raise NativeStateError("rollback verification failed")
            except BaseException:
                self._restore_exact_pair(current, current_backup)
                raise
            return selected, safety_backup

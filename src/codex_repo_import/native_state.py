from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Any, Iterator

from . import __version__
from .planner import ImportPlan, encode_state, sha256_bytes


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


class NativeStateStore:
    def __init__(
        self,
        state_file: Path,
        *,
        backup_root: Path | None = None,
        proc_root: Path = Path("/proc"),
    ) -> None:
        self.state_file = state_file
        self.state_backup_file = state_file.with_name(state_file.name + ".bak")
        self.backup_root = backup_root or state_file.parent / "backups" / "codex-linux-repo-import"
        self.proc_root = proc_root
        self.lock_file = state_file.parent / ".codex-linux-repo-import.lock"

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
            backup = self._create_backup(
                current,
                current_backup,
                kind="pre-apply",
                details={
                    "compatibility": compatibility.level,
                    "app_version": compatibility.app_version,
                    "plan_summary": plan.manifest.get("summary", {}),
                    "after_sha256": plan.after_sha256,
                },
            )
            self._write_mutation_journal(backup, plan)
            self._assert_unchanged(current, current_backup)
            try:
                backup_metadata = current_backup or current
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
            # KeyboardInterrupt/SystemExit are BaseException subclasses. They
            # must restore the pair too; otherwise Ctrl+C between the two
            # renames can leave main and .bak on different generations.
            except BaseException:
                self._restore_exact_pair(current, current_backup)
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

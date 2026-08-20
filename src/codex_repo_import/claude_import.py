from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import queue
import shutil
import sqlite3
import stat
import subprocess
import threading
import time
from typing import Any, Iterator, Sequence

from . import __version__


MAX_CLAUDE_LINE_BYTES = 32 * 1024 * 1024
MAX_CLAUDE_SESSION_BYTES = 512 * 1024 * 1024
MAX_IMPORT_LEDGER_BYTES = 64 * 1024 * 1024
SAFE_MESSAGE_RECORD_LIMIT = 16_000
DEFAULT_APP_SERVER_TIMEOUT = 300.0


class ClaudeImportError(RuntimeError):
    pass


@dataclass(frozen=True)
class ClaudeDiagnostic:
    code: str
    message: str
    path: str | None = None

    def to_dict(self) -> dict[str, str]:
        payload = {"code": self.code, "message": self.message}
        if self.path is not None:
            payload["path"] = self.path
        return payload


@dataclass(frozen=True)
class ImportedSessionReference:
    source_path: Path
    target_id: str | None
    content_sha256: str | None
    imported_at_ms: int | None
    target_rollout_path: Path | None = None


@dataclass(frozen=True)
class ClaudeSessionCandidate:
    source_path: Path
    source_cwd: str
    target_cwd: str
    size_bytes: int
    modified_at_ns: int
    content_sha256: str
    message_records: int
    first_timestamp: datetime | None
    last_timestamp: datetime | None
    status: str = "pending"
    blocked_reason: str | None = None
    imported_thread_id: str | None = None
    target_rollout_path: Path | None = None

    @property
    def eligible(self) -> bool:
        return self.status == "pending"

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "source_path": str(self.source_path),
            "source_cwd": self.source_cwd,
            "target_cwd": self.target_cwd,
            "status": self.status,
            "blocked_reason": self.blocked_reason,
            "size_bytes": self.size_bytes,
            "message_records": self.message_records,
            "first_timestamp": _format_timestamp(self.first_timestamp),
            "last_timestamp": _format_timestamp(self.last_timestamp),
        }


@dataclass(frozen=True)
class ClaudeImportPlan:
    source_root: Path
    candidates: tuple[ClaudeSessionCandidate, ...]
    diagnostics: tuple[ClaudeDiagnostic, ...]

    @property
    def pending(self) -> list[ClaudeSessionCandidate]:
        return [item for item in self.candidates if item.status == "pending"]

    @property
    def imported(self) -> list[ClaudeSessionCandidate]:
        return [item for item in self.candidates if item.status == "imported"]

    @property
    def blocked(self) -> list[ClaudeSessionCandidate]:
        return [item for item in self.candidates if item.status == "blocked"]

    def to_public_dict(self) -> dict[str, Any]:
        workspaces = sorted({item.target_cwd for item in self.pending})
        return {
            "schema_version": 1,
            "provider": "claude-code",
            "source_root": str(self.source_root),
            "source_read_only": True,
            "conversion_engine": "codex-app-server-external-migration",
            "summary": {
                "discovered": len(self.candidates),
                "pending": len(self.pending),
                "already_imported": len(self.imported),
                "blocked": len(self.blocked),
                "workspaces": len(workspaces),
            },
            "sessions": [item.to_public_dict() for item in self.candidates],
            "diagnostics": [item.to_dict() for item in self.diagnostics],
        }


@dataclass(frozen=True)
class ClaudeImportResult:
    source_path: Path
    status: str
    imported_thread_id: str | None = None
    message: str | None = None

    def to_public_dict(self) -> dict[str, str | None]:
        return {
            "source_path": str(self.source_path),
            "status": self.status,
            "imported_thread_id": self.imported_thread_id,
            "message": self.message,
        }


@dataclass(frozen=True)
class ClaudeImportBackup:
    path: Path
    manifest: dict[str, Any]


def _format_timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _has_message_content(record: dict[str, Any]) -> bool:
    message = record.get("message")
    if not isinstance(message, dict):
        return False
    content = message.get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        return bool(content)
    return False


def _canonical_child(path: Path, root: Path) -> Path:
    try:
        canonical = path.resolve(strict=True)
    except OSError as exc:
        raise ClaudeImportError(f"cannot resolve Claude session path {path}: {exc}") from exc
    try:
        canonical.relative_to(root)
    except ValueError as exc:
        raise ClaudeImportError(f"Claude session escapes the configured source root: {path}") from exc
    return canonical


def _scan_claude_file(
    path: Path,
    *,
    allow_large_sessions: bool,
) -> tuple[ClaudeSessionCandidate | None, list[ClaudeDiagnostic]]:
    diagnostics: list[ClaudeDiagnostic] = []
    try:
        metadata = path.stat()
    except OSError as exc:
        return None, [ClaudeDiagnostic("source_stat_error", str(exc), str(path))]
    if not stat.S_ISREG(metadata.st_mode):
        return None, [ClaudeDiagnostic("source_not_regular", "source is not a regular file", str(path))]
    if metadata.st_uid != os.geteuid():
        return None, [
            ClaudeDiagnostic(
                "source_not_owned",
                "Claude session is not owned by the current user",
                str(path),
            )
        ]
    if metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        return None, [
            ClaudeDiagnostic(
                "source_insecure_permissions",
                "Claude session is group/world writable",
                str(path),
            )
        ]
    if metadata.st_size > MAX_CLAUDE_SESSION_BYTES:
        return None, [
            ClaudeDiagnostic(
                "source_too_large",
                f"session exceeds {MAX_CLAUDE_SESSION_BYTES} bytes",
                str(path),
            )
        ]

    digest = hashlib.sha256()
    source_cwd: str | None = None
    message_records = 0
    first_timestamp: datetime | None = None
    last_timestamp: datetime | None = None
    invalid_records = 0

    try:
        with path.open("rb") as handle:
            while True:
                raw = handle.readline(MAX_CLAUDE_LINE_BYTES + 1)
                if not raw:
                    break
                if len(raw) > MAX_CLAUDE_LINE_BYTES and not raw.endswith(b"\n"):
                    return None, [
                        ClaudeDiagnostic(
                            "record_too_large",
                            f"one JSONL record exceeds {MAX_CLAUDE_LINE_BYTES} bytes",
                            str(path),
                        )
                    ]
                digest.update(raw)
                try:
                    record = json.loads(raw.decode("utf-8"))
                except (UnicodeError, json.JSONDecodeError):
                    invalid_records += 1
                    continue
                if not isinstance(record, dict):
                    invalid_records += 1
                    continue
                if source_cwd is None:
                    raw_cwd = record.get("cwd")
                    if isinstance(raw_cwd, str) and raw_cwd.strip():
                        source_cwd = raw_cwd.strip()
                if record.get("type") not in {"user", "assistant"}:
                    continue
                if record.get("isMeta") is True or record.get("isSidechain") is True:
                    continue
                if not _has_message_content(record):
                    continue
                message_records += 1
                timestamp = _parse_timestamp(record.get("timestamp"))
                if timestamp is None:
                    timestamp_ms = record.get("timestamp_ms")
                    if isinstance(timestamp_ms, int) and not isinstance(timestamp_ms, bool):
                        try:
                            timestamp = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
                        except (OverflowError, OSError, ValueError):
                            timestamp = None
                if timestamp is not None:
                    first_timestamp = timestamp if first_timestamp is None else min(first_timestamp, timestamp)
                    last_timestamp = timestamp if last_timestamp is None else max(last_timestamp, timestamp)
    except OSError as exc:
        return None, [ClaudeDiagnostic("source_read_error", str(exc), str(path))]

    if invalid_records:
        diagnostics.append(
            ClaudeDiagnostic(
                "invalid_records_skipped",
                f"ignored {invalid_records} malformed JSONL records",
                str(path),
            )
        )
    if source_cwd is None:
        return None, diagnostics + [
            ClaudeDiagnostic("missing_cwd", "no session cwd was found", str(path))
        ]
    if message_records == 0:
        return None, diagnostics + [
            ClaudeDiagnostic("missing_messages", "no importable user/assistant messages were found", str(path))
        ]
    if last_timestamp is None:
        return None, diagnostics + [
            ClaudeDiagnostic("missing_timestamp", "no valid message timestamp was found", str(path))
        ]

    # Codex's Claude parser treats the cwd embedded in the JSONL as
    # authoritative. The App Server's migration-item cwd cannot override it,
    # so accepting a remapped target here would produce a misleading plan.
    target_cwd = source_cwd
    target = Path(source_cwd)
    blocked_reason: str | None = None
    if not target.is_absolute():
        blocked_reason = "recorded cwd is not absolute"
    elif not target.exists() or not target.is_dir():
        blocked_reason = (
            "recorded cwd does not exist; Codex's Claude importer currently "
            "requires the embedded workspace path to exist"
        )
    else:
        try:
            target_cwd = str(target.resolve(strict=True))
        except OSError as exc:
            blocked_reason = f"cannot resolve target cwd: {exc}"
    if message_records > SAFE_MESSAGE_RECORD_LIMIT and not allow_large_sessions:
        blocked_reason = (
            f"session has {message_records} message records; the safe limit is "
            f"{SAFE_MESSAGE_RECORD_LIMIT} (use --allow-large-sessions to override)"
        )

    candidate = ClaudeSessionCandidate(
        source_path=path,
        source_cwd=source_cwd,
        target_cwd=target_cwd,
        size_bytes=metadata.st_size,
        modified_at_ns=metadata.st_mtime_ns,
        content_sha256=digest.hexdigest(),
        message_records=message_records,
        first_timestamp=first_timestamp,
        last_timestamp=last_timestamp,
        status="blocked" if blocked_reason else "pending",
        blocked_reason=blocked_reason,
    )
    return candidate, diagnostics


def _sqlite_readonly(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _load_import_references(
    codex_home: Path,
    source_root: Path,
) -> tuple[dict[Path, list[ImportedSessionReference]], set[str], bool, list[ClaudeDiagnostic]]:
    references: dict[Path, list[ImportedSessionReference]] = {}
    thread_rollouts: dict[str, Path] = {}
    known_threads: set[str] = set()
    diagnostics: list[ClaudeDiagnostic] = []
    database_available = False
    database = codex_home / "state_5.sqlite"

    if database.is_file():
        try:
            connection = _sqlite_readonly(database)
            try:
                tables = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                if "threads" in tables:
                    database_available = True
                    for thread_id, rollout_path in connection.execute(
                        "SELECT id, rollout_path FROM threads"
                    ):
                        if isinstance(thread_id, str):
                            known_threads.add(thread_id)
                            if isinstance(rollout_path, str) and rollout_path:
                                thread_rollouts[thread_id] = Path(rollout_path)
                if "external_agent_config_imports" in tables:
                    rows = connection.execute(
                        "SELECT completed_at_ms, successes, provider_id "
                        "FROM external_agent_config_imports"
                    )
                    for completed_at_ms, raw_successes, provider_id in rows:
                        if not isinstance(raw_successes, str):
                            continue
                        try:
                            successes = json.loads(raw_successes)
                        except json.JSONDecodeError:
                            diagnostics.append(
                                ClaudeDiagnostic(
                                    "invalid_legacy_import_history",
                                    "ignored malformed external-agent import history",
                                    str(database),
                                )
                            )
                            continue
                        if not isinstance(successes, list):
                            continue
                        for item in successes:
                            if not isinstance(item, dict) or item.get("item_type") != "SESSIONS":
                                continue
                            raw_source = item.get("source")
                            target_id = item.get("target")
                            if not isinstance(raw_source, str):
                                continue
                            source = Path(raw_source).expanduser().resolve(strict=False)
                            try:
                                source.relative_to(source_root)
                            except ValueError:
                                continue
                            if provider_id not in {None, "claude-code"}:
                                continue
                            reference = ImportedSessionReference(
                                source_path=source,
                                target_id=target_id if isinstance(target_id, str) else None,
                                content_sha256=None,
                                imported_at_ms=(
                                    completed_at_ms if isinstance(completed_at_ms, int) else None
                                ),
                                target_rollout_path=(
                                    thread_rollouts.get(target_id)
                                    if isinstance(target_id, str)
                                    else None
                                ),
                            )
                            references.setdefault(source, []).append(reference)
            finally:
                connection.close()
        except (OSError, sqlite3.Error) as exc:
            diagnostics.append(
                ClaudeDiagnostic("import_history_database_error", str(exc), str(database))
            )

    ledger = codex_home / "external_agent_session_imports.json"
    if ledger.is_file():
        try:
            if ledger.stat().st_size > MAX_IMPORT_LEDGER_BYTES:
                raise ClaudeImportError(
                    f"external-agent import ledger exceeds {MAX_IMPORT_LEDGER_BYTES} bytes"
                )
            payload = json.loads(ledger.read_text(encoding="utf-8"))
            records = payload.get("records") if isinstance(payload, dict) else None
            if not isinstance(records, list):
                raise ClaudeImportError("external-agent import ledger has an invalid shape")
            for item in records:
                if not isinstance(item, dict):
                    continue
                raw_source = item.get("source_path")
                content_sha256 = item.get("content_sha256")
                target_id = item.get("imported_thread_id")
                imported_at = item.get("imported_at")
                if not isinstance(raw_source, str) or not isinstance(content_sha256, str):
                    continue
                source = Path(raw_source).expanduser().resolve(strict=False)
                try:
                    source.relative_to(source_root)
                except ValueError:
                    continue
                reference = ImportedSessionReference(
                    source_path=source,
                    target_id=target_id if isinstance(target_id, str) else None,
                    content_sha256=content_sha256,
                    imported_at_ms=(imported_at * 1000 if isinstance(imported_at, int) else None),
                    target_rollout_path=(
                        thread_rollouts.get(target_id) if isinstance(target_id, str) else None
                    ),
                )
                references.setdefault(source, []).append(reference)
        except (OSError, UnicodeError, json.JSONDecodeError, ClaudeImportError) as exc:
            diagnostics.append(ClaudeDiagnostic("invalid_import_ledger", str(exc), str(ledger)))

    return references, known_threads, database_available, diagnostics


def _apply_import_history(
    candidate: ClaudeSessionCandidate,
    references: dict[Path, list[ImportedSessionReference]],
    known_threads: set[str],
    database_available: bool,
) -> tuple[ClaudeSessionCandidate, ClaudeDiagnostic | None]:
    matches = references.get(candidate.source_path, [])
    stale_exact: ImportedSessionReference | None = None
    changed_target: ImportedSessionReference | None = None
    modified_at_ms = candidate.modified_at_ns // 1_000_000

    for reference in reversed(matches):
        target_known = (
            not database_available
            or reference.target_id is None
            or reference.target_id in known_threads
        )
        exact_hash = reference.content_sha256 == candidate.content_sha256
        legacy_unchanged = (
            reference.content_sha256 is None
            and reference.imported_at_ms is not None
            and modified_at_ms <= reference.imported_at_ms + 2_000
        )
        if exact_hash or legacy_unchanged:
            if target_known:
                return (
                    replace(
                        candidate,
                        status="imported",
                        imported_thread_id=reference.target_id,
                        target_rollout_path=reference.target_rollout_path,
                    ),
                    None,
                )
            stale_exact = reference
        elif reference.target_id is not None:
            changed_target = reference

    if stale_exact is not None:
        return (
            replace(
                candidate,
                status="blocked",
                blocked_reason="import registry points to a missing Codex thread",
                imported_thread_id=stale_exact.target_id,
                target_rollout_path=stale_exact.target_rollout_path,
            ),
            ClaudeDiagnostic(
                "imported_thread_missing",
                "matching import ledger entry points to a thread absent from the Codex catalog",
                str(candidate.source_path),
            ),
        )
    if changed_target is not None:
        return (
            replace(
                candidate,
                imported_thread_id=changed_target.target_id,
                target_rollout_path=changed_target.target_rollout_path,
            ),
            None,
        )
    return candidate, None


def scan_claude_history(
    projects_root: Path,
    codex_home: Path,
    *,
    selected_sources: Sequence[Path] = (),
    allow_large_sessions: bool = False,
) -> ClaudeImportPlan:
    raw_root = projects_root.expanduser().absolute()
    try:
        root = raw_root.resolve(strict=True)
    except OSError as exc:
        raise ClaudeImportError(f"cannot read Claude projects directory {raw_root}: {exc}") from exc
    if not root.is_dir():
        raise ClaudeImportError(f"Claude projects path is not a directory: {root}")

    selected: set[Path] = set()
    for source in selected_sources:
        raw_source = source.expanduser()
        if not raw_source.is_absolute():
            raise ClaudeImportError(f"--source must be absolute: {source}")
        selected.add(_canonical_child(raw_source, root))

    discovered: list[Path] = []
    diagnostics: list[ClaudeDiagnostic] = []
    try:
        project_directories = sorted(root.iterdir())
    except OSError as exc:
        raise ClaudeImportError(f"cannot list Claude projects directory: {exc}") from exc
    for project_directory in project_directories:
        if project_directory.is_symlink() or not project_directory.is_dir():
            continue
        try:
            children = sorted(project_directory.iterdir())
        except OSError as exc:
            diagnostics.append(
                ClaudeDiagnostic("project_read_error", str(exc), str(project_directory))
            )
            continue
        for path in children:
            if path.suffix != ".jsonl":
                continue
            if path.is_symlink():
                diagnostics.append(
                    ClaudeDiagnostic(
                        "source_symlink_rejected",
                        "session symlinks are not imported",
                        str(path),
                    )
                )
                continue
            canonical = _canonical_child(path, root)
            if canonical.parent.parent != root:
                continue
            if selected and canonical not in selected:
                continue
            discovered.append(canonical)

    missing_selected = selected.difference(discovered)
    if missing_selected:
        missing = ", ".join(str(path) for path in sorted(missing_selected))
        raise ClaudeImportError(f"selected Claude session was not found: {missing}")

    references, known_threads, database_available, history_diagnostics = _load_import_references(
        codex_home.expanduser().absolute(), root
    )
    diagnostics.extend(history_diagnostics)
    candidates: list[ClaudeSessionCandidate] = []
    for path in discovered:
        candidate, file_diagnostics = _scan_claude_file(
            path,
            allow_large_sessions=allow_large_sessions,
        )
        diagnostics.extend(file_diagnostics)
        if candidate is None:
            continue
        candidate, history_diagnostic = _apply_import_history(
            candidate,
            references,
            known_threads,
            database_available,
        )
        if history_diagnostic is not None:
            diagnostics.append(history_diagnostic)
        candidates.append(candidate)

    candidates.sort(
        key=lambda item: (
            item.last_timestamp or datetime.min.replace(tzinfo=timezone.utc),
            str(item.source_path),
        )
    )
    return ClaudeImportPlan(root, tuple(candidates), tuple(diagnostics))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise ClaudeImportError(f"cannot safely hash {path}: {exc}") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ClaudeImportError(f"cannot hash a non-regular file: {path}")
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def source_is_unchanged(candidate: ClaudeSessionCandidate) -> bool:
    try:
        metadata = candidate.source_path.stat(follow_symlinks=False)
        return (
            stat.S_ISREG(metadata.st_mode)
            and metadata.st_size == candidate.size_bytes
            and metadata.st_mtime_ns == candidate.modified_at_ns
            and _sha256_file(candidate.source_path) == candidate.content_sha256
        )
    except (OSError, ClaudeImportError):
        return False


def imported_target_for(
    candidate: ClaudeSessionCandidate,
    codex_home: Path,
    source_root: Path,
) -> str | None:
    references, known_threads, database_available, _ = _load_import_references(
        codex_home, source_root
    )
    updated, _ = _apply_import_history(
        candidate,
        references,
        known_threads,
        database_available,
    )
    return updated.imported_thread_id if updated.status == "imported" else None


def migration_item_for(candidate: ClaudeSessionCandidate) -> dict[str, Any]:
    if not candidate.eligible:
        raise ClaudeImportError("only pending Claude sessions can be imported")
    return {
        "itemType": "SESSIONS",
        "description": f"Transfer Claude Code session {candidate.source_path.name}",
        "cwd": None,
        "details": {
            "plugins": [],
            "sessions": [
                {
                    "path": str(candidate.source_path),
                    "cwd": candidate.target_cwd,
                    "title": None,
                }
            ],
            "mcpServers": [],
            "hooks": [],
            "subagents": [],
            "commands": [],
        },
    }


def _offline_subprocess_environment(codex_home: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment["CODEX_HOME"] = str(codex_home)
    # Session conversion is local. Force incidental app-server/plugin network
    # warmups onto a closed loopback port instead of allowing external traffic.
    blocked_proxy = "http://127.0.0.1:9"
    environment["HTTP_PROXY"] = blocked_proxy
    environment["HTTPS_PROXY"] = blocked_proxy
    environment["ALL_PROXY"] = blocked_proxy
    environment["http_proxy"] = blocked_proxy
    environment["https_proxy"] = blocked_proxy
    environment["all_proxy"] = blocked_proxy
    environment["NO_PROXY"] = "127.0.0.1,localhost,::1"
    environment["no_proxy"] = environment["NO_PROXY"]
    return environment


class CodexAppServerClient:
    def __init__(
        self,
        *,
        codex_home: Path,
        codex_bin: str = "codex",
        timeout: float = DEFAULT_APP_SERVER_TIMEOUT,
    ) -> None:
        self.codex_home = codex_home.expanduser().absolute()
        self.codex_bin = codex_bin
        self.timeout = timeout
        self.process: subprocess.Popen[str] | None = None
        self._messages: queue.Queue[dict[str, Any] | object] = queue.Queue()
        self._notifications: list[dict[str, Any]] = []
        self._stderr: list[str] = []
        self._request_id = 0
        self._eof = object()
        self._reader_threads: list[threading.Thread] = []

    def __enter__(self) -> CodexAppServerClient:
        self.start()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def start(self) -> None:
        if self.process is not None:
            return
        executable = shutil.which(self.codex_bin) if os.sep not in self.codex_bin else self.codex_bin
        if not executable or not Path(executable).is_file():
            raise ClaudeImportError(f"Codex executable was not found: {self.codex_bin}")
        command = [
            executable,
            "--enable",
            "external_migration",
            "app-server",
            "--listen",
            "stdio://",
        ]
        try:
            self.process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                cwd=str(self.codex_home.parent),
                env=_offline_subprocess_environment(self.codex_home),
            )
        except OSError as exc:
            raise ClaudeImportError(f"failed to start Codex app-server: {exc}") from exc
        self._reader_threads = [
            threading.Thread(target=self._read_stdout, args=(self.process,), daemon=True),
            threading.Thread(target=self._read_stderr, args=(self.process,), daemon=True),
        ]
        for thread in self._reader_threads:
            thread.start()
        try:
            self._request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "codex-linux-repo-import",
                        "version": __version__,
                    },
                    "capabilities": {"experimentalApi": True},
                },
            )
            self._send({"method": "initialized"})
        except BaseException:
            self.close()
            raise

    def _read_stdout(self, process: subprocess.Popen[str]) -> None:
        assert process.stdout is not None
        try:
            for line in process.stdout:
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(message, dict):
                    self._messages.put(message)
        finally:
            self._messages.put(self._eof)

    def _read_stderr(self, process: subprocess.Popen[str]) -> None:
        assert process.stderr is not None
        for line in process.stderr:
            self._stderr.append(line.rstrip())
            if len(self._stderr) > 40:
                del self._stderr[: len(self._stderr) - 40]

    def _send(self, message: dict[str, Any]) -> None:
        if self.process is None or self.process.stdin is None:
            raise ClaudeImportError("Codex app-server is not running")
        try:
            self.process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
            self.process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise ClaudeImportError(self._server_failure("app-server input closed")) from exc

    def _server_failure(self, summary: str) -> str:
        detail = next((line for line in reversed(self._stderr) if line), "")
        return f"{summary}: {detail}" if detail else summary

    def _next_message(self, deadline: float) -> dict[str, Any]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ClaudeImportError("timed out waiting for Codex app-server")
        try:
            message = self._messages.get(timeout=remaining)
        except queue.Empty as exc:
            raise ClaudeImportError("timed out waiting for Codex app-server") from exc
        if message is self._eof:
            raise ClaudeImportError(self._server_failure("Codex app-server exited unexpectedly"))
        assert isinstance(message, dict)
        return message

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._request_id += 1
        request_id = self._request_id
        self._send({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + self.timeout
        while True:
            message = self._next_message(deadline)
            if message.get("id") == request_id and "method" not in message:
                error = message.get("error")
                if isinstance(error, dict):
                    detail = error.get("message")
                    raise ClaudeImportError(
                        f"Codex app-server {method} failed: {detail or error}"
                    )
                result = message.get("result")
                return result if isinstance(result, dict) else {}
            if isinstance(message.get("method"), str) and "id" not in message:
                self._notifications.append(message)
                continue
            if isinstance(message.get("method"), str) and "id" in message:
                self._send(
                    {
                        "id": message["id"],
                        "error": {
                            "code": -32601,
                            "message": "unsupported server request during offline Claude import",
                        },
                    }
                )

    def _wait_for_completion(self, import_id: str | None) -> dict[str, Any]:
        deadline = time.monotonic() + self.timeout
        while True:
            for index, message in enumerate(self._notifications):
                if message.get("method") != "externalAgentConfig/import/completed":
                    continue
                params = message.get("params")
                if import_id is not None and isinstance(params, dict):
                    received_id = params.get("importId")
                    if received_id not in {None, import_id}:
                        continue
                return self._notifications.pop(index)
            message = self._next_message(deadline)
            if message.get("method") == "externalAgentConfig/import/completed":
                params = message.get("params")
                if import_id is None or not isinstance(params, dict) or params.get("importId") in {
                    None,
                    import_id,
                }:
                    return message
            if isinstance(message.get("method"), str) and "id" not in message:
                self._notifications.append(message)

    def import_session(self, candidate: ClaudeSessionCandidate) -> dict[str, Any]:
        result = self._request(
            "externalAgentConfig/import",
            {"migrationItems": [migration_item_for(candidate)]},
        )
        raw_import_id = result.get("importId")
        import_id = raw_import_id if isinstance(raw_import_id, str) else None
        completion = self._wait_for_completion(import_id)
        params = completion.get("params")
        return params if isinstance(params, dict) else {}

    def close(self) -> None:
        process = self.process
        if process is None:
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.terminate()
            except OSError:
                pass
            try:
                process.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                except OSError:
                    pass
                try:
                    process.wait(timeout=3)
                except (OSError, subprocess.TimeoutExpired):
                    pass
        finally:
            for thread in self._reader_threads:
                thread.join(timeout=2)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None and not stream.closed:
                    try:
                        stream.close()
                    except OSError:
                        pass
            self._reader_threads.clear()
            self.process = None


def completion_failures(payload: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    results = payload.get("itemTypeResults")
    if not isinstance(results, list):
        return failures
    for result in results:
        if not isinstance(result, dict) or result.get("itemType") != "SESSIONS":
            continue
        raw_failures = result.get("failures")
        if not isinstance(raw_failures, list):
            continue
        for failure in raw_failures:
            if not isinstance(failure, dict):
                continue
            message = failure.get("message")
            failures.append(message if isinstance(message, str) else "unknown import failure")
    return failures


def _write_private(path: Path, payload: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            path.unlink()
        except OSError:
            pass
        raise


def _copy_private(source: Path, destination: Path) -> str:
    try:
        source_descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise ClaudeImportError(f"cannot safely open backup source {source}: {exc}") from exc
    destination_descriptor = -1
    digest = hashlib.sha256()
    try:
        metadata = os.fstat(source_descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ClaudeImportError(f"backup source is not a regular file: {source}")
        destination_descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        try:
            while True:
                chunk = os.read(source_descriptor, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                offset = 0
                while offset < len(chunk):
                    offset += os.write(destination_descriptor, chunk[offset:])
            os.fsync(destination_descriptor)
        except BaseException:
            try:
                destination.unlink()
            except OSError:
                pass
            raise
    finally:
        os.close(source_descriptor)
        if destination_descriptor >= 0:
            os.close(destination_descriptor)
    return digest.hexdigest()


def _backup_sqlite(source: Path, destination: Path) -> str:
    if source.is_symlink() or not source.is_file():
        raise ClaudeImportError(f"Codex state database is not a regular non-symlink file: {source}")
    _write_private(destination, b"")
    source_connection: sqlite3.Connection | None = None
    destination_connection: sqlite3.Connection | None = None
    try:
        try:
            source_connection = _sqlite_readonly(source)
            destination_connection = sqlite3.connect(destination)
            source_connection.backup(destination_connection)
            destination_connection.commit()
        finally:
            if destination_connection is not None:
                destination_connection.close()
            if source_connection is not None:
                source_connection.close()
    except BaseException:
        try:
            destination.unlink()
        except OSError:
            pass
        raise
    return _sha256_file(destination)


@contextmanager
def claude_import_lock(backup_root: Path) -> Iterator[None]:
    backup_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if backup_root.is_symlink():
        raise ClaudeImportError(f"backup root must not be a symlink: {backup_root}")
    metadata = backup_root.stat()
    if metadata.st_uid != os.geteuid() or metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ClaudeImportError(
            f"backup root must be private and owned by the current user (mode 0700): {backup_root}"
        )
    lock_path = backup_root / ".claude-import.lock"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        lock_metadata = os.fstat(descriptor)
        if not stat.S_ISREG(lock_metadata.st_mode) or lock_metadata.st_uid != os.geteuid():
            raise ClaudeImportError(f"unsafe Claude import lock file: {lock_path}")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ClaudeImportError("another Claude import is already running") from exc
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def create_claude_import_backup(
    codex_home: Path,
    backup_root: Path,
    candidates: Sequence[ClaudeSessionCandidate],
) -> ClaudeImportBackup:
    now = datetime.now(timezone.utc)
    backup_id = f"claude-{now.strftime('%Y%m%dT%H%M%S%fZ')}-{os.getpid()}"
    backup = backup_root / backup_id
    backup.mkdir(mode=0o700, parents=False, exist_ok=False)
    copied: list[dict[str, Any]] = []

    database = codex_home / "state_5.sqlite"
    if database.is_file():
        destination = backup / "state_5.sqlite"
        copied.append(
            {
                "source": str(database),
                "backup": destination.name,
                "sha256": _backup_sqlite(database, destination),
                "kind": "sqlite-online-backup",
            }
        )

    for name in (
        ".codex-global-state.json",
        ".codex-global-state.json.bak",
        "session_index.jsonl",
        "external_agent_session_imports.json",
    ):
        source = codex_home / name
        if not source.is_file():
            continue
        destination = backup / name
        copied.append(
            {
                "source": str(source),
                "backup": destination.name,
                "sha256": _copy_private(source, destination),
                "kind": "file-copy",
            }
        )

    rollout_backup = backup / "existing_rollouts"
    copied_rollouts: set[Path] = set()
    codex_home_resolved = codex_home.resolve(strict=True)
    for candidate in candidates:
        source = candidate.target_rollout_path
        if source is None or not source.is_file():
            continue
        if source.is_symlink():
            raise ClaudeImportError(f"existing imported rollout must not be a symlink: {source}")
        try:
            canonical_source = source.resolve(strict=True)
            canonical_source.relative_to(codex_home_resolved)
        except (OSError, ValueError) as exc:
            raise ClaudeImportError(
                f"existing imported rollout is outside the Codex data directory: {source}"
            ) from exc
        if canonical_source in copied_rollouts:
            continue
        copied_rollouts.add(canonical_source)
        rollout_backup.mkdir(mode=0o700, exist_ok=True)
        source_key = hashlib.sha256(str(canonical_source).encode("utf-8")).hexdigest()[:16]
        destination = rollout_backup / f"{source_key}-{canonical_source.name}"
        copied.append(
            {
                "source": str(canonical_source),
                "backup": str(destination.relative_to(backup)),
                "sha256": _copy_private(canonical_source, destination),
                "kind": "existing-imported-rollout",
            }
        )

    manifest: dict[str, Any] = {
        "schema_version": 1,
        "kind": "claude-session-import",
        "backup_id": backup_id,
        "created_at": now.isoformat().replace("+00:00", "Z"),
        "tool_version": __version__,
        "codex_home": str(codex_home),
        "source_files_are_read_only": True,
        "restore_mode": "manual-recovery-snapshot",
        "selected_sessions": [
            {
                "source_path": str(candidate.source_path),
                "content_sha256": candidate.content_sha256,
                "target_cwd": candidate.target_cwd,
                "existing_thread_id": candidate.imported_thread_id,
            }
            for candidate in candidates
        ],
        "copied_files": copied,
        "results": [],
    }
    _write_private(
        backup / "manifest.json",
        (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
    )
    return ClaudeImportBackup(backup, manifest)


def finalize_claude_import_backup(
    backup: ClaudeImportBackup,
    results: Sequence[ClaudeImportResult],
) -> None:
    manifest = dict(backup.manifest)
    manifest["completed_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    manifest["results"] = [item.to_public_dict() for item in results]
    path = backup.path / "manifest.json"
    temporary = backup.path / ".manifest.json.tmp"
    if temporary.exists():
        raise ClaudeImportError(f"unexpected backup temporary file: {temporary}")
    _write_private(
        temporary,
        (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
    )
    os.replace(temporary, path)

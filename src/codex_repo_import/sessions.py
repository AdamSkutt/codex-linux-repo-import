from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Iterable

from .models import Diagnostic, SessionRecord


MAX_SESSION_META_BYTES = 4 * 1024 * 1024
VSCODE_EXTENSION = "vscode-extension"
CODEX_DESKTOP = "codex-desktop"
PROVENANCE_LABELS = {
    VSCODE_EXTENSION: "VS Code extension",
    CODEX_DESKTOP: "Codex Desktop",
}


def classify_session_provenance(originator: object, source: object) -> str | None:
    """Classify supported parent sessions without guessing an external provider.

    Codex Desktop parent conversations use the same exact pair whether they
    were created in Desktop or rewritten from a supported external agent. The
    first metadata record does not retain a reliable provider name, so keep the
    provenance generic and exact.
    """

    if originator == "codex_vscode" and source == "vscode":
        return VSCODE_EXTENSION
    if originator == "Codex Desktop" and source == "vscode":
        return CODEX_DESKTOP
    return None


def count_session_provenance(records: Iterable[SessionRecord]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        counts[record.provenance] = counts.get(record.provenance, 0) + 1
    return {key: counts[key] for key in sorted(counts)}


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("session timestamp is missing")
    normalized = value.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def read_session_meta(path: Path, *, archived: bool) -> tuple[SessionRecord | None, Diagnostic | None]:
    """Read only the first JSONL record; message bodies are never inspected."""

    try:
        with path.open("rb") as handle:
            first_line = handle.readline(MAX_SESSION_META_BYTES + 1)
    except OSError as exc:
        return None, Diagnostic("session_read_error", str(exc), str(path))

    if len(first_line) > MAX_SESSION_META_BYTES:
        return None, Diagnostic(
            "session_meta_too_large",
            f"first JSONL record exceeds {MAX_SESSION_META_BYTES} bytes",
            str(path),
        )

    try:
        envelope = json.loads(first_line.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeError) as exc:
        return None, Diagnostic("invalid_session_json", str(exc), str(path))

    if not isinstance(envelope, dict) or envelope.get("type") != "session_meta":
        return None, Diagnostic(
            "missing_session_meta",
            "first JSONL record is not session_meta",
            str(path),
        )

    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        return None, Diagnostic("invalid_session_meta", "payload is not an object", str(path))

    # Exact matching matters: subagent sessions use a source object and must not
    # inflate projects that happen to use more agents.
    provenance = classify_session_provenance(
        payload.get("originator"),
        payload.get("source"),
    )
    if provenance is None:
        return None, None

    thread_id = payload.get("id")
    cwd = payload.get("cwd")
    if not isinstance(thread_id, str) or not thread_id:
        return None, Diagnostic("missing_thread_id", "session id is missing", str(path))
    if not isinstance(cwd, str) or not cwd:
        return None, Diagnostic("missing_cwd", "session cwd is missing", str(path))

    try:
        started_at = _parse_timestamp(payload.get("timestamp"))
    except (TypeError, ValueError) as exc:
        return None, Diagnostic("invalid_timestamp", str(exc), str(path))

    cli_version = payload.get("cli_version")
    forked_from_id = payload.get("forked_from_id")
    return (
        SessionRecord(
            thread_id=thread_id,
            started_at=started_at,
            cwd=cwd,
            archived=archived,
            rollout_path=path,
            originator=str(payload.get("originator")),
            provenance=provenance,
            cli_version=cli_version if isinstance(cli_version, str) else None,
            forked_from_id=forked_from_id if isinstance(forked_from_id, str) else None,
        ),
        None,
    )


def _session_files(root: Path) -> Iterable[Path]:
    if not root.exists():
        return []
    return sorted(root.rglob("rollout-*.jsonl"))


def scan_sessions(
    sessions_dir: Path,
    archived_sessions_dir: Path,
) -> tuple[list[SessionRecord], list[Diagnostic]]:
    records: dict[str, SessionRecord] = {}
    diagnostics: list[Diagnostic] = []

    # Active wins if a duplicate id also exists in the archive.
    for root, archived in ((sessions_dir, False), (archived_sessions_dir, True)):
        for path in _session_files(root):
            record, diagnostic = read_session_meta(path, archived=archived)
            if diagnostic is not None:
                diagnostics.append(diagnostic)
            if record is None:
                continue
            previous = records.get(record.thread_id)
            if previous is None:
                records[record.thread_id] = record
                continue
            if previous.archived and not record.archived:
                records[record.thread_id] = record
            diagnostics.append(
                Diagnostic(
                    "duplicate_thread_id",
                    f"kept {'active' if not records[record.thread_id].archived else 'first'} record for {record.thread_id}",
                    str(path),
                )
            )

    return sorted(records.values(), key=lambda item: (item.started_at, item.thread_id)), diagnostics

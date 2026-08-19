from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Iterable

from codex_repo_import.models import ProjectGroup, ResolvedRoot, SessionRecord


FIXTURES = Path(__file__).parent / "fixtures"


def utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def session(
    thread_id: str,
    started_at: str,
    cwd: str,
    *,
    archived: bool = False,
) -> SessionRecord:
    return SessionRecord(
        thread_id=thread_id,
        started_at=utc(started_at),
        cwd=cwd,
        archived=archived,
        rollout_path=Path(f"/synthetic/{thread_id}.jsonl"),
        originator="codex_vscode",
        provenance="vscode-extension",
    )


def group(
    root: str,
    sessions: Iterable[SessionRecord],
    *,
    eligible: bool = True,
    kind: str = "workspace",
    reason: str | None = None,
) -> ProjectGroup:
    records = list(sessions)
    return ProjectGroup(
        root=ResolvedRoot(
            root=root,
            kind=kind,
            eligible_for_apply=eligible,
            reason=reason,
        ),
        sessions=records,
        cwd_aliases={item.cwd for item in records},
    )


def fixture_state() -> dict[str, Any]:
    return json.loads((FIXTURES / "native_state.json").read_text(encoding="utf-8"))


def cloned_state() -> dict[str, Any]:
    return deepcopy(fixture_state())

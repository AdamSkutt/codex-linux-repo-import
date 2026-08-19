from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Diagnostic:
    code: str
    message: str
    path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.path is not None:
            result["path"] = self.path
        return result


@dataclass(frozen=True)
class SessionRecord:
    thread_id: str
    started_at: datetime
    cwd: str
    archived: bool
    rollout_path: Path
    originator: str
    provenance: str
    cli_version: str | None = None
    forked_from_id: str | None = None

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "id": self.thread_id,
            "started_at": self.started_at.isoformat().replace("+00:00", "Z"),
            "cwd": self.cwd,
            "forked_from_id": self.forked_from_id,
            "archived": self.archived,
        }


@dataclass(frozen=True)
class ResolvedRoot:
    root: str
    kind: str
    eligible_for_apply: bool
    mapped: bool = False
    reason: str | None = None


@dataclass
class ProjectGroup:
    root: ResolvedRoot
    sessions: list[SessionRecord] = field(default_factory=list)
    cwd_aliases: set[str] = field(default_factory=set)
    metrics: dict[str, int | float] = field(default_factory=dict)
    score: float = 0.0

    @property
    def active_sessions(self) -> list[SessionRecord]:
        return [session for session in self.sessions if not session.archived]

    @property
    def archived_sessions(self) -> list[SessionRecord]:
        return [session for session in self.sessions if session.archived]

    @property
    def newest_started_at(self) -> datetime:
        candidates = self.active_sessions or self.sessions
        return max(session.started_at for session in candidates)

    def to_manifest_dict(self) -> dict[str, Any]:
        ordered = sorted(
            self.sessions,
            key=lambda session: (-session.started_at.timestamp(), session.thread_id),
        )
        return {
            "project_key": self.root.root,
            "root": self.root.root,
            "root_kind": self.root.kind,
            "cwd_aliases": sorted(self.cwd_aliases),
            "eligible_for_apply": self.root.eligible_for_apply,
            "ineligible_reason": self.root.reason,
            "metrics": self.metrics,
            "threads": [session.to_public_dict() for session in ordered],
        }

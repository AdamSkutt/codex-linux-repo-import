from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math
from zoneinfo import ZoneInfo

from .models import ProjectGroup, SessionRecord


DEFAULT_TIMEZONE = "Europe/Istanbul"


def _week_start(value):
    return value - timedelta(days=value.weekday())


def score_project(
    sessions: list[SessionRecord],
    *,
    as_of: datetime,
    timezone_name: str = DEFAULT_TIMEZONE,
) -> tuple[float, dict[str, int | float]]:
    active = [session for session in sessions if not session.archived]
    archived_count = len(sessions) - len(active)
    if not active:
        return 0.0, {
            "chat_count": 0,
            "archived_chat_count": archived_count,
            "active_days": 0,
            "span_days": 0.0,
            "age_days": 0.0,
            "active_weeks": 0,
            "frequency": 0.0,
            "recency": 0.0,
            "activity": 0.0,
            "span": 0.0,
            "continuity": 0.0,
            "score": 0.0,
        }

    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)
    as_of = as_of.astimezone(timezone.utc)
    tz = ZoneInfo(timezone_name)
    ordered = sorted(session.started_at.astimezone(timezone.utc) for session in active)
    first, last = ordered[0], ordered[-1]
    local_dates = [item.astimezone(tz).date() for item in ordered]
    active_days = len(set(local_dates))
    active_weeks = len({(day.isocalendar().year, day.isocalendar().week) for day in local_dates})
    span_days = max(0.0, (last - first).total_seconds() / 86400.0)
    age_days = max(0.0, (as_of - last).total_seconds() / 86400.0)

    first_week = _week_start(min(local_dates))
    last_week = _week_start(max(local_dates))
    week_span = max(1, ((last_week - first_week).days // 7) + 1)

    frequency = min(1.0, math.log1p(len(active)) / math.log(21.0))
    recency = 2.0 ** (-age_days / 30.0)
    activity = min(1.0, math.log1p(active_days) / math.log(31.0))
    span = min(1.0, span_days / 180.0)
    if len(active) < 2 or span_days == 0:
        continuity = 0.0
    else:
        coverage = active_weeks / week_span
        persistence = min(1.0, span_days / 90.0)
        continuity = math.sqrt(coverage * persistence)

    raw_score = 100.0 * (
        0.35 * frequency
        + 0.30 * recency
        + 0.15 * activity
        + 0.10 * span
        + 0.10 * continuity
    )
    metrics: dict[str, int | float] = {
        "chat_count": len(active),
        "archived_chat_count": archived_count,
        "active_days": active_days,
        "span_days": round(span_days, 6),
        "age_days": round(age_days, 6),
        "active_weeks": active_weeks,
        "frequency": round(frequency, 6),
        "recency": round(recency, 6),
        "activity": round(activity, 6),
        "span": round(span, 6),
        "continuity": round(continuity, 6),
        "score": round(raw_score, 6),
    }
    return raw_score, metrics


def rank_projects(
    groups: list[ProjectGroup],
    *,
    as_of: datetime,
    timezone_name: str = DEFAULT_TIMEZONE,
) -> list[ProjectGroup]:
    for group in groups:
        group.score, group.metrics = score_project(
            group.sessions,
            as_of=as_of,
            timezone_name=timezone_name,
        )
    return sorted(
        groups,
        key=lambda group: (
            -group.score,
            -group.newest_started_at.timestamp(),
            -len(group.active_sessions),
            group.root.root.encode("utf-8"),
        ),
    )

"""Session marks from the orchestrator's session views (pure).

A mark is only the start of a session: its id, slug, preset, origin and creation time. Nothing
else of the view (spec, status, agents, messages) is read, so the stream's message text and
prompts can never reach the database or a log.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

SERVICE_ORIGINS = {
    "agents-webhook": "webhook",
    "agents-schedule": "schedule",
    "agents-matrix-connector": "chat",
}


@dataclass(frozen=True)
class Mark:
    session_id: uuid.UUID
    slug: str
    preset_id: uuid.UUID | None
    preset_name: str | None
    origin: str
    started_at: datetime


@dataclass(frozen=True)
class SnapshotPlan:
    to_insert: list[Mark] = field(default_factory=list)
    to_close: list[uuid.UUID] = field(default_factory=list)


def origin_of(created_by_service: str | None, created_by_session: str | None) -> str:
    """Who created the session: an agent (a creating session wins), a trigger service, the
    UI (no provenance) or another service."""
    if created_by_session:
        return "agent"
    if not created_by_service:
        return "ui"
    return SERVICE_ORIGINS.get(created_by_service, "other")


def _uuid(value: Any) -> uuid.UUID | None:
    if not isinstance(value, str):
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _instant(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def mark_from_session(view: Any) -> Mark | None:
    """The mark of one session view, or None when its id, slug or creation time is missing or
    malformed. A malformed preset id or name is dropped, not fatal."""
    if not isinstance(view, dict):
        return None
    session_id = _uuid(view.get("id"))
    slug = view.get("slug")
    started_at = _instant(view.get("created_at"))
    if session_id is None or not isinstance(slug, str) or not slug or started_at is None:
        return None
    service = view.get("created_by_service")
    session = view.get("created_by_session")
    preset_name = view.get("preset_name")
    return Mark(
        session_id=session_id,
        slug=slug,
        preset_id=_uuid(view.get("preset_id")),
        preset_name=preset_name if isinstance(preset_name, str) else None,
        origin=origin_of(
            service if isinstance(service, str) else None,
            session if isinstance(session, str) else None,
        ),
        started_at=started_at,
    )


def live_ids(views: list[Any]) -> set[uuid.UUID]:
    """The ids of every listed session view that has a valid id, even when the rest of the
    view is unparsable: such a session is still live and must keep its open mark."""
    return {found for view in views if isinstance(view, dict) and (found := _uuid(view.get("id")))}


def plan_snapshot(
    open_ids: set[uuid.UUID],
    known_ids: set[uuid.UUID],
    snapshot: list[Mark],
    live: set[uuid.UUID],
) -> SnapshotPlan:
    """What a full list of live sessions changes: insert the parsed marks of sessions never
    seen, close the open marks of sessions not in `live` (every listed id, parsed or not). A
    known (open or closed) session is never inserted again, so a closed mark is never
    reopened."""
    seen = set(known_ids)
    to_insert: list[Mark] = []
    for mark in snapshot:
        if mark.session_id not in seen:
            seen.add(mark.session_id)
            to_insert.append(mark)
    return SnapshotPlan(to_insert, sorted(open_ids - live))

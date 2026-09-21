"""Local-only post-use skill outcome events.

The outcome ledger is deliberately separate from ``skills/.usage.json`` and the
shared Relay metrics package. It records a bounded, human-reviewable signal
without storing transcript, prompt, response, command, result, or credential
payloads. Session/task identifiers are one-way profile-local tags.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from contextlib import contextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from hermes_constants import get_hermes_home
from utils import atomic_write_text

# fcntl is Unix-only; keep the Windows fallback import lazy and fail-open.
msvcrt = None
try:
    import fcntl
except ImportError:  # pragma: no cover - platform-specific fallback
    fcntl = None
    with suppress(ImportError):
        import msvcrt

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "hermes.skill.outcome.v1"
OUTCOME_VALUES = frozenset({
    "accepted", "corrected", "blocked", "abandoned", "reverted", "ignored",
})
SOURCE_VALUES = frozenset({"agent", "cli", "user", "unknown"})
MAX_EVENTS = 10_000
MAX_SKILL_TAG_LENGTH = 128
_TAG_PREFIX = "sha256:"
_SAFE_SKILL_RE = re.compile(r"^[a-z0-9][a-z0-9._:/@+\\-]{0,127}$")
_TAG_RE = re.compile(r"^sha256:[0-9a-f]{16}$")
_REQUIRED_FIELDS = frozenset({
    "schema_version", "event_id", "recorded_at", "outcome", "skill_tag",
    "provenance", "session_tag", "task_tag", "source",
})


def outcomes_path() -> Path:
    """Return the active profile's private outcome ledger path."""
    return get_hermes_home() / "skills" / ".outcomes.jsonl"


def _lock_path() -> Path:
    return outcomes_path().with_suffix(".jsonl.lock")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _digest(value: str) -> str:
    return _TAG_PREFIX + hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:16]


def _bounded_tag(value: Any) -> str:
    """Hash an opaque session/task identifier; never persist the source value."""
    if value is None:
        return "unknown"
    raw = str(value).strip()
    return _digest(raw) if raw else "unknown"


def skill_tag(value: Any) -> str:
    """Keep a safe skill name or hash unusual/unbounded attribution."""
    if not isinstance(value, str):
        return "unknown"
    raw = value.strip().lower()
    if not raw:
        return "unknown"
    return raw if len(raw) <= MAX_SKILL_TAG_LENGTH and _SAFE_SKILL_RE.fullmatch(raw) else _digest(raw)


def _provenance(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in {"agent_created", "external", "installed", "local", "unknown"} else "unknown"


def _source(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in SOURCE_VALUES else "unknown"


def _flock(fd, lock: bool) -> None:
    if fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_EX if lock else fcntl.LOCK_UN)
        return
    if msvcrt is not None:  # type: ignore[attr-defined]
        fd.seek(0)
        msvcrt.locking(  # type: ignore[attr-defined]
            fd.fileno(),
            msvcrt.LK_LOCK if lock else msvcrt.LK_UNLCK,  # type: ignore[attr-defined]
            1,
        )


@contextmanager
def _ledger_lock() -> Iterator[None]:
    """Serialize append/prune cycles across Hermes processes."""
    path = _lock_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = open(path, "a+", encoding="utf-8")
    except OSError:
        # Telemetry is fail-open. A missing platform lock must not break a task.
        yield
        return
    with fd:
        _flock(fd, True)
        try:
            yield
        finally:
            with suppress(OSError, IOError):
                _flock(fd, False)


def _valid_event(event: Any) -> bool:
    if not isinstance(event, dict) or set(event) != _REQUIRED_FIELDS:
        return False
    try:
        uuid.UUID(str(event.get("event_id")))
    except (AttributeError, ValueError, TypeError):
        return False
    return bool(
        event.get("schema_version") == SCHEMA_VERSION
        and isinstance(event.get("event_id"), str)
        and isinstance(event.get("recorded_at"), str)
        and event.get("outcome") in OUTCOME_VALUES
        and isinstance(event.get("skill_tag"), str)
        and (event["skill_tag"] == "unknown" or _TAG_RE.fullmatch(event["skill_tag"]) or _SAFE_SKILL_RE.fullmatch(event["skill_tag"]))
        and event.get("provenance") in {"agent_created", "external", "installed", "local", "unknown"}
        and (event.get("session_tag") == "unknown" or _TAG_RE.fullmatch(str(event.get("session_tag"))))
        and (event.get("task_tag") == "unknown" or _TAG_RE.fullmatch(str(event.get("task_tag"))))
        and event.get("source") in SOURCE_VALUES
    )


def _read_events_unlocked() -> list[dict[str, Any]]:
    path = outcomes_path()
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        logger.debug("Unable to read skill outcome ledger: %s", exc)
        return []
    events: list[dict[str, Any]] = []
    for line in lines[-MAX_EVENTS:]:
        try:
            event = json.loads(line)
        except (TypeError, json.JSONDecodeError):
            continue
        if _valid_event(event):
            events.append(event)
    return events


def record_outcome(
    skill_name: str,
    outcome: str,
    *,
    session_id: Any = None,
    task_id: Any = None,
    provenance: Any = "unknown",
    source: Any = "agent",
) -> dict[str, Any]:
    """Append one bounded post-use outcome and return the persisted event.

    This function intentionally accepts no free-text note, transcript, prompt,
    response, command, result, or credential field. Invalid outcomes raise
    ``ValueError`` so a caller cannot silently create an unqueryable category.
    """
    normalized_outcome = str(outcome or "").strip().lower()
    if normalized_outcome not in OUTCOME_VALUES:
        raise ValueError(f"Unsupported skill outcome: {outcome!r}")
    event = {
        "schema_version": SCHEMA_VERSION,
        "event_id": str(uuid.uuid4()),
        "recorded_at": _now_iso(),
        "outcome": normalized_outcome,
        "skill_tag": skill_tag(skill_name),
        "provenance": _provenance(provenance),
        "session_tag": _bounded_tag(session_id),
        "task_tag": _bounded_tag(task_id),
        "source": _source(source),
    }
    with _ledger_lock():
        events = (_read_events_unlocked() + [event])[-MAX_EVENTS:]
        content = "".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in events)
        atomic_write_text(outcomes_path(), content, tmp_prefix=".outcomes_", create_mode=0o600, preserve_mode=True)
    return event


def read_outcomes(
    *, skill_name: str | None = None, outcome: str | None = None, limit: int = 100,
) -> list[dict[str, Any]]:
    """Read the newest bounded events for local reporting."""
    if outcome is not None:
        normalized_outcome = str(outcome).strip().lower()
        if normalized_outcome not in OUTCOME_VALUES:
            raise ValueError(f"Unsupported skill outcome: {outcome!r}")
    else:
        normalized_outcome = None
    try:
        bounded_limit = max(1, min(int(limit), MAX_EVENTS))
    except (TypeError, ValueError):
        bounded_limit = 100
    requested_skill = skill_tag(skill_name) if skill_name else None
    with _ledger_lock():
        events = _read_events_unlocked()
    selected = [
        event for event in events
        if (requested_skill is None or event["skill_tag"] == requested_skill)
        and (normalized_outcome is None or event["outcome"] == normalized_outcome)
    ]
    return selected[-bounded_limit:]

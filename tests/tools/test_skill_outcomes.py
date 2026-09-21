"""Tests for bounded local post-use skill outcome events."""

import json
import re
import uuid
from pathlib import Path

import pytest


@pytest.fixture
def outcomes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    (home / "skills").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    import importlib
    import tools.skill_outcomes as module
    importlib.reload(module)
    return home


def test_all_outcomes_round_trip_with_hashed_context(outcomes_home):
    from tools.skill_outcomes import OUTCOME_VALUES, read_outcomes, record_outcome

    for outcome in sorted(OUTCOME_VALUES):
        record_outcome(
            "private skill/token=not-a-name",
            outcome,
            session_id="session-secret",
            task_id="task-secret",
            provenance="agent_created",
        )

    events = read_outcomes(limit=20)
    assert [event["outcome"] for event in events] == sorted(OUTCOME_VALUES)
    for event in events:
        uuid.UUID(event["event_id"])
        assert event["schema_version"] == "hermes.skill.outcome.v1"
        assert re.fullmatch(r"sha256:[0-9a-f]{16}", event["skill_tag"])
        assert re.fullmatch(r"sha256:[0-9a-f]{16}", event["session_tag"])
        assert re.fullmatch(r"sha256:[0-9a-f]{16}", event["task_tag"])

    schema_path = (
        Path(__file__).resolve().parents[2]
        / "hermes_cli" / "observability" / "schemas" / "hermes.skill.outcome.v1.schema.json"
    )
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    for event in events:
        jsonschema.Draft202012Validator(schema).validate(event)

    raw = (outcomes_home / "skills" / ".outcomes.jsonl").read_text(encoding="utf-8")
    assert "session-secret" not in raw
    assert "task-secret" not in raw
    assert "not-a-name" not in raw


def test_safe_skill_name_is_reportable_and_filters(outcomes_home):
    from tools.skill_outcomes import read_outcomes, record_outcome

    record_outcome("ux-audit", "accepted", source="user")
    record_outcome("ux-audit", "corrected", source="agent")
    record_outcome("other-skill", "blocked")

    assert [row["outcome"] for row in read_outcomes(skill_name="ux-audit")] == [
        "accepted", "corrected"
    ]
    assert [row["skill_tag"] for row in read_outcomes(outcome="blocked")] == ["other-skill"]
    assert len(read_outcomes(limit=1)) == 1


def test_invalid_outcome_and_corrupt_lines_are_not_reported(outcomes_home):
    from tools.skill_outcomes import outcomes_path, read_outcomes, record_outcome

    with pytest.raises(ValueError):
        record_outcome("ux-audit", "completed")
    outcomes_path().write_text(
        "not-json\n" + json.dumps({"schema_version": "old"}) + "\n", encoding="utf-8"
    )
    assert read_outcomes() == []


def test_agent_wrapper_records_without_exposing_source_ids(outcomes_home):
    from tools import skill_usage
    from tools.skill_outcomes import read_outcomes

    assert skill_usage.record_outcome(
        "ux-audit", "accepted", session_id="session-secret", task_id="task-secret"
    ) is True
    [event] = read_outcomes()
    assert event["skill_tag"] == "ux-audit"
    assert event["session_tag"].startswith("sha256:")
    assert event["task_tag"].startswith("sha256:")


def test_agent_wrapper_is_fail_open(outcomes_home, monkeypatch):
    import tools.skill_outcomes as module
    from tools import skill_usage

    monkeypatch.setattr(module, "record_outcome", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk")))
    assert skill_usage.record_outcome("ux-audit", "accepted") is False

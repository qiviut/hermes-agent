"""CLI write/read coverage for local skill outcome events."""

import json
import sys


def test_skills_outcome_write_and_report(monkeypatch, tmp_path, capsys):
    home = tmp_path / ".hermes"
    (home / "skills").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))

    from hermes_cli.main import main

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "hermes", "skills", "outcome", "ux-audit", "accepted",
            "--session-id", "session-secret", "--task-id", "task-secret", "--json",
        ],
    )
    main()
    written = json.loads(capsys.readouterr().out)
    assert written["outcome"] == "accepted"
    assert written["skill_tag"] == "ux-audit"
    assert written["session_tag"].startswith("sha256:")
    assert written["task_tag"].startswith("sha256:")

    monkeypatch.setattr(
        sys,
        "argv",
        ["hermes", "skills", "outcomes", "--json", "--limit", "10"],
    )
    main()
    report_output = capsys.readouterr().out
    report = json.loads(report_output)
    assert len(report) == 1
    assert report[0]["skill_tag"] == "ux-audit"
    assert "session-secret" not in report_output

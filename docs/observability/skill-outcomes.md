# Local Skill Outcome Events

Hermes records post-use skill outcomes locally so load counts are not mistaken
for task success. This is an observational signal, not causal attribution: a
turn may load several skills, and an outcome should be recorded only when a
reviewer or agent has a real signal.

## Event contract

Events are JSON Lines under:

```text
$HERMES_HOME/skills/.outcomes.jsonl
```

Each event uses `hermes.skill.outcome.v1` and contains only:

- `outcome`: `accepted`, `corrected`, `blocked`, `abandoned`, `reverted`, or `ignored`;
- `skill_tag`: a safe bounded skill name, or a short SHA-256 tag for an unusual
  or unbounded name;
- `provenance`: `agent_created`, `external`, `installed`, `local`, or `unknown`;
- `session_tag` and `task_tag`: short SHA-256 tags, never the source identifiers;
- `source`: `agent`, `cli`, `user`, or `unknown`;
- a UUID event ID and UTC timestamp.

There is no note, transcript, prompt, response, command, result, hostname,
credential, or arbitrary payload field. The ledger is local-only and is not
included in shared Relay metrics packages or the opt-in remote sender. It is
bounded to the newest 10,000 valid events and written with mode `0600`.

## Write paths

Agent-facing code should use the fail-open wrapper:

```python
from tools.skill_usage import record_outcome

record_outcome(
    "ux-audit",
    "accepted",
    session_id=session_id,
    task_id=task_id,
)
```

The explicit CLI path is useful for a human or harness:

```bash
hermes skills outcome ux-audit accepted \
  --session-id SESSION_ID --task-id TASK_ID
```

Only the allowlisted outcome values are accepted.

## Read/report path

Use the local report command:

```bash
hermes skills outcomes --limit 100
hermes skills outcomes --skill ux-audit --outcome corrected --json
```

Report output contains the bounded event fields only. Empty, malformed, or
unknown-schema lines are ignored rather than promoted into a success signal.

## Interpretation

- `accepted`: the user or evaluator accepted the result without a material correction;
- `corrected`: the result needed a material user correction;
- `blocked`: a constraint, permission, or dependency prevented completion;
- `abandoned`: work stopped without a usable disposition;
- `reverted`: the resulting change was undone;
- `ignored`: the skill loaded but no trustworthy outcome signal was available.

Do not infer that a skill caused success from one event. Use session/task tags,
co-loaded skills, and representative cohorts before changing routing or
retiring a skill.

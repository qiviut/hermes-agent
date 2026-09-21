# Model Routing and Timeout Budgets

Use this reference when choosing a current GPT-5.6 route or when a short
foreground timeout is likely to kill healthy Hermes work. It records the
source-backed distinction between a control-call timeout, a terminal command
budget, a provider request budget, and an agent inactivity watchdog.

## Current GPT-5.6 Codex routes

The Codex OAuth fallback catalog in `hermes_cli/codex_models.py` currently
includes:

- `gpt-5.6-sol`
- `gpt-5.6-terra`
- `gpt-5.6-luna`

Hermes may also expose `-900k` picker variants for models whose metadata allows
the large-context route. The suffix is Hermes-side and is stripped before the
model id is sent to the backend; select the explicit variant when the larger
context window is intended. Do not infer that a model is available on every
provider: live provider discovery and the active OAuth/API route are
authoritative.

For an OpenAI Codex OAuth setup, inspect and set the route through the CLI:

```text
terminal(command="hermes config get model --json")
terminal(command="hermes config set model.provider openai-codex")
terminal(command="hermes config set model.default gpt-5.6-luna")
terminal(command="hermes config check")
```

Verify the selected route with `hermes status` or `hermes config get model
--json`; do not claim that a model works from a static catalog entry alone.
The current source uses `gpt-5.6-*` entries in
`hermes_cli/codex_models.py`; the official model configuration guide is
https://hermes-agent.nousresearch.com/docs/user-guide/configuring-models.

## Timeout layers

Do not use a 5–15 second timeout for an agent startup, model call, or progress
check. Those values are only reasonable for a local control command that has
no model work. Current source/docs give these materially larger defaults:

- `terminal.timeout: 180` — per-command terminal budget.
- `HERMES_API_TIMEOUT=1800` — provider request budget when no provider-specific
  request timeout is configured.
- `HERMES_API_CALL_STALE_TIMEOUT=90` — inactivity detector for a non-streaming
  call; it is not a total request deadline and is auto-disabled for local
  endpoints when implicit.
- `HERMES_STREAM_READ_TIMEOUT=120` — streaming socket read timeout; local
  providers may raise it to the API budget.
- `HERMES_AGENT_TIMEOUT=1800` — gateway inactivity timeout; tool calls and
  streamed tokens reset it, so it is not a maximum wall-clock duration.

Provider-specific `request_timeout_seconds`, `stale_timeout_seconds`, and
model-level overrides in `config.yaml` take precedence over the legacy
environment variables. See:

- `website/docs/user-guide/configuration.md` (provider and terminal timeout
  sections)
- `website/docs/reference/environment-variables.md`
- `hermes_cli/config_defaults.py` (`agent.gateway_timeout` and run budgets)

## Practical execution policy

- Use `timeout=30`–`60` only for tmux/process control calls such as creating a
  session or sending a keystroke.
- Give startup and progress probes `60`–`180` seconds when model/provider
  initialization may be involved.
- Give a substantial foreground `hermes chat -q` run about `900` seconds, or
  move it to `background=true` / tmux when it can legitimately run longer.
- Treat the outer tool timeout as a watchdog for the control call. Keep the
  agent process in tmux/background and inspect its pane, log, exit status, and
  artifacts separately.
- Prefer progress polling over one giant `sleep`; a slow but active stream is
  not the same as a wedged process.
- Do not raise a timeout to hide a deadlock or missing progress signal. If a
  command is silent past its inactivity budget, capture logs and diagnose the
  provider, subprocess, or approval boundary.

## Configuration boundary

Put non-secret timeout and routing settings in `config.yaml`; keep API keys,
OAuth tokens, and passwords in `.env`. After changing config, restart the CLI
or gateway as appropriate. Existing conversations keep their prompt/tool
cache, so do not pretend a mid-session config change rewires the active turn.

Official references:

- https://hermes-agent.nousresearch.com/docs/user-guide/configuration
- https://hermes-agent.nousresearch.com/docs/reference/environment-variables
- https://hermes-agent.nousresearch.com/docs/user-guide/configuring-models

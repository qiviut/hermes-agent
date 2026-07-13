"""Tests for the MCP elicitation handler in tools.mcp_tool.

These tests exercise ElicitationHandler in isolation -- the underlying
approval system and the MCP transport layer are mocked, so no real MCP
server or user input is required.

Tests skip cleanly if the optional `mcp` SDK is not installed (it is an
optional dependency under the `[mcp]` extra).
"""

import asyncio
import threading
from typing import Any, cast
from unittest.mock import patch

import pytest


pytest.importorskip("mcp.types")

from mcp.types import ElicitResult  # noqa: E402  -- after importorskip

from tools.mcp_tool import (  # noqa: E402
    ElicitationHandler,
    _format_elicitation_schema_summary,
)


def _form_params(message="please confirm", schema=None):
    """Build a stand-in for ElicitRequestFormParams.

    We use a plain object (not the SDK type directly) so the test doesn't
    couple to optional Pydantic validation -- the handler reads fields via
    getattr() and tolerates duck-typed inputs.
    """
    from types import SimpleNamespace
    return SimpleNamespace(
        mode="form",
        message=message,
        requested_schema=schema or {},
    )


def _url_params(message="open this url", url="https://example.com/auth", elicitation_id="e1"):
    from types import SimpleNamespace
    return SimpleNamespace(
        mode="url",
        message=message,
        url=url,
        elicitation_id=elicitation_id,
    )


class TestSchemaSummary:
    def test_empty_schema_falls_back_to_generic_message(self):
        out = _format_elicitation_schema_summary({}, "pay")
        assert "pay" in out
        assert "Approval requested" in out

    def test_properties_render_with_type_and_description(self):
        schema = {
            "type": "object",
            "properties": {
                "amount": {"type": "string", "description": "USD amount"},
                "recipient": {"type": "string"},
            },
        }
        out = _format_elicitation_schema_summary(schema, "pay")
        assert "amount (string): USD amount" in out
        assert "recipient (string)" in out


class TestElicitationHandlerFormMode:
    def test_user_accepts_once_returns_accept(self):
        handler = ElicitationHandler("pay", {"timeout": 5})
        params = _form_params(
            "authorize a payment of $0.50",
            {"properties": {"approved": {"type": "boolean"}}},
        )

        with patch("tools.approval.request_elicitation_consent", return_value="accept"):
            result = asyncio.run(handler(context=None, params=params))

        assert isinstance(result, ElicitResult)
        assert result.action == "accept"
        assert result.content == {}
        assert handler.metrics["accepted"] == 1
        assert handler.metrics["declined"] == 0

    def test_user_denies_returns_decline(self):
        handler = ElicitationHandler("pay", {"timeout": 5})
        params = _form_params()

        with patch("tools.approval.request_elicitation_consent", return_value="decline"):
            result = asyncio.run(handler(context=None, params=params))

        assert result.action == "decline"
        assert handler.metrics["declined"] == 1
        assert handler.metrics["accepted"] == 0

    def test_cancel_propagates_through(self):
        """An interrupted consent wait propagates as MCP cancel."""
        handler = ElicitationHandler("pay", {"timeout": 5})
        params = _form_params()

        with patch("tools.approval.request_elicitation_consent", return_value="cancel"):
            result = asyncio.run(handler(context=None, params=params))

        assert result.action == "cancel"
        assert handler.metrics["errors"] == 1


class TestElicitationHandlerFailureModes:
    def test_url_mode_is_declined_without_prompting(self):
        handler = ElicitationHandler("pay", {"timeout": 5})
        params = _url_params()

        # If the handler tried to prompt, this would raise AssertionError
        # because the side_effect treats the call as a test failure.
        with patch(
            "tools.approval.request_elicitation_consent",
            side_effect=AssertionError("URL mode must not prompt"),
        ):
            result = asyncio.run(handler(context=None, params=params))

        assert result.action == "decline"
        assert handler.metrics["declined"] == 1

    def test_exception_in_approval_fails_closed_to_decline(self):
        handler = ElicitationHandler("pay", {"timeout": 5})
        params = _form_params()

        with patch(
            "tools.approval.request_elicitation_consent",
            side_effect=RuntimeError("approval system blew up"),
        ):
            result = asyncio.run(handler(context=None, params=params))

        assert result.action == "decline"
        assert handler.metrics["errors"] == 1

    def test_legacy_timeout_config_does_not_cancel_consent(self):
        handler = ElicitationHandler("pay", {"timeout": 0.05})
        params = _form_params()

        def stall(*_args, **_kwargs):
            import time as _t
            _t.sleep(0.2)
            return "accept"

        with patch("tools.approval.request_elicitation_consent", side_effect=stall):
            result = asyncio.run(handler(context=None, params=params))

        assert result.action == "accept"
        assert handler.metrics["errors"] == 0

    def test_cancelled_mcp_call_releases_sync_consent_waiter(self):
        handler = ElicitationHandler("pay", {})
        entered = threading.Event()
        exited = threading.Event()

        def consent(*_args, cancel_event=None, **_kwargs):
            assert cancel_event is not None
            entered.set()
            assert cancel_event.wait(timeout=2)
            exited.set()
            return "cancel"

        async def scenario():
            with patch("tools.approval.request_elicitation_consent", side_effect=consent):
                task = asyncio.create_task(handler(context=None, params=_form_params()))
                assert await asyncio.to_thread(entered.wait, 1)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert await asyncio.to_thread(exited.wait, 2)

        asyncio.run(scenario())


class TestElicitationHandlerWiring:
    def test_session_kwargs_returns_callback(self):
        handler = ElicitationHandler("pay", {})
        kwargs = handler.session_kwargs()
        assert kwargs == {"elicitation_callback": handler}

    def test_no_decision_timeout_is_configured(self):
        handler = ElicitationHandler("pay", {})
        assert not hasattr(handler, "timeout")

    def test_owner_signals_pending_human_wait(self):
        from types import SimpleNamespace

        pending = threading.Event()
        owner = SimpleNamespace(
            _pending_call_context=None,
            _elicitation_pending=pending,
            _elicitation_cancel=threading.Event(),
        )
        handler = ElicitationHandler("pay", {}, owner=cast(Any, owner))
        observed = []

        def consent(*_args, **_kwargs):
            observed.append(pending.is_set())
            return "accept"

        with patch("tools.approval.request_elicitation_consent", side_effect=consent):
            result = asyncio.run(handler(context=None, params=_form_params()))

        assert result.action == "accept"
        assert observed == [True]
        assert not pending.is_set()

    def test_owner_cancel_signal_releases_receive_loop_elicitation(self):
        from types import SimpleNamespace

        pending = threading.Event()
        cancel = threading.Event()
        owner = SimpleNamespace(
            _pending_call_context=None,
            _elicitation_pending=pending,
            _elicitation_cancel=cancel,
            _active_elicitation_pending=pending,
            _active_elicitation_cancel=cancel,
        )
        handler = ElicitationHandler("pay", {}, owner=cast(Any, owner))
        entered = threading.Event()

        def consent(*_args, cancel_event=None, **_kwargs):
            assert cancel_event is not None
            assert cancel_event is cancel
            entered.set()
            assert cancel_event.wait(timeout=2)
            return "cancel"

        async def scenario():
            with patch("tools.approval.request_elicitation_consent", side_effect=consent):
                task = asyncio.create_task(handler(context=None, params=_form_params()))
                assert await asyncio.to_thread(entered.wait, 1)
                cancel.set()
                result = await task
                assert result.action == "cancel"
                assert not pending.is_set()
                assert not cancel.is_set()

        asyncio.run(scenario())

    def test_pre_signaled_active_cancel_is_not_cleared_by_handler_start(self):
        from types import SimpleNamespace

        pending = threading.Event()
        cancel = threading.Event()
        cancel.set()
        owner = SimpleNamespace(
            _pending_call_context=None,
            _elicitation_pending=threading.Event(),
            _elicitation_cancel=threading.Event(),
            _active_elicitation_pending=pending,
            _active_elicitation_cancel=cancel,
        )
        handler = ElicitationHandler("pay", {}, owner=cast(Any, owner))

        def consent(*_args, cancel_event=None, **_kwargs):
            assert cancel_event is not None
            assert cancel_event is cancel
            assert cancel_event.is_set()
            return "cancel"

        with patch("tools.approval.request_elicitation_consent", side_effect=consent):
            result = asyncio.run(handler(context=None, params=_form_params()))

        assert result.action == "cancel"
        assert not pending.is_set()
        assert not cancel.is_set()

    def test_disabled_config_does_not_construct_handler(self):
        """The server task initializer checks ``elicitation.enabled`` --
        an explicit ``False`` should suppress handler creation. The unit
        of that decision lives in MCPServerTask, but the handler itself
        must remain harmless to instantiate with arbitrary config."""
        handler = ElicitationHandler("pay", {"enabled": False, "timeout": 10})
        # Just confirm it instantiates; the gate lives at the higher layer.
        assert not hasattr(handler, "timeout")


class TestElicitationHandlerContextBridge:
    """The MCP recv-loop task that fires elicitation callbacks does NOT
    inherit the agent's contextvars (HERMES_SESSION_PLATFORM etc.). The
    handler reads ``owner._pending_call_context`` -- a snapshot captured
    by the MCP tool wrapper around ``session.call_tool`` -- and replays
    it before invoking the approval router so gateway-session detection
    survives the task hop. Regression tests for that bridge."""

    def test_captured_context_is_replayed_in_consent_call(self):
        """The captured context's contextvar values must be observable
        when ``request_elicitation_consent`` runs -- otherwise the
        gateway-platform detection in approval.py sees an empty platform
        string and falls back to the CLI path (the bug this fixes)."""
        import contextvars
        from types import SimpleNamespace

        probe: contextvars.ContextVar[str] = contextvars.ContextVar(
            "elicitation_test_probe", default=""
        )
        seen: list[str] = []

        def fake_consent(*_args, **_kwargs):
            seen.append(probe.get())
            return "accept"

        token = probe.set("gateway:telegram")
        try:
            captured = contextvars.copy_context()
        finally:
            probe.reset(token)
        assert probe.get() == "", (
            "Sanity check: the probe must be empty outside the captured "
            "context, otherwise the test would pass even without replay."
        )

        owner = SimpleNamespace(_pending_call_context=captured)
        handler = ElicitationHandler("pay", {"timeout": 5}, owner=owner)
        params = _form_params()

        with patch("tools.approval.request_elicitation_consent", side_effect=fake_consent):
            result = asyncio.run(handler(context=None, params=params))

        assert result.action == "accept"
        assert seen == ["gateway:telegram"], (
            f"Expected the captured contextvar to be visible inside the "
            f"consent call; got {seen!r}"
        )

    def test_missing_captured_context_falls_back_to_direct_call(self):
        """Without an owner (or with an owner that hasn't entered a tool
        call) the handler must still invoke the consent router -- just
        without the contextvar replay. Otherwise CLI/TUI sessions, which
        don't set HERMES_SESSION_PLATFORM, would break."""
        handler = ElicitationHandler("pay", {"timeout": 5}, owner=None)
        params = _form_params()

        with patch("tools.approval.request_elicitation_consent", return_value="accept") as m:
            result = asyncio.run(handler(context=None, params=params))

        assert result.action == "accept"
        assert m.call_count == 1

    def test_captured_context_can_be_replayed_multiple_times(self):
        """A single tool call may trigger more than one elicitation
        (e.g. the agent retries an MCP call within the same wrapper).
        ``Context.run`` raises if a context is re-entered, so the handler
        must ``.copy()`` before each run."""
        import contextvars
        from types import SimpleNamespace

        probe: contextvars.ContextVar[str] = contextvars.ContextVar(
            "elicitation_test_probe_multi", default=""
        )
        seen: list[str] = []

        def fake_consent(*_args, **_kwargs):
            seen.append(probe.get())
            return "accept"

        token = probe.set("gateway:slack")
        try:
            captured = contextvars.copy_context()
        finally:
            probe.reset(token)

        owner = SimpleNamespace(_pending_call_context=captured)
        handler = ElicitationHandler("pay", {"timeout": 5}, owner=owner)
        params = _form_params()

        with patch("tools.approval.request_elicitation_consent", side_effect=fake_consent):
            for _ in range(3):
                asyncio.run(handler(context=None, params=params))

        assert seen == ["gateway:slack"] * 3

    def test_pending_call_context_none_does_not_crash(self):
        """``owner._pending_call_context`` is set to None between tool
        calls. An elicitation arriving in that window must not crash."""
        from types import SimpleNamespace

        owner = SimpleNamespace(_pending_call_context=None)
        handler = ElicitationHandler("pay", {"timeout": 5}, owner=owner)
        params = _form_params()

        with patch("tools.approval.request_elicitation_consent", return_value="decline"):
            result = asyncio.run(handler(context=None, params=params))

        assert result.action == "decline"

"""Regression: a blocking gateway approval wait must honor an interrupt (#8697).

When an agent calls a dangerous command, the gateway approval flow blocks the
agent's execution thread inside ``_await_gateway_decision`` on
``threading.Event.wait()`` until the user responds or interrupts the session.
Before the fix, ``/stop`` (which calls
``AIAgent.interrupt()`` → per-thread interrupt flag) was silently ignored by
that wait loop, so the session stayed wedged indefinitely.

The fix checks ``is_interrupted()`` at the top of the poll loop.  Because the
wait runs on the agent's execution thread — the exact thread
``AIAgent.interrupt()`` flags — the check sees the signal and returns a distinct
fail-closed ``interrupted`` outcome so the agent loop unwinds without recording
a user denial.
"""

import os
import threading
import time


def _clear_approval_state():
    """Reset all module-level approval state between tests."""
    from tools import approval as mod
    mod._gateway_queues.clear()
    mod._gateway_notify_cbs.clear()
    mod._session_approved.clear()
    mod._permanent_approved.clear()
    mod._pending.clear()


class TestApprovalInterrupt:
    SESSION_KEY = "interrupt-test-session"

    def setup_method(self):
        from tools.interrupt import set_interrupt
        from tools import approval as _approval_mod
        from tools import interrupt as _interrupt_mod

        _clear_approval_state()
        # Wipe ALL per-thread interrupt bits — thread idents are recycled by
        # the OS, so a bit set on a now-dead thread in a prior test can leak
        # onto a fresh worker that happens to reuse the ident.
        with _interrupt_mod._lock:
            _interrupt_mod._interrupted_threads.clear()
        set_interrupt(False)
        self._saved_env = {
            k: os.environ.get(k)
            for k in ("HERMES_GATEWAY_SESSION", "HERMES_YOLO_MODE",
                      "HERMES_SESSION_KEY")
        }
        os.environ.pop("HERMES_YOLO_MODE", None)
        os.environ["HERMES_GATEWAY_SESSION"] = "1"
        os.environ["HERMES_SESSION_KEY"] = self.SESSION_KEY
        self._session_token = _approval_mod.set_current_session_key(self.SESSION_KEY)

    def teardown_method(self):
        from tools.interrupt import set_interrupt
        from tools import approval as _approval_mod
        from tools import interrupt as _interrupt_mod

        with _interrupt_mod._lock:
            _interrupt_mod._interrupted_threads.clear()
        set_interrupt(False)
        _approval_mod.reset_current_session_key(self._session_token)
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        _clear_approval_state()

    def test_interrupt_unblocks_pending_approval_quickly(self):
        """An interrupt on the waiting thread remains distinct from denial."""
        from tools import approval as mod
        from tools.interrupt import set_interrupt



        approval_data = {
            "command": "rm -rf /tmp/whatever",
            "description": "recursive delete",
            "pattern_key": "rm_rf",
            "pattern_keys": ["rm_rf"],
        }

        result_holder = {}
        notified = threading.Event()

        def _notify_cb(_data):
            # Mimic the gateway: a callback is registered and invoked once the
            # approval is enqueued.  We just record that the user *would* have
            # been prompted.
            notified.set()

        def _worker():
            result_holder["result"] = mod._await_gateway_decision(
                self.SESSION_KEY, _notify_cb, approval_data
            )
            result_holder["thread_id"] = threading.get_ident()

        t = threading.Thread(target=_worker, daemon=True)
        start = time.monotonic()
        t.start()

        # Wait until the worker has enqueued + notified, proving it is actually
        # blocked inside the poll loop.
        assert notified.wait(timeout=5), "approval was never enqueued/notified"

        # Simulate /stop: AIAgent.interrupt() flags the agent's execution
        # thread.  Here the worker thread *is* that execution thread.
        set_interrupt(True, t.ident)

        t.join(timeout=10)
        elapsed = time.monotonic() - start

        assert not t.is_alive(), "approval wait did not return after interrupt"
        assert result_holder["result"] == {
            "resolved": False,
            "choice": None,
            "reason": None,
            "interrupted": True,
        }
        # The interrupt, not a deadline, is what released the wait.
        assert elapsed < 10, f"interrupt path too slow ({elapsed:.1f}s)"
        # Queue entry was cleaned up.
        assert not mod.has_blocking_approval(self.SESSION_KEY)

    def test_clear_session_unblocks_as_interruption_not_denial(self):
        """Session cleanup cannot fabricate either denial or approval."""
        from tools import approval as mod

        result_holder = {}
        notified = threading.Event()

        def _worker():
            result_holder["result"] = mod._await_gateway_decision(
                self.SESSION_KEY,
                lambda _data: notified.set(),
                {
                    "command": "rm -rf /tmp/whatever",
                    "description": "recursive delete",
                    "pattern_key": "rm_rf",
                    "pattern_keys": ["rm_rf"],
                },
            )

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        assert notified.wait(timeout=5), "approval was never enqueued/notified"
        mod.clear_session(self.SESSION_KEY)
        t.join(timeout=5)

        assert not t.is_alive(), "session cleanup did not release approval wait"
        assert result_holder["result"] == {
            "resolved": False,
            "choice": None,
            "reason": None,
            "interrupted": True,
        }

    def test_safer_alternative_blocks_mechanism_without_grant(self, monkeypatch):
        """Typed safer-alternative intent never falls through as approval."""
        from tools import approval as mod

        monkeypatch.setattr(
            mod,
            "_await_gateway_decision",
            lambda *_args, **_kwargs: {
                "resolved": True,
                "choice": "safer_alternative",
                "reason": None,
            },
        )
        monkeypatch.setattr(
            "tools.tirith_security.check_command_security",
            lambda _command: {"action": "allow", "findings": [], "summary": ""},
        )
        monkeypatch.setattr(mod, "_get_approval_mode", lambda: "manual")
        monkeypatch.setattr(mod, "_YOLO_MODE_FROZEN", False)
        mod.register_gateway_notify(self.SESSION_KEY, lambda _data: None)

        command_result = mod.check_all_command_guards("chmod 777 /tmp/example", "local")
        code_result = mod.check_execute_code_guard("print('example')", "local")

        for result in (command_result, code_result):
            assert result["approved"] is False
            assert result["outcome"] == "safer_alternative"
            assert result["user_consent"] is False
            assert "do not broaden authority" in result["message"]

    def test_unrelated_thread_interrupt_does_not_unblock(self):
        """An interrupt flagged on a *different* thread must NOT release this
        session's approval wait — interrupts are thread-scoped."""
        from tools import approval as mod
        from tools.interrupt import set_interrupt



        approval_data = {
            "command": "rm -rf /tmp/whatever",
            "description": "recursive delete",
            "pattern_key": "rm_rf",
            "pattern_keys": ["rm_rf"],
        }
        result_holder = {}
        notified = threading.Event()

        def _notify_cb(_data):
            notified.set()

        def _worker():
            result_holder["result"] = mod._await_gateway_decision(
                self.SESSION_KEY, _notify_cb, approval_data
            )

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        assert notified.wait(timeout=5)

        # Flag an interrupt on a thread that is NOT the worker.
        set_interrupt(True, threading.get_ident())

        time.sleep(1.2)
        assert t.is_alive(), "foreign interrupt unexpectedly resolved approval"
        assert "result" not in result_holder

        set_interrupt(False, threading.get_ident())
        mod.resolve_gateway_approval(self.SESSION_KEY, "deny")
        t.join(timeout=5)
        assert not t.is_alive()
        assert result_holder["result"] == {"resolved": True, "choice": "deny", "reason": None}



def test_invalid_callback_choice_fails_closed():
    from tools.approval import prompt_dangerous_approval

    result = prompt_dangerous_approval(
        "rm -rf /",
        "recursive delete",
        approval_callback=lambda *_args, **_kwargs: "cancel",
    )
    assert result == "deny"


def test_invalid_gateway_choice_does_not_resolve_pending_approval():
    import tools.approval as approval

    session_key = "test-invalid-gateway-choice"
    entry = approval._ApprovalEntry({"command": "rm -rf /"})
    with approval._lock:
        approval._gateway_queues[session_key] = [entry]
    try:
        assert approval.resolve_gateway_approval(session_key, "typo_allow") == 0
        assert not entry.event.is_set()
        assert approval.has_blocking_approval(session_key)
        assert approval.resolve_gateway_approval(session_key, "deny") == 1
        assert entry.event.is_set()
        assert entry.result == "deny"
    finally:
        with approval._lock:
            approval._gateway_queues.pop(session_key, None)

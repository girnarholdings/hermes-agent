"""Tests for /notify approval-request notifications."""


def test_fire_approval_request_notification_uses_input_needed_message(monkeypatch):
    from tools import notify_utils

    calls = []
    monkeypatch.setattr(
        notify_utils,
        "fire_notification",
        lambda *, title="Hermes Agent", message="Task complete", config=None: calls.append(
            {"title": title, "message": message, "config": config}
        ),
    )

    notify_utils.fire_approval_request_notification()

    assert calls == [
        {"title": "Hermes Agent", "message": "Input needed: approval required", "config": None}
    ]


def test_fire_approval_request_notification_does_not_clear_pending_notify(monkeypatch, tmp_path):
    from tools import notify_utils

    monkeypatch.setattr(notify_utils, "_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(notify_utils, "fire_notification", lambda **kwargs: None)
    notify_utils.set_notify_flag()

    notify_utils.fire_approval_request_notification()

    assert notify_utils.is_notify_pending() is True


def test_approval_request_notification_skips_messaging_gateway_platform(monkeypatch):
    from gateway import session_context
    from tools import approval
    from tools import notify_utils

    calls = []
    monkeypatch.setattr(
        session_context,
        "get_session_env",
        lambda name, default="": "telegram" if name == "HERMES_SESSION_PLATFORM" else default,
    )
    monkeypatch.setattr(notify_utils, "is_notify_pending", lambda: True)
    monkeypatch.setattr(notify_utils, "fire_approval_request_notification", lambda: calls.append("approval"))

    approval._notify_approval_request_if_pending()

    assert calls == []


def test_check_all_command_guards_notifies_when_cli_approval_requested(monkeypatch):
    from tools import approval
    from tools import notify_utils

    calls = []
    monkeypatch.setenv("HERMES_INTERACTIVE", "1")
    monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
    monkeypatch.delenv("HERMES_EXEC_ASK", raising=False)
    monkeypatch.delenv("HERMES_YOLO_MODE", raising=False)
    monkeypatch.setattr(approval, "_get_approval_mode", lambda: "manual")
    monkeypatch.setattr(approval, "detect_hardline_command", lambda command: (False, None))
    monkeypatch.setattr(
        approval,
        "detect_dangerous_command",
        lambda command: (True, "dangerous:test", "test approval"),
    )
    monkeypatch.setattr(approval, "is_approved", lambda session_key, pattern_key: False)
    monkeypatch.setattr(approval, "prompt_dangerous_approval", lambda *args, **kwargs: "deny")
    monkeypatch.setattr(notify_utils, "is_notify_pending", lambda: True)
    monkeypatch.setattr(notify_utils, "fire_approval_request_notification", lambda: calls.append("approval"))

    result = approval.check_all_command_guards("rm -rf /tmp/demo", "local")

    assert result["approved"] is False
    assert calls == ["approval"]


def test_gateway_approval_prompt_is_emitted_before_desktop_notification(monkeypatch):
    from tools import approval
    from tools import notify_utils

    calls = []
    session_key = "notify-order-session"

    def notify_cb(_approval_data):
        calls.append("approval-prompt")
        approval.resolve_gateway_approval(session_key, "deny")

    monkeypatch.setenv("HERMES_GATEWAY_SESSION", "1")
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
    monkeypatch.delenv("HERMES_EXEC_ASK", raising=False)
    monkeypatch.delenv("HERMES_YOLO_MODE", raising=False)
    monkeypatch.setattr(approval, "get_current_session_key", lambda default="default": session_key)
    monkeypatch.setattr(approval, "_get_approval_mode", lambda: "manual")
    monkeypatch.setattr(approval, "_get_approval_config", lambda: {"gateway_timeout": 1})
    monkeypatch.setattr(approval, "detect_hardline_command", lambda command: (False, None))
    monkeypatch.setattr(
        approval,
        "detect_dangerous_command",
        lambda command: (True, "dangerous:test", "test approval"),
    )
    monkeypatch.setattr(approval, "is_approved", lambda session_key, pattern_key: False)
    monkeypatch.setattr(notify_utils, "is_notify_pending", lambda: True)
    monkeypatch.setattr(notify_utils, "fire_approval_request_notification", lambda: calls.append("desktop-notify"))
    approval.register_gateway_notify(session_key, notify_cb)

    result = approval.check_all_command_guards("rm -rf /tmp/demo", "local")

    assert result["approved"] is False
    assert calls == ["approval-prompt", "desktop-notify"]


def test_check_all_command_guards_skips_approval_notification_without_notify_pending(monkeypatch):
    from tools import approval
    from tools import notify_utils

    calls = []
    monkeypatch.setenv("HERMES_INTERACTIVE", "1")
    monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
    monkeypatch.delenv("HERMES_EXEC_ASK", raising=False)
    monkeypatch.delenv("HERMES_YOLO_MODE", raising=False)
    monkeypatch.setattr(approval, "_get_approval_mode", lambda: "manual")
    monkeypatch.setattr(approval, "detect_hardline_command", lambda command: (False, None))
    monkeypatch.setattr(
        approval,
        "detect_dangerous_command",
        lambda command: (True, "dangerous:test", "test approval"),
    )
    monkeypatch.setattr(approval, "is_approved", lambda session_key, pattern_key: False)
    monkeypatch.setattr(approval, "prompt_dangerous_approval", lambda *args, **kwargs: "deny")
    monkeypatch.setattr(notify_utils, "is_notify_pending", lambda: False)
    monkeypatch.setattr(notify_utils, "fire_approval_request_notification", lambda: calls.append("approval"))

    result = approval.check_all_command_guards("rm -rf /tmp/demo", "local")

    assert result["approved"] is False
    assert calls == []

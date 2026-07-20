"""Hermetic coverage for the redacted multiplex adapter runtime inventory."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from gateway.adapter_runtime import (
    ADAPTER_RUNTIME_FIELDS,
    DuplicateCredentialError,
    evaluate_adapter_runtime_inventory,
    get_adapter_runtime_path,
    read_adapter_runtime_inventory,
    reset_adapter_runtime_inventory,
    update_adapter_runtime,
)
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.run import GatewayRunner


def _record(profile: str, platform: str):
    return next(
        item
        for item in read_adapter_runtime_inventory()
        if item["profile"] == profile and item["platform"] == platform
    )


def test_seven_adapter_inventory_has_exact_schema_and_no_secret_canaries(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    reset_adapter_runtime_inventory()
    forbidden = (
        "credential-canary-do-not-persist",
        "fingerprint-canary-do-not-persist",
        "user-id-canary-do-not-persist",
        "chat-id-canary-do-not-persist",
        "message-id-canary-do-not-persist",
        "content-canary-do-not-persist",
    )

    for index in range(7):
        update_adapter_runtime(
            f"profile-{index}",
            "telegram",
            configured=True,
            enabled=True,
            connected=True,
            authenticated=True,
            poll_succeeded=True,
        )

    records = read_adapter_runtime_inventory()
    raw = get_adapter_runtime_path().read_bytes()
    assert len(records) == 7
    assert all(tuple(item) == ADAPTER_RUNTIME_FIELDS for item in records)
    for canary in forbidden:
        assert canary.encode() not in raw


def test_atomic_path_mode_and_replace(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import utils

    real_replace = utils.os.replace
    replacements = []

    def _record_replace(source, target):
        replacements.append((Path(source), Path(target)))
        return real_replace(source, target)

    monkeypatch.setattr(utils.os, "replace", _record_replace)
    update_adapter_runtime(
        "default", "telegram", configured=True, enabled=True
    )

    target = tmp_path / "gateway_adapter_runtime.json"
    assert get_adapter_runtime_path() == target
    assert target.exists()
    assert os.stat(target).st_mode & 0o777 == 0o600
    assert replacements and replacements[-1][1] == target
    assert replacements[-1][0].parent == target.parent
    assert not list(tmp_path.glob(".gateway_adapter_runtime_*.tmp"))

    # Multiplex profile scopes must never split the process inventory across
    # profile homes: Infra reads one gateway-owned file.
    from hermes_constants import (
        reset_hermes_home_override,
        set_hermes_home_override,
    )

    token = set_hermes_home_override(tmp_path / "profiles" / "worker")
    try:
        assert get_adapter_runtime_path() == target
    finally:
        reset_hermes_home_override(token)


@pytest.mark.asyncio
async def test_auth_failure_records_class_only_and_never_exception_text(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    reset_adapter_runtime_inventory()
    credential_canary = "credential-canary-in-config"
    exception_text_canary = "private-auth-response-content"

    class InvalidCredential(Exception):
        pass

    class _Adapter:
        def __init__(self):
            self.token = credential_canary
            self.platform = Platform.TELEGRAM
            self.disconnected = False
            self._observer = None

        def set_adapter_runtime_observer(self, observer):
            self._observer = observer

        def set_message_handler(self, _handler):
            pass

        def set_fatal_error_handler(self, _handler):
            pass

        def set_session_store(self, _store):
            pass

        def set_busy_session_handler(self, _handler):
            pass

        def set_topic_recovery_fn(self, _handler):
            pass

        def set_authorization_check(self, _handler):
            pass

        async def disconnect(self):
            self.disconnected = True

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=True)
    runner._profile_adapters = {}
    runner.session_store = object()
    runner._busy_text_mode = "off"
    adapter = _Adapter()

    profile_cfg = GatewayConfig(multiplex_profiles=True)
    profile_cfg.platforms = {
        Platform.TELEGRAM: PlatformConfig(
            enabled=True, token=credential_canary
        )
    }

    async def _fail_connect(_adapter, _platform):
        raise InvalidCredential(exception_text_canary)

    monkeypatch.setattr("gateway.config.load_gateway_config", lambda: profile_cfg)
    monkeypatch.setattr(runner, "_create_adapter", lambda _p, _c: adapter)
    monkeypatch.setattr(runner, "_connect_adapter_with_timeout", _fail_connect)
    monkeypatch.setattr(
        runner, "_make_adapter_auth_check", lambda _p, profile_name=None: None
    )
    monkeypatch.setattr(runner, "_adapter_disconnect_timeout_secs", lambda: 0)

    connected = await runner._start_one_profile_adapters(
        "worker", tmp_path / "worker", {}
    )
    record = _record("worker", "telegram")
    raw = get_adapter_runtime_path().read_bytes()

    assert connected == 0
    assert adapter.disconnected is True
    assert record["configured"] is True
    assert record["enabled"] is True
    assert record["connected"] is False
    assert record["authenticated"] is False
    assert record["last_error_class"] == "InvalidCredential"
    assert credential_canary.encode() not in raw
    assert exception_text_canary.encode() not in raw


def test_health_startup_grace_error_stale_and_recovery(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    reset_adapter_runtime_inventory()
    update_adapter_runtime(
        "worker", "telegram", configured=True, enabled=True
    )
    starting = read_adapter_runtime_inventory()
    now = datetime.now(timezone.utc)

    assert evaluate_adapter_runtime_inventory(
        starting,
        now=now,
        gateway_state="starting",
        startup_grace_seconds=120,
    )[0]["state"] == "starting"
    assert evaluate_adapter_runtime_inventory(
        starting,
        now=now,
        gateway_state="running",
        startup_grace_seconds=120,
    )[0]["state"] == "unhealthy"

    update_adapter_runtime("worker", "telegram", error=PermissionError)
    failed = read_adapter_runtime_inventory()
    assert evaluate_adapter_runtime_inventory(
        failed, now=now, gateway_state="starting"
    )[0]["state"] == "error"

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._record_adapter_runtime_event(
        "worker", "telegram", "poll_success"
    )
    recovered = read_adapter_runtime_inventory()
    recovered_record = recovered[0]
    assert recovered_record["last_error_class"] == "PermissionError"
    assert evaluate_adapter_runtime_inventory(
        recovered, now=datetime.now(timezone.utc), gateway_state="running"
    )[0]["state"] == "healthy"

    stale_at = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
    recovered_record["last_successful_poll_at"] = stale_at
    assert evaluate_adapter_runtime_inventory(
        [recovered_record],
        now=datetime.now(timezone.utc),
        gateway_state="running",
        poll_stale_after_seconds=300,
    )[0]["state"] == "stale"


def test_shutdown_marks_adapter_stopped_without_erasing_last_error(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    update_adapter_runtime(
        "worker",
        "telegram",
        configured=True,
        enabled=True,
        connected=True,
        authenticated=True,
        poll_succeeded=True,
        error=ConnectionError,
    )
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._record_adapter_runtime_event("worker", "telegram", "stopped")

    record = _record("worker", "telegram")
    assert record["connected"] is False
    assert record["authenticated"] is False
    assert record["last_error_class"] == "ConnectionError"
    assert evaluate_adapter_runtime_inventory(
        [record], gateway_state="stopped"
    )[0] == {
        "profile": "worker",
        "platform": "telegram",
        "state": "stopped",
    }


@pytest.mark.asyncio
async def test_duplicate_credential_remains_fail_closed_and_inventory_is_redacted(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    credential_canary = "duplicate-credential-canary"

    class _Adapter:
        def __init__(self):
            self.token = credential_canary
            self.disconnected = False

        async def disconnect(self):
            self.disconnected = True

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=True)
    runner._profile_adapters = {}
    duplicate = _Adapter()
    profile_cfg = GatewayConfig(multiplex_profiles=True)
    profile_cfg.platforms = {
        Platform.TELEGRAM: PlatformConfig(
            enabled=True, token=credential_canary
        )
    }
    fingerprint = GatewayRunner._adapter_credential_fingerprint(duplicate)
    claimed = {(Platform.TELEGRAM, fingerprint): "default"}
    monkeypatch.setattr("gateway.config.load_gateway_config", lambda: profile_cfg)
    monkeypatch.setattr(runner, "_create_adapter", lambda _p, _c: duplicate)
    monkeypatch.setattr(runner, "_adapter_disconnect_timeout_secs", lambda: 0)

    connected = await runner._start_one_profile_adapters(
        "worker", tmp_path / "worker", claimed
    )
    raw = get_adapter_runtime_path().read_bytes()
    record = _record("worker", "telegram")

    assert connected == 0
    assert duplicate.disconnected is True
    assert record["last_error_class"] == DuplicateCredentialError.__name__
    assert credential_canary.encode() not in raw
    assert fingerprint.encode() not in raw

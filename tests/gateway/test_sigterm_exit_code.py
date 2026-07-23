"""Unexpected-SIGTERM shutdowns exit 143 and the generated unit whitelists it.

An unmarked SIGTERM (operator ``systemctl restart``/``stop`` without the
CLI's planned-stop marker) used to exit 1, so every operator restart logged
"Failed with result 'exit-code'" in systemd. The gateway now exits
128+SIGTERM (143) and the generated unit lists that code in
``SuccessExitStatus``: generated units record a clean stop, while
operator-managed ``Restart=on-failure`` units still see a non-zero exit and
revive the gateway after external kills.
"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import gateway.run as gateway_run
from gateway.config import GatewayConfig
from gateway.restart import GATEWAY_SIGTERM_EXIT_CODE
from hermes_cli import gateway as gateway_cli


class _ExitCalled(Exception):
    def __init__(self, code: int):
        super().__init__(code)
        self.code = code


def _raise_exit(code: int) -> None:
    raise _ExitCalled(code)


def test_sigterm_exit_code_is_shell_convention():
    assert GATEWAY_SIGTERM_EXIT_CODE == 143  # 128 + SIGTERM(15)


def test_main_force_exits_143_after_signal_initiated_shutdown(monkeypatch):
    async def fake_start_gateway(config=None):
        raise SystemExit(GATEWAY_SIGTERM_EXIT_CODE)

    stdout = SimpleNamespace(flush=Mock())
    stderr = SimpleNamespace(flush=Mock())

    monkeypatch.setattr(gateway_run, "start_gateway", fake_start_gateway)
    monkeypatch.setattr(gateway_run.os, "_exit", _raise_exit)
    monkeypatch.setattr(gateway_run.sys, "argv", ["gateway.run"])
    monkeypatch.setattr(gateway_run.sys, "stdout", stdout)
    monkeypatch.setattr(gateway_run.sys, "stderr", stderr)

    with pytest.raises(_ExitCalled) as exc_info:
        gateway_run.main()

    assert exc_info.value.code == GATEWAY_SIGTERM_EXIT_CODE


def test_user_unit_whitelists_sigterm_exit_status(monkeypatch):
    monkeypatch.setattr(gateway_cli, "load_gateway_config", GatewayConfig)

    unit = gateway_cli.generate_systemd_unit(system=False)

    assert f"SuccessExitStatus={GATEWAY_SIGTERM_EXIT_CODE}" in unit
    assert "Restart=always" in unit


def test_system_unit_whitelists_sigterm_exit_status(monkeypatch, tmp_path):
    monkeypatch.setattr(gateway_cli, "load_gateway_config", GatewayConfig)
    monkeypatch.setattr(
        gateway_cli,
        "_system_service_identity",
        lambda _user: ("hermes", "hermes", str(tmp_path)),
    )

    unit = gateway_cli.generate_systemd_unit(system=True, run_as_user="hermes")

    assert f"SuccessExitStatus={GATEWAY_SIGTERM_EXIT_CODE}" in unit
    assert "Restart=always" in unit

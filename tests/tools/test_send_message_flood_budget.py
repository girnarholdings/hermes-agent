"""Focused tests for Telegram flood-control retry budgeting in
``tools/send_message_tool.py``.

Regression coverage for the 2026-07-19 delivery incident: flood control
returned ``retry_after=249-255s``; the previous fixed 3-attempt cap raised on
attempt 3 and the message was dropped. Flood errors must instead keep honoring
the server's ``retry_after`` until a wall-clock budget is exhausted, while
non-flood transient errors keep the classic 3-attempt cap.
"""

from types import SimpleNamespace

import pytest

# python-telegram-bot is an optional dep; keep the skip consistent with the
# other Telegram send-path test modules.
pytest.importorskip("telegram", reason="python-telegram-bot not installed")

from tools import send_message_tool as smt


class _FloodError(Exception):
    """Mimics ``telegram.error.RetryAfter``: carries a server ``retry_after``."""

    def __init__(self, retry_after):
        super().__init__(f"Flood control exceeded. Retry in {int(retry_after)} seconds")
        self.retry_after = retry_after


class _TransientError(Exception):
    """A non-flood transient error (matches ``_telegram_retry_delay``'s 5xx list)."""

    def __init__(self):
        super().__init__("Bad Gateway (502)")


def _deterministic_flood_clock(monkeypatch, start=1000.0):
    """Freeze ``time.monotonic`` and make ``asyncio.sleep`` advance it, so budget
    math is exercised without any real waiting. Jitter is pinned to 0."""
    clock = [start]
    monkeypatch.setattr(smt.time, "monotonic", lambda: clock[0])

    async def fake_sleep(delay):
        clock[0] += delay

    monkeypatch.setattr(smt.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(smt.random, "uniform", lambda a, b: 0.0)
    return clock


# ---------------------------------------------------------------------------
# Flood-control retry budget
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_flood_retry_honors_retry_after_within_budget(monkeypatch):
    _deterministic_flood_clock(monkeypatch)
    calls = [0]

    async def send_message(**kwargs):
        calls[0] += 1
        if calls[0] <= 4:  # 4 floods x 120s = 480s < 500s budget
            raise _FloodError(120)
        return SimpleNamespace(message_id=7)

    bot = SimpleNamespace(send_message=send_message)

    result = await smt._send_telegram_message_with_retry(
        bot, chat_id=1, text="hi", flood_budget_seconds=500
    )

    assert result.message_id == 7
    # 5 total attempts (4 retries) — far past the old fixed 3-attempt cap.
    assert calls[0] == 5


@pytest.mark.asyncio
async def test_flood_retry_gives_up_after_budget(monkeypatch):
    _deterministic_flood_clock(monkeypatch)
    calls = [0]

    async def send_message(**kwargs):
        calls[0] += 1
        raise _FloodError(255)  # server keeps flooding at 255s

    bot = SimpleNamespace(send_message=send_message)

    with pytest.raises(_FloodError):
        await smt._send_telegram_message_with_retry(
            bot, chat_id=1, text="hi", flood_budget_seconds=600
        )

    # t0=1000, deadline=1600. attempt1 wait 255 -> 1255<=1600 (sleep).
    # attempt2 now 1255 wait 255 -> 1510<=1600 (sleep).
    # attempt3 now 1510 wait 255 -> 1765>1600 -> give up. 3 sends total.
    assert calls[0] == 3


@pytest.mark.asyncio
async def test_non_flood_transient_keeps_three_attempt_cap(monkeypatch):
    async def fake_sleep(delay):
        return None

    monkeypatch.setattr(smt.asyncio, "sleep", fake_sleep)
    calls = [0]

    async def send_message(**kwargs):
        calls[0] += 1
        raise _TransientError()

    bot = SimpleNamespace(send_message=send_message)

    with pytest.raises(_TransientError):
        await smt._send_telegram_message_with_retry(bot, chat_id=1, text="hi")

    # Non-flood transient errors keep the classic 3-attempt behavior.
    assert calls[0] == 3


def test_is_telegram_flood_error_classification():
    assert smt._is_telegram_flood_error(_FloodError(10)) is True
    assert (
        smt._is_telegram_flood_error(
            Exception("Flood control exceeded. Retry in 5 seconds")
        )
        is True
    )
    assert smt._is_telegram_flood_error(_TransientError()) is False

"""Hermetic tests for the cron delivery outbox (queue-and-drain).

Covers: enqueue + idempotent keys, age/count clog bounds, FIFO drain with
stop-on-failure, retryable-error classification. No network, no Telegram.
"""

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest


@pytest.fixture()
def outbox_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # Re-import with fresh env so _outbox_path picks up tmp_path.
    import importlib

    import cron.delivery_outbox as do

    importlib.reload(do)
    yield do, tmp_path / "cron" / "delivery_outbox.jsonl"


def test_enqueue_and_drain_fifo(outbox_env):
    do, path = outbox_env
    sent = []
    do.enqueue("j1", "Job One", "telegram:1", "first", key="k1")
    do.enqueue("j2", "Job Two", "telegram:1", "second", key="k2")
    do.enqueue("j3", "Job Three", "telegram:1", "third", key="k3")
    assert do.pending_count() == 3

    def send(target, content, media):
        sent.append(content)
        return True

    # Batch default is 3 -> all three delivered FIFO.
    n = do.drain(send)
    assert n == 3
    assert sent == ["first", "second", "third"]
    assert do.pending_count() == 0
    assert not path.exists() or path.read_text().strip() == ""


def test_drain_stops_on_failure(outbox_env):
    do, _ = outbox_env
    do.enqueue("j1", "J", "telegram:1", "a", key="k1")
    do.enqueue("j2", "J", "telegram:1", "b", key="k2")

    def send(target, content, media):
        return False  # channel still down

    assert do.drain(send) == 0
    assert do.pending_count() == 2  # nothing lost

    # Partial recovery: first succeeds, second fails -> 1 delivered, order kept.
    state = {"n": 0}

    def flaky(target, content, media):
        state["n"] += 1
        return state["n"] == 1

    assert do.drain(flaky) == 1
    assert do.pending_count() == 1


def test_idempotent_key(outbox_env):
    do, _ = outbox_env
    do.enqueue("j1", "J", "telegram:1", "msg", key="same")
    do.enqueue("j1", "J", "telegram:1", "msg", key="same")
    assert do.pending_count() == 1


def test_clog_bounds(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_DELIVERY_OUTBOX_MAX_ENTRIES", "2")
    monkeypatch.setenv("HERMES_DELIVERY_OUTBOX_MAX_AGE", "3600")
    import importlib

    import cron.delivery_outbox as do

    importlib.reload(do)
    do.enqueue("j1", "J", "t:1", "one", key="k1")
    do.enqueue("j2", "J", "t:1", "two", key="k2")
    do.enqueue("j3", "J", "t:1", "three", key="k3")  # over cap -> oldest shed
    assert do.pending_count() == 2
    assert do._read_all(do._outbox_path())[0]["content"] == "two"

    # Age expiry: an entry older than the window is dropped on next enqueue.
    path = do._outbox_path()
    recs = do._read_all(path)
    recs[0]["queued_at"] = time.time() - 7200
    do._write_all(path, recs)
    do.enqueue("j4", "J", "t:1", "four", key="k4")
    assert all(r["content"] != "two" for r in do._read_all(path))


def test_retryable_classification():
    from cron.delivery_outbox import is_retryable_delivery_failure

    assert is_retryable_delivery_failure("Telegram send failed: Timed out (target telegram:1)")
    assert is_retryable_delivery_failure("send_path_degraded")
    assert is_retryable_delivery_failure("Connection error to api.telegram.org")
    assert not is_retryable_delivery_failure("chat not found")
    assert not is_retryable_delivery_failure("")
    assert not is_retryable_delivery_failure("bot was blocked by the user")


def test_drain_survives_send_exception(outbox_env):
    do, _ = outbox_env
    do.enqueue("j1", "J", "telegram:1", "x", key="k1")

    def boom(target, content, media):
        raise RuntimeError("channel exploded")

    assert do.drain(boom) == 0
    assert do.pending_count() == 1  # still queued, tick survived

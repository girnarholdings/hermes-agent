from concurrent.futures import Future
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import cron.scheduler as scheduler
from cron.delivery_receipts import (
    DeliveryEvidence,
    DeliveryReceiptLedger,
    DeliveryReceiptRetryable,
    DeliveryReceiptUnknown,
    build_receipt_key,
)
from gateway.config import Platform
from gateway.platforms.base import SendResult


ARTIFACT = "sha256:" + "b" * 64


def _evidence():
    return DeliveryEvidence(
        namespace="screener",
        message_class="equity-daily",
        pipeline="equity-screener",
        as_of_slot="2026-07-15T08:00:00-04:00",
        artifact_identity=ARTIFACT,
    )


def _job():
    return {
        "id": "native-receipt-job",
        "name": "Equity Screener",
        "deliver": "origin",
        "origin": {
            "platform": "telegram",
            "chat_id": "12345",
            "thread_id": "77",
        },
    }


def _receipt(tmp_path):
    key = build_receipt_key(
        _evidence(),
        platform="telegram",
        chat_id="12345",
        thread_id="77",
    )
    return DeliveryReceiptLedger(tmp_path / "state" / "delivery_receipts"), key


def _config():
    pconfig = MagicMock()
    pconfig.enabled = True
    pconfig.extra = {}
    config = MagicMock()
    config.platforms = {Platform.TELEGRAM: pconfig}
    config.filter_silence_narration = False
    return config


def _loop():
    loop = MagicMock()
    loop.is_running.return_value = True
    return loop


def _completed_future(value):
    future = Future()
    future.set_result(value)
    return future


def _deliver_with_live_result(tmp_path, monkeypatch, live_result, standalone_result=None):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config = _config()
    adapter = MagicMock()
    standalone = AsyncMock(
        return_value=standalone_result
        if standalone_result is not None
        else {"success": True, "message_id": "standalone-42"}
    )
    future = _completed_future(live_result)

    def schedule(coro, _loop_arg):
        coro.close()
        return future

    with patch("gateway.config.load_gateway_config", return_value=config), patch(
        "cron.scheduler.load_config",
        return_value={"cron": {"wrap_response": False}},
    ), patch(
        "agent.async_utils.safe_schedule_threadsafe",
        side_effect=schedule,
    ), patch(
        "tools.send_message_tool._send_to_platform",
        new=standalone,
    ):
        result = scheduler._deliver_result(
            _job(),
            "Screener report",
            adapters={Platform.TELEGRAM: adapter},
            loop=_loop(),
            delivery_evidence=_evidence(),
        )
    return result, standalone


def test_live_send_marks_sent_only_with_durable_message_id(tmp_path, monkeypatch):
    result, standalone = _deliver_with_live_result(
        tmp_path,
        monkeypatch,
        SendResult(success=True, message_id="live-42"),
    )
    ledger, key = _receipt(tmp_path)
    record = ledger.get(key)

    assert result is None
    standalone.assert_not_awaited()
    assert record["state"] == "sent"
    assert record["receipt_id"] == "live-42"
    assert record["attempts"][0]["transport"] == "live_adapter"


@pytest.mark.parametrize(
    "live_result",
    [
        None,
        SendResult(success=True, message_id=None),
        SendResult(
            success=True,
            message_id="last-chunk",
            raw_response={
                "partial_overflow": True,
                "delivered_chunks": 1,
                "total_chunks": 2,
            },
        ),
    ],
)
def test_live_none_missing_receipt_or_partial_is_unknown_without_fallback(
    tmp_path,
    monkeypatch,
    live_result,
):
    with pytest.raises(DeliveryReceiptUnknown):
        _, standalone = _deliver_with_live_result(
            tmp_path,
            monkeypatch,
            live_result,
        )

    ledger, key = _receipt(tmp_path)
    assert ledger.get(key)["state"] == "unknown"


def test_dict_success_true_delivered_false_is_not_sent(tmp_path, monkeypatch):
    result, standalone = _deliver_with_live_result(
        tmp_path,
        monkeypatch,
        {"success": True, "delivered": False},
        standalone_result={"success": True, "message_id": "fallback-42"},
    )
    ledger, key = _receipt(tmp_path)
    record = ledger.get(key)

    assert result is None
    standalone.assert_awaited_once()
    assert record["state"] == "sent"
    assert [attempt["outcome"] for attempt in record["attempts"]] == [
        "not-sent",
        "sent",
    ]
    assert [attempt["transport"] for attempt in record["attempts"]] == [
        "live_adapter",
        "standalone",
    ]


class _TimeoutFuture:
    def __init__(self, *, cancelled):
        self.cancelled = cancelled

    def result(self, timeout=None):
        raise TimeoutError("confirmation timeout")

    def cancel(self):
        return self.cancelled


def _deliver_with_future(tmp_path, monkeypatch, future, standalone_result=None):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config = _config()
    standalone = AsyncMock(
        return_value=standalone_result
        or {"success": True, "message_id": "standalone-42"}
    )

    def schedule(coro, _loop_arg):
        coro.close()
        return future

    with patch("gateway.config.load_gateway_config", return_value=config), patch(
        "cron.scheduler.load_config",
        return_value={"cron": {"wrap_response": False}},
    ), patch(
        "agent.async_utils.safe_schedule_threadsafe",
        side_effect=schedule,
    ), patch(
        "tools.send_message_tool._send_to_platform",
        new=standalone,
    ):
        result = scheduler._deliver_result(
            _job(),
            "Screener report",
            adapters={Platform.TELEGRAM: MagicMock()},
            loop=_loop(),
            delivery_evidence=_evidence(),
        )
    return result, standalone


def test_live_inflight_timeout_is_unknown_and_never_falls_back(tmp_path, monkeypatch):
    with pytest.raises(DeliveryReceiptUnknown):
        _deliver_with_future(
            tmp_path,
            monkeypatch,
            _TimeoutFuture(cancelled=False),
        )

    ledger, key = _receipt(tmp_path)
    assert ledger.get(key)["state"] == "unknown"


def test_live_cancel_before_dispatch_records_not_sent_then_falls_back(
    tmp_path,
    monkeypatch,
):
    result, standalone = _deliver_with_future(
        tmp_path,
        monkeypatch,
        _TimeoutFuture(cancelled=True),
    )
    ledger, key = _receipt(tmp_path)
    record = ledger.get(key)

    assert result is None
    standalone.assert_awaited_once()
    assert record["state"] == "sent"
    assert [attempt["outcome"] for attempt in record["attempts"]] == [
        "not-sent",
        "sent",
    ]


def test_live_exception_is_unknown_without_fallback(tmp_path, monkeypatch):
    future = Future()
    future.set_exception(ConnectionError("provider disconnected"))

    with pytest.raises(DeliveryReceiptUnknown):
        _deliver_with_future(tmp_path, monkeypatch, future)

    ledger, key = _receipt(tmp_path)
    assert ledger.get(key)["state"] == "unknown"


def test_standalone_missing_message_id_is_unknown(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config = _config()
    standalone = AsyncMock(return_value={"success": True})

    with patch("gateway.config.load_gateway_config", return_value=config), patch(
        "cron.scheduler.load_config",
        return_value={"cron": {"wrap_response": False}},
    ), patch(
        "tools.send_message_tool._send_to_platform",
        new=standalone,
    ):
        with pytest.raises(DeliveryReceiptUnknown):
            scheduler._deliver_result(
                _job(),
                "Screener report",
                delivery_evidence=_evidence(),
            )

    ledger, key = _receipt(tmp_path)
    assert ledger.get(key)["state"] == "unknown"


def test_sent_receipt_suppresses_all_duplicate_transport(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ledger, key = _receipt(tmp_path)
    claim = ledger.begin_send(key, transport="live_adapter")
    ledger.record_outcome(
        key,
        attempt_id=claim["attempt_id"],
        outcome="sent",
        receipt_id="existing-42",
    )
    config = _config()
    standalone = AsyncMock()
    schedule = MagicMock()

    with patch("gateway.config.load_gateway_config", return_value=config), patch(
        "cron.scheduler.load_config",
        return_value={"cron": {"wrap_response": False}},
    ), patch(
        "agent.async_utils.safe_schedule_threadsafe",
        new=schedule,
    ), patch(
        "tools.send_message_tool._send_to_platform",
        new=standalone,
    ):
        result = scheduler._deliver_result(
            _job(),
            "Screener report",
            adapters={Platform.TELEGRAM: MagicMock()},
            loop=_loop(),
            delivery_evidence=_evidence(),
        )

    assert result is None
    schedule.assert_not_called()
    standalone.assert_not_awaited()


def test_begin_is_durable_before_standalone_transport(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ledger, key = _receipt(tmp_path)

    async def standalone(*args, **kwargs):
        assert ledger.get(key)["state"] == "unknown"
        return {"success": True, "message_id": "42"}

    with patch("gateway.config.load_gateway_config", return_value=_config()), patch(
        "cron.scheduler.load_config",
        return_value={"cron": {"wrap_response": False}},
    ), patch(
        "tools.send_message_tool._send_to_platform",
        new=standalone,
    ):
        scheduler._deliver_result(
            _job(),
            "Screener report",
            delivery_evidence=_evidence(),
        )

    assert ledger.get(key)["state"] == "sent"


def test_begin_is_durable_before_live_transport(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ledger, key = _receipt(tmp_path)
    config = _config()
    standalone = AsyncMock()

    def schedule(coro, _loop_arg):
        assert ledger.get(key)["state"] == "unknown"
        coro.close()
        return _completed_future(SendResult(success=True, message_id="live-42"))

    with patch("gateway.config.load_gateway_config", return_value=config), patch(
        "cron.scheduler.load_config",
        return_value={"cron": {"wrap_response": False}},
    ), patch(
        "agent.async_utils.safe_schedule_threadsafe",
        side_effect=schedule,
    ), patch(
        "tools.send_message_tool._send_to_platform",
        new=standalone,
    ):
        scheduler._deliver_result(
            _job(),
            "Screener report",
            adapters={Platform.TELEGRAM: MagicMock()},
            loop=_loop(),
            delivery_evidence=_evidence(),
        )

    assert ledger.get(key)["state"] == "sent"
    standalone.assert_not_awaited()


def test_confirmed_standalone_no_send_is_retryable(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    standalone = AsyncMock(return_value={"success": True, "delivered": False})

    with patch("gateway.config.load_gateway_config", return_value=_config()), patch(
        "cron.scheduler.load_config",
        return_value={"cron": {"wrap_response": False}},
    ), patch(
        "tools.send_message_tool._send_to_platform",
        new=standalone,
    ):
        with pytest.raises(DeliveryReceiptRetryable):
            scheduler._deliver_result(
                _job(),
                "Screener report",
                delivery_evidence=_evidence(),
            )

    ledger, key = _receipt(tmp_path)
    assert ledger.get(key)["state"] == "failed"


def test_no_agent_marker_survives_run_job_and_is_removed_from_delivery_text(
    monkeypatch,
):
    import json

    marker = "SCREENER_DELIVERY_EVIDENCE=" + json.dumps(
        {
            "version": 1,
            "key": {
                "message_class": "equity-daily",
                "pipeline": "equity-screener",
                "as_of_slot": "2026-07-15T08:00:00-04:00",
                "artifact_identity": ARTIFACT,
            },
        },
        separators=(",", ":"),
    )
    monkeypatch.setattr(
        scheduler,
        "_run_job_script",
        lambda _path: (True, f"{marker}\nScreener report"),
    )
    evidence = []

    success, _output, final_response, error = scheduler.run_job(
        {
            "id": "no-agent-evidence",
            "name": "Equity Screener",
            "script": "producer.py",
            "no_agent": True,
        },
        delivery_evidence_out=evidence,
    )

    assert success is True
    assert error is None
    assert final_response == "Screener report"
    assert evidence == [_evidence()]


def test_build_job_prompt_extracts_evidence_before_prompt_injection(monkeypatch):
    import json

    marker = "SCREENER_DELIVERY_EVIDENCE=" + json.dumps(
        {
            "version": 1,
            "key": {
                "message_class": "equity-daily",
                "pipeline": "equity-screener",
                "as_of_slot": "2026-07-15T08:00:00-04:00",
                "artifact_identity": ARTIFACT,
            },
        },
        separators=(",", ":"),
    )
    monkeypatch.setattr(
        scheduler,
        "_run_job_script",
        lambda _path: (True, f"{marker}\nproducer data"),
    )
    evidence = []

    prompt = scheduler._build_job_prompt(
        {
            "id": "agent-evidence",
            "name": "Equity Screener",
            "prompt": "Synthesize this data",
            "script": "producer.py",
        },
        delivery_evidence_out=evidence,
    )

    assert "producer data" in prompt
    assert "DELIVERY_EVIDENCE" not in prompt
    assert evidence == [_evidence()]


def test_run_one_passes_evidence_to_delivery(monkeypatch):
    captured = {}

    def run_job(job, *, defer_agent_teardown=None, delivery_evidence_out=None):
        delivery_evidence_out.append(_evidence())
        return True, "output", "Screener report", None

    def deliver(job, content, adapters=None, loop=None, *, delivery_evidence=None):
        captured["evidence"] = delivery_evidence
        return None

    monkeypatch.setattr(scheduler, "claim_dispatch", lambda _job_id: True)
    monkeypatch.setattr(scheduler, "run_job", run_job)
    monkeypatch.setattr(scheduler, "save_job_output", lambda *_args: "/tmp/out")
    monkeypatch.setattr(scheduler, "_deliver_result", deliver)
    monkeypatch.setattr(scheduler, "_record_job_outcome", lambda *args, **kwargs: None)
    monkeypatch.setattr(scheduler, "_consume_interrupted_flag", lambda _job_id: False)
    monkeypatch.setattr(scheduler, "_is_interrupted", lambda _job_id: False)

    assert scheduler.run_one_job(_job()) is True
    assert captured["evidence"] == _evidence()


def test_failed_agent_alert_cannot_consume_producer_receipt(monkeypatch):
    captured = {}
    recorded = {}

    def run_job(job, *, defer_agent_teardown=None, delivery_evidence_out=None):
        delivery_evidence_out.append(_evidence())
        return False, "producer output", "", "model failed"

    def deliver(job, content, adapters=None, loop=None, *, delivery_evidence=None):
        captured["content"] = content
        captured["evidence"] = delivery_evidence
        return None

    def record(job, success, error, delivery_error=None, *, retry_allowed=True):
        recorded.update(success=success, error=error, retry_allowed=retry_allowed)

    monkeypatch.setattr(scheduler, "claim_dispatch", lambda _job_id: True)
    monkeypatch.setattr(scheduler, "run_job", run_job)
    monkeypatch.setattr(scheduler, "save_job_output", lambda *_args: "/tmp/out")
    monkeypatch.setattr(scheduler, "_deliver_result", deliver)
    monkeypatch.setattr(scheduler, "_record_job_outcome", record)
    monkeypatch.setattr(scheduler, "_consume_interrupted_flag", lambda _job_id: False)
    monkeypatch.setattr(scheduler, "_is_interrupted", lambda _job_id: False)

    assert scheduler.run_one_job(_job()) is True
    assert "model failed" in captured["content"]
    assert captured["evidence"] is None
    assert recorded == {
        "success": False,
        "error": "model failed",
        "retry_allowed": True,
    }


def test_unknown_delivery_marks_failure_and_blocks_automatic_retry(monkeypatch):
    recorded = {}

    def run_job(job, *, defer_agent_teardown=None, delivery_evidence_out=None):
        delivery_evidence_out.append(_evidence())
        return True, "output", "Screener report", None

    def deliver(*args, **kwargs):
        raise DeliveryReceiptUnknown("ambiguous transport")

    def record(job, success, error, delivery_error=None, *, retry_allowed=True):
        recorded.update(
            success=success,
            error=error,
            delivery_error=delivery_error,
            retry_allowed=retry_allowed,
        )

    monkeypatch.setattr(scheduler, "claim_dispatch", lambda _job_id: True)
    monkeypatch.setattr(scheduler, "run_job", run_job)
    monkeypatch.setattr(scheduler, "save_job_output", lambda *_args: "/tmp/out")
    monkeypatch.setattr(scheduler, "_deliver_result", deliver)
    monkeypatch.setattr(scheduler, "_record_job_outcome", record)
    monkeypatch.setattr(scheduler, "_consume_interrupted_flag", lambda _job_id: False)
    monkeypatch.setattr(scheduler, "_is_interrupted", lambda _job_id: False)

    assert scheduler.run_one_job(_job()) is True
    assert recorded == {
        "success": False,
        "error": "ambiguous transport",
        "delivery_error": "ambiguous transport",
        "retry_allowed": False,
    }


def test_confirmed_no_send_marks_failure_and_allows_retry(monkeypatch):
    recorded = {}

    def run_job(job, *, defer_agent_teardown=None, delivery_evidence_out=None):
        delivery_evidence_out.append(_evidence())
        return True, "output", "Screener report", None

    def deliver(*args, **kwargs):
        raise DeliveryReceiptRetryable("confirmed not sent")

    def record(job, success, error, delivery_error=None, *, retry_allowed=True):
        recorded.update(success=success, retry_allowed=retry_allowed)

    monkeypatch.setattr(scheduler, "claim_dispatch", lambda _job_id: True)
    monkeypatch.setattr(scheduler, "run_job", run_job)
    monkeypatch.setattr(scheduler, "save_job_output", lambda *_args: "/tmp/out")
    monkeypatch.setattr(scheduler, "_deliver_result", deliver)
    monkeypatch.setattr(scheduler, "_record_job_outcome", record)
    monkeypatch.setattr(scheduler, "_consume_interrupted_flag", lambda _job_id: False)
    monkeypatch.setattr(scheduler, "_is_interrupted", lambda _job_id: False)

    assert scheduler.run_one_job(_job()) is True
    assert recorded == {"success": False, "retry_allowed": True}

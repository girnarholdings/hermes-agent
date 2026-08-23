import json
import os
import stat
import threading

import pytest

from cron.delivery_receipts import (
    DeliveryEvidence,
    DeliveryEvidenceError,
    DeliveryReceiptBlocked,
    DeliveryReceiptError,
    DeliveryReceiptLedger,
    build_receipt_key,
    extract_delivery_evidence,
)


ARTIFACT = "sha256:" + "a" * 64


def _marker(*, namespace="SCREENER", key_overrides=None, payload_overrides=None):
    key = {
        "message_class": "equity-daily",
        "pipeline": "equity-screener",
        "as_of_slot": "2026-07-15/daily",
        "artifact_identity": ARTIFACT,
    }
    key.update(key_overrides or {})
    payload = {
        "version": 1,
        "delivery_action": "send",
        "state": "prepared",
        "key": key,
    }
    payload.update(payload_overrides or {})
    return f"{namespace}_DELIVERY_EVIDENCE={json.dumps(payload, separators=(',', ':'))}"


def _evidence(**overrides):
    values = {
        "namespace": "screener",
        "message_class": "equity-daily",
        "pipeline": "equity-screener",
        "as_of_slot": "2026-07-15/daily",
        "artifact_identity": ARTIFACT,
    }
    values.update(overrides)
    return DeliveryEvidence(**values)


def _key(**overrides):
    target = {
        "platform": "telegram",
        "chat_id": "12345",
        "thread_id": "77",
    }
    target.update(overrides)
    return build_receipt_key(_evidence(), **target)


def test_current_screener_marker_is_parsed_and_removed_from_prompt():
    cleaned, evidence = extract_delivery_evidence(
        f"artifact ready\n{_marker()}\nsummary follows"
    )

    assert cleaned == "artifact ready\nsummary follows"
    assert evidence == _evidence()


@pytest.mark.parametrize(
    "marker",
    [
        "screener_DELIVERY_EVIDENCE={}",
        "SCREENER_DELIVERY_EVIDENCE=not-json",
        _marker(payload_overrides={"version": 2}),
        _marker(payload_overrides={"version": True}),
        _marker(key_overrides={"unexpected": "value"}),
        _marker(key_overrides={"artifact_identity": "../../artifact"}),
        _marker(key_overrides={"as_of_slot": "2026-07-15\nforged"}),
    ],
)
def test_malformed_or_noncanonical_evidence_fails_closed(marker):
    with pytest.raises(DeliveryEvidenceError):
        extract_delivery_evidence(marker)


def test_multiple_markers_fail_closed():
    with pytest.raises(DeliveryEvidenceError, match="multiple"):
        extract_delivery_evidence(f"{_marker()}\n{_marker(namespace='OTHER')}")


def test_duplicate_json_keys_fail_closed():
    marker = (
        'SCREENER_DELIVERY_EVIDENCE={"version":1,"version":1,"key":'
        '{"message_class":"equity-daily","pipeline":"equity-screener",'
        '"as_of_slot":"2026-07-15/daily","artifact_identity":"'
        + ARTIFACT
        + '"}}'
    )

    with pytest.raises(DeliveryEvidenceError, match="duplicate"):
        extract_delivery_evidence(marker)


def test_external_transport_action_is_not_imported():
    _, evidence = extract_delivery_evidence(
        _marker(payload_overrides={"delivery_action": "suppress", "state": "sent"})
    )

    assert evidence == _evidence()
    assert not hasattr(evidence, "delivery_action")


def test_target_binding_separates_platform_chat_thread_and_pipeline():
    base = _key()

    assert _key(platform="slack") != base
    assert _key(chat_id="54321") != base
    assert _key(thread_id="78") != base
    assert build_receipt_key(
        _evidence(pipeline="earnings-screener"),
        platform="telegram",
        chat_id="12345",
        thread_id="77",
    ) != base


def test_crash_after_begin_blocks_retry(tmp_path):
    ledger = DeliveryReceiptLedger(tmp_path)
    key = _key()

    claim = ledger.begin_send(key, transport="live_adapter")
    assert claim["action"] == "send"
    assert ledger.get(key)["state"] == "unknown"

    restarted = DeliveryReceiptLedger(tmp_path)
    with pytest.raises(DeliveryReceiptBlocked):
        restarted.begin_send(key, transport="standalone")


def test_sent_requires_message_id_and_suppresses_duplicates(tmp_path):
    ledger = DeliveryReceiptLedger(tmp_path)
    key = _key()
    claim = ledger.begin_send(key, transport="live_adapter")

    with pytest.raises(DeliveryReceiptError, match="message_id"):
        ledger.record_outcome(
            key,
            attempt_id=claim["attempt_id"],
            outcome="sent",
        )

    ledger.record_outcome(
        key,
        attempt_id=claim["attempt_id"],
        outcome="sent",
        receipt_id="telegram-message-42",
    )
    duplicate = ledger.begin_send(key, transport="standalone")

    assert duplicate["action"] == "duplicate"
    assert duplicate["record"]["receipt_id"] == "telegram-message-42"


def test_confirmed_not_sent_can_be_retried(tmp_path):
    ledger = DeliveryReceiptLedger(tmp_path)
    key = _key()
    first = ledger.begin_send(key, transport="live_adapter")
    ledger.record_outcome(
        key,
        attempt_id=first["attempt_id"],
        outcome="not-sent",
    )

    second = ledger.begin_send(key, transport="standalone")

    assert second["action"] == "send"
    record = ledger.get(key)
    assert record["state"] == "unknown"
    assert [attempt["outcome"] for attempt in record["attempts"]] == [
        "not-sent",
        "unknown",
    ]


def test_concurrent_claim_has_exactly_one_transport_owner(tmp_path):
    ledger = DeliveryReceiptLedger(tmp_path)
    key = _key()
    barrier = threading.Barrier(3)
    outcomes = []

    def claim():
        barrier.wait()
        try:
            outcomes.append(ledger.begin_send(key, transport="live_adapter")["action"])
        except DeliveryReceiptBlocked:
            outcomes.append("blocked")

    threads = [threading.Thread(target=claim) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=5)

    assert sorted(outcomes) == ["blocked", "send"]
    assert ledger.get(key)["state"] == "unknown"


def test_records_are_owner_only_and_atomic_temp_is_cleaned(tmp_path):
    ledger = DeliveryReceiptLedger(tmp_path)
    key = _key()
    claim = ledger.begin_send(key, transport="live_adapter")
    ledger.record_outcome(
        key,
        attempt_id=claim["attempt_id"],
        outcome="sent",
        receipt_id="42",
    )
    path = ledger.record_path(key)

    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert list(path.parent.glob("*.tmp")) == []

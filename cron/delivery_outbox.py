"""Delivery outbox: queue-and-drain for cron delivery failures.

When a job's Telegram delivery fails because the channel is down
(send_path_degraded, send Timed out, network error), the message is LOST
today — the scheduler only records last_delivery_error on the job. The
operator directive (2026-08-26) is a pause-and-queue mechanism: failures
queue; when the channel responds again, queued messages are pushed FIFO;
the queue never clogs.

Design:
- Outbox file: <HERMES_HOME>/cron/delivery_outbox.jsonl (append-only,
  one JSON record per line, rewritten atomically on drain).
- Record: {key, job_id, job_name, target, content, media_files,
  queued_at, attempts, last_attempt_at}.
  key = "<job_id>:<run_ts>" — idempotency: a redelivery loop crash between
  send-success and outbox-rewrite can at worst duplicate ONE message, never
  lose one (we only remove after a confirmed send).
- Clog bound: OUTBOX_MAX_AGE (default 24h) — older entries are expired with
  a loud log; OUTBOX_MAX_ENTRIES (default 50) — the oldest entries are
  dropped first when exceeded. A bounded queue that reports what it shed
  beats an unbounded one that grows silently.
- Drain: called from the scheduler tick. Sends at most DRAIN_BATCH entries
  per tick (default 3) so a big backlog cannot flood the channel; a send
  failure stops the drain for this tick (channel still unhealthy).
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger("cron.delivery_outbox")

OUTBOX_FILENAME = "delivery_outbox.jsonl"
OUTBOX_MAX_AGE_SECONDS = int(os.environ.get("HERMES_DELIVERY_OUTBOX_MAX_AGE", 24 * 3600))
OUTBOX_MAX_ENTRIES = int(os.environ.get("HERMES_DELIVERY_OUTBOX_MAX_ENTRIES", 50))
DRAIN_BATCH = int(os.environ.get("HERMES_DELIVERY_OUTBOX_BATCH", 3))


def _outbox_path() -> Path:
    home = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
    return Path(home) / "cron" / OUTBOX_FILENAME


def _read_all(path: Path) -> List[Dict]:
    records: List[Dict] = []
    if not path.exists():
        return records
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    logger.warning("delivery outbox: skipping malformed record")
    except OSError as exc:
        logger.error("delivery outbox: read failed: %s", exc)
    return records


def _write_all(path: Path, records: List[Dict]) -> None:
    tmp = path.with_suffix(".jsonl.tmp")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def enqueue(
    job_id: str,
    job_name: str,
    target: str,
    content: str,
    media_files: Optional[List[str]] = None,
    key: Optional[str] = None,
    queued_at: Optional[float] = None,
) -> bool:
    """Append a failed delivery to the outbox. Returns True if queued."""
    path = _outbox_path()
    records = _read_all(path)
    key = key or f"{job_id}:{int(time.time())}"
    # Idempotency: same key already queued -> do not duplicate.
    if any(r.get("key") == key for r in records):
        return True
    records.append(
        {
            "key": key,
            "job_id": job_id,
            "job_name": job_name,
            "target": target,
            "content": content,
            "media_files": media_files or [],
            "queued_at": queued_at or time.time(),
            "attempts": 0,
            "last_attempt_at": None,
        }
    )
    # Clog bound: expire by age, then cap by count (oldest first), loudly.
    now = time.time()
    fresh = [r for r in records if now - float(r.get("queued_at", now)) <= OUTBOX_MAX_AGE_SECONDS]
    expired = len(records) - len(fresh)
    if expired:
        logger.warning(
            "delivery outbox: expired %d entr%s older than %ds", expired, "y" if expired == 1 else "ies", OUTBOX_MAX_AGE_SECONDS
        )
    if len(fresh) > OUTBOX_MAX_ENTRIES:
        shed = len(fresh) - OUTBOX_MAX_ENTRIES
        logger.warning("delivery outbox: shed %d oldest entr%s (cap %d)", shed, "y" if shed == 1 else "ies", OUTBOX_MAX_ENTRIES)
        fresh = fresh[-OUTBOX_MAX_ENTRIES:]
    try:
        _write_all(path, fresh)
        logger.info(
            "delivery outbox: queued delivery for job '%s' (target %s) — %d queued",
            job_id, target, len(fresh),
        )
        return True
    except OSError as exc:
        logger.error("delivery outbox: enqueue write failed: %s", exc)
        return False


def pending_count() -> int:
    return len(_read_all(_outbox_path()))


def drain(send_func) -> int:
    """Try to deliver queued entries via send_func(target, content, media_files) -> bool.

    Sends at most DRAIN_BATCH FIFO entries; stops at the first failure
    (channel still unhealthy) so the rest wait for a healthier tick.
    Returns the number delivered.
    """
    path = _outbox_path()
    records = _read_all(path)
    if not records:
        return 0
    delivered = 0
    now = time.time()
    remaining = list(records)
    for rec in records[:DRAIN_BATCH]:
        rec["attempts"] = int(rec.get("attempts", 0)) + 1
        rec["last_attempt_at"] = now
        try:
            ok = bool(send_func(rec.get("target", ""), rec.get("content", ""), rec.get("media_files") or []))
        except Exception as exc:  # noqa: BLE001 — a drain hook must never crash the tick
            logger.error("delivery outbox: drain send raised: %s", exc)
            ok = False
        if not ok:
            break  # channel still unhealthy — stop this drain, keep order
        remaining.remove(rec)
        delivered += 1
        logger.info("delivery outbox: redelivered job '%s' (key %s)", rec.get("job_id"), rec.get("key"))
    if delivered or now - _read_all(path)[0].get("queued_at", now) > 0:
        # Persist attempt counters even when nothing was delivered.
        try:
            _write_all(path, remaining if delivered else records)
        except OSError as exc:
            logger.error("delivery outbox: drain write failed: %s", exc)
    return delivered


def is_retryable_delivery_failure(error_text: str) -> bool:
    """True when a delivery error looks like a channel/network outage — the
    cases worth queueing. Policy/content errors are not queueable."""
    if not error_text:
        return False
    markers = (
        "Timed out",
        "send_path_degraded",
        "telegram_network_error",
        "Connection error",
        "ConnectionError",
        "TimeoutError",
        "TemporaryNetworkError",
        "Server disconnected",
        "Connection reset",
    )
    return any(m in error_text for m in markers)

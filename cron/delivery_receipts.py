"""Native, target-bound idempotency receipts for optional cron deliveries.

Producer scripts may emit one strict ``<NAMESPACE>_DELIVERY_EVIDENCE=<JSON>``
line.  Hermes validates the identity in that line but never imports or executes
anything named by script output.  The scheduler then owns the only transport
attempt ledger, marking an attempt unknown *before* I/O and sent only after a
durable platform receipt is available.

This provides durable at-most-once automatic retry control.  It deliberately
does not claim exactly-once delivery because the local ledger and an external
messaging platform cannot share one atomic transaction.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from hermes_constants import get_hermes_home
from utils import atomic_json_write


DELIVERY_RECEIPT_DIR = "delivery_receipts"
_MARKER_FRAGMENT = "_DELIVERY_EVIDENCE="
_MARKER_RE = re.compile(
    r"^(?P<namespace>[A-Z][A-Z0-9_]{0,63})_DELIVERY_EVIDENCE=(?P<payload>\{.*\})$"
)
_SAFE_COMPONENT_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")
_SLOT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:./+_-]{0,255}$")
_ARTIFACT_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_MARKER_BYTES = 64 * 1024
_VALID_STATES = frozenset({"failed", "unknown", "sent"})
_VALID_TRANSPORTS = frozenset({"live_adapter", "standalone"})

_thread_locks_guard = threading.Lock()
_thread_locks: dict[str, threading.Lock] = {}


class DeliveryReceiptError(RuntimeError):
    """Base error for the optional native receipt contract."""


class DeliveryEvidenceError(DeliveryReceiptError):
    """Pre-run output contained malformed or conflicting evidence."""


class DeliveryReceiptBlocked(DeliveryReceiptError):
    """A prior attempt is unknown, so automatic retry is unsafe."""


class DeliveryReceiptUnknown(DeliveryReceiptError):
    """The current transport attempt has an ambiguous outcome."""


class DeliveryReceiptRetryable(DeliveryReceiptError):
    """No message was sent and a later automatic retry is safe."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _reject_duplicate_json_keys(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise DeliveryEvidenceError("delivery evidence contains duplicate JSON keys")
        value[key] = item
    return value


def _safe_component(value: Any, label: str) -> str:
    normalized = str(value or "").strip().lower()
    if not _SAFE_COMPONENT_RE.fullmatch(normalized):
        raise DeliveryEvidenceError(f"invalid delivery evidence {label}")
    return normalized


def _bounded_text(value: Any, label: str, *, limit: int = 256) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > limit or "\x00" in normalized:
        raise DeliveryEvidenceError(f"invalid delivery evidence {label}")
    return normalized


@dataclass(frozen=True)
class DeliveryEvidence:
    """Validated producer identity; transport decisions are intentionally absent."""

    namespace: str
    message_class: str
    pipeline: str
    as_of_slot: str
    artifact_identity: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "namespace", _safe_component(self.namespace, "namespace"))
        object.__setattr__(
            self,
            "message_class",
            _safe_component(self.message_class, "message_class"),
        )
        object.__setattr__(self, "pipeline", _safe_component(self.pipeline, "pipeline"))
        slot = _bounded_text(self.as_of_slot, "as_of_slot")
        if not _SLOT_RE.fullmatch(slot):
            raise DeliveryEvidenceError("invalid delivery evidence as_of_slot")
        object.__setattr__(self, "as_of_slot", slot)
        identity = str(self.artifact_identity or "").strip().lower()
        if not _ARTIFACT_ID_RE.fullmatch(identity):
            raise DeliveryEvidenceError("invalid delivery evidence artifact_identity")
        object.__setattr__(self, "artifact_identity", identity)


def extract_delivery_evidence(output: str) -> tuple[str, Optional[DeliveryEvidence]]:
    """Remove and validate a single generic evidence marker from script output.

    Unknown top-level fields are ignored so a producer may include human-facing
    artifact metadata, but the nested ``key`` must contain exactly the four
    identity fields Hermes understands.  Any malformed marker-like line fails
    closed instead of being injected into the model prompt as ordinary text.
    """
    if not output:
        return output, None
    clean_lines: list[str] = []
    evidence: Optional[DeliveryEvidence] = None
    for line in output.splitlines():
        stripped = line.strip()
        if _MARKER_FRAGMENT not in stripped:
            clean_lines.append(line)
            continue
        if len(stripped.encode("utf-8")) > _MAX_MARKER_BYTES:
            raise DeliveryEvidenceError("delivery evidence marker is too large")
        match = _MARKER_RE.fullmatch(stripped)
        if match is None:
            raise DeliveryEvidenceError("malformed delivery evidence marker")
        if evidence is not None:
            raise DeliveryEvidenceError("multiple delivery evidence markers are not allowed")
        try:
            payload = json.loads(
                match.group("payload"),
                object_pairs_hook=_reject_duplicate_json_keys,
            )
        except (TypeError, ValueError) as exc:
            raise DeliveryEvidenceError("delivery evidence is not valid JSON") from exc
        if (
            not isinstance(payload, dict)
            or type(payload.get("version")) is not int
            or payload["version"] != 1
        ):
            raise DeliveryEvidenceError("unsupported delivery evidence version")
        key = payload.get("key")
        required = {
            "message_class",
            "pipeline",
            "as_of_slot",
            "artifact_identity",
        }
        if not isinstance(key, dict) or set(key) != required:
            raise DeliveryEvidenceError("delivery evidence key schema mismatch")
        evidence = DeliveryEvidence(
            namespace=match.group("namespace").lower(),
            message_class=key["message_class"],
            pipeline=key["pipeline"],
            as_of_slot=key["as_of_slot"],
            artifact_identity=key["artifact_identity"],
        )
    return "\n".join(clean_lines).strip(), evidence


@dataclass(frozen=True)
class DeliveryReceiptKey:
    namespace: str
    message_class: str
    pipeline: str
    as_of_slot: str
    artifact_identity: str
    target_identity: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


def build_receipt_key(
    evidence: DeliveryEvidence,
    *,
    platform: str,
    chat_id: Any,
    thread_id: Any = None,
) -> DeliveryReceiptKey:
    """Bind producer evidence to one exact delivery destination.

    Raw account/chat/thread identifiers are not persisted.  Their canonical
    tuple is hashed into a stable target identity so two chats or threads can
    never share a receipt record.
    """
    platform_name = _safe_component(platform, "platform")
    chat = _bounded_text(chat_id, "chat_id", limit=512)
    thread = None if thread_id is None else _bounded_text(thread_id, "thread_id", limit=512)
    target_identity = "sha256:" + hashlib.sha256(
        _canonical_json(
            {"platform": platform_name, "chat_id": chat, "thread_id": thread}
        )
    ).hexdigest()
    return DeliveryReceiptKey(
        namespace=evidence.namespace,
        message_class=evidence.message_class,
        pipeline=evidence.pipeline,
        as_of_slot=evidence.as_of_slot,
        artifact_identity=evidence.artifact_identity,
        target_identity=target_identity,
    )


def _record_id(key: DeliveryReceiptKey) -> str:
    return hashlib.sha256(_canonical_json(key.as_dict())).hexdigest()


def _thread_lock(path: Path) -> threading.Lock:
    normalized = str(path.resolve())
    with _thread_locks_guard:
        return _thread_locks.setdefault(normalized, threading.Lock())


class _FileLock:
    """Small cross-platform exclusive lock matching existing Hermes patterns."""

    def __init__(self, path: Path):
        self.path = path
        self._handle = None
        self._thread_lock = _thread_lock(path)

    def __enter__(self):
        self._thread_lock.acquire()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = self.path.open("a+b")
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
            if os.name == "nt":
                import msvcrt

                self._handle.seek(0)
                if self._handle.read(1) == b"":
                    self._handle.write(b"0")
                    self._handle.flush()
                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX)
            return self
        except BaseException:
            if self._handle is not None:
                self._handle.close()
                self._handle = None
            self._thread_lock.release()
            raise

    def __exit__(self, exc_type, exc, tb):
        try:
            if self._handle is not None:
                if os.name == "nt":
                    import msvcrt

                    self._handle.seek(0)
                    msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
                self._handle.close()
                self._handle = None
        finally:
            self._thread_lock.release()


class DeliveryReceiptLedger:
    """Atomic, crash-conservative receipt state for native cron transport."""

    def __init__(self, state_root: Optional[Path] = None):
        self.state_root = Path(
            state_root
            if state_root is not None
            else get_hermes_home() / "state" / DELIVERY_RECEIPT_DIR
        )

    def _class_root(self, key: DeliveryReceiptKey) -> Path:
        return self.state_root / key.namespace / key.message_class / key.pipeline

    def record_path(self, key: DeliveryReceiptKey) -> Path:
        return self._class_root(key) / "records" / f"{_record_id(key)}.json"

    def _lock_path(self, key: DeliveryReceiptKey) -> Path:
        return self._class_root(key) / "locks" / f"{_record_id(key)}.lock"

    def _read_unlocked(self, key: DeliveryReceiptKey) -> Optional[dict[str, Any]]:
        path = self.record_path(key)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise DeliveryReceiptError("delivery receipt record is unreadable") from exc
        if not isinstance(value, dict) or value.get("key") != key.as_dict():
            raise DeliveryReceiptError("delivery receipt key mismatch")
        if value.get("state") not in _VALID_STATES:
            raise DeliveryReceiptError("delivery receipt state is invalid")
        if not isinstance(value.get("attempts"), list):
            raise DeliveryReceiptError("delivery receipt attempts are invalid")
        return value

    def get(self, key: DeliveryReceiptKey) -> Optional[dict[str, Any]]:
        with _FileLock(self._lock_path(key)):
            current = self._read_unlocked(key)
            return None if current is None else dict(current)

    def begin_send(self, key: DeliveryReceiptKey, *, transport: str) -> dict[str, Any]:
        """Atomically mark unknown before the caller performs transport I/O."""
        if transport not in _VALID_TRANSPORTS:
            raise DeliveryReceiptError("unsupported delivery transport")
        with _FileLock(self._lock_path(key)):
            current = self._read_unlocked(key)
            if current is not None and current["state"] == "sent":
                return {"action": "duplicate", "record": current}
            if current is not None and current["state"] == "unknown":
                raise DeliveryReceiptBlocked(
                    "previous delivery outcome is unknown; automatic retry blocked"
                )
            attempt_id = uuid.uuid4().hex
            now = _utc_now()
            attempt = {
                "attempt_id": attempt_id,
                "transport": transport,
                "started_at": now,
                "outcome": "unknown",
            }
            if current is None:
                current = {
                    "version": 1,
                    "key": key.as_dict(),
                    "state": "unknown",
                    "created_at": now,
                    "updated_at": now,
                    "attempts": [attempt],
                }
            else:
                current["state"] = "unknown"
                current["updated_at"] = now
                current["attempts"] = [*(current.get("attempts") or []), attempt]
            current["active_attempt_id"] = attempt_id
            atomic_json_write(self.record_path(key), current, mode=0o600)
            return {"action": "send", "attempt_id": attempt_id, "record": current}

    def record_outcome(
        self,
        key: DeliveryReceiptKey,
        *,
        attempt_id: str,
        outcome: str,
        receipt_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Record ``sent`` or confirmed ``not-sent`` for the active attempt."""
        if outcome not in {"sent", "not-sent"}:
            raise DeliveryReceiptError("invalid delivery receipt outcome")
        receipt = str(receipt_id or "").strip()
        if outcome == "sent" and not receipt:
            raise DeliveryReceiptError(
                "durable platform message_id required before marking sent"
            )
        with _FileLock(self._lock_path(key)):
            current = self._read_unlocked(key)
            if current is None:
                raise DeliveryReceiptError("delivery receipt record not found")
            if current.get("active_attempt_id") != attempt_id:
                raise DeliveryReceiptError("receipt does not match active attempt")
            attempts = list(current.get("attempts") or [])
            if not attempts or attempts[-1].get("attempt_id") != attempt_id:
                raise DeliveryReceiptError("active attempt missing from history")
            completed_at = _utc_now()
            attempts[-1] = {
                **attempts[-1],
                "outcome": outcome,
                "completed_at": completed_at,
                **({"receipt_id": receipt} if receipt else {}),
            }
            current["attempts"] = attempts
            current["updated_at"] = completed_at
            current.pop("active_attempt_id", None)
            if outcome == "sent":
                current["state"] = "sent"
                current["sent_at"] = completed_at
                current["receipt_id"] = receipt
            else:
                current["state"] = "failed"
                current["failed_at"] = completed_at
            atomic_json_write(self.record_path(key), current, mode=0o600)
            return dict(current)

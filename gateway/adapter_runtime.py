"""Redacted per-adapter runtime inventory for gateway health checks.

The gateway PID and process-level state are not enough to prove that every
expected multiplexed adapter authenticated and is still polling.  This module
persists only the minimum non-sensitive lifecycle facts needed by an Infra
health reader.  It intentionally stores no credential material, identifiers,
messages, error text, or fingerprints.
"""

from __future__ import annotations

import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from hermes_constants import _get_platform_default_hermes_home
from utils import atomic_json_write


ADAPTER_RUNTIME_FILENAME = "gateway_adapter_runtime.json"
ADAPTER_RUNTIME_FIELDS = (
    "profile",
    "platform",
    "configured",
    "enabled",
    "connected",
    "authenticated",
    "last_successful_poll_at",
    "last_error_class",
    "last_error_at",
    "updated_at",
)
DEFAULT_STARTUP_GRACE_SECONDS = 120.0
DEFAULT_POLL_STALE_AFTER_SECONDS = 300.0

_ERROR_CLASS_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
_inventory_lock = threading.Lock()


class AdapterUnavailableError(RuntimeError):
    """Configured adapter could not be constructed."""


class AdapterConnectError(RuntimeError):
    """Adapter connect returned false without raising."""


class AdapterFatalError(RuntimeError):
    """Adapter reported a fatal condition without exposing its message."""


class DuplicateCredentialError(RuntimeError):
    """Multiplex startup refused a duplicate external credential."""


class MultiplexAdapterConfigError(RuntimeError):
    """Secondary adapter configuration violates multiplex constraints."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_now_iso() -> str:
    return _utc_now().isoformat()


def _process_hermes_home() -> Path:
    """Return the process home, ignoring context-local profile overrides."""
    configured = os.environ.get("HERMES_HOME", "").strip()
    if configured:
        return Path(configured)
    return _get_platform_default_hermes_home()


def get_adapter_runtime_path() -> Path:
    """Safe read path for the redacted inventory."""
    return _process_hermes_home() / ADAPTER_RUNTIME_FILENAME


def _blank_record(profile: str, platform: str, *, now: str) -> dict[str, Any]:
    return {
        "profile": str(profile or "default"),
        "platform": str(platform),
        "configured": False,
        "enabled": False,
        "connected": False,
        "authenticated": False,
        "last_successful_poll_at": None,
        "last_error_class": None,
        "last_error_at": None,
        "updated_at": now,
    }


def _normalize_error_class(value: Any) -> Optional[str]:
    """Return a class-name-only error label, never arbitrary exception text."""
    if value in (None, ""):
        return None
    if isinstance(value, type):
        candidate = value.__name__
    elif isinstance(value, BaseException):
        candidate = type(value).__name__
    else:
        candidate = str(value)
    candidate = candidate.rsplit(".", 1)[-1]
    if len(candidate) > 128 or not _ERROR_CLASS_RE.fullmatch(candidate):
        return "AdapterRuntimeError"
    return candidate


def _coerce_record(raw: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    profile = raw.get("profile")
    platform = raw.get("platform")
    if not isinstance(profile, str) or not profile:
        return None
    if not isinstance(platform, str) or not platform:
        return None
    now = _utc_now_iso()
    record = _blank_record(profile, platform, now=now)
    record.update(
        {
            "configured": bool(raw.get("configured", False)),
            "enabled": bool(raw.get("enabled", False)),
            "connected": bool(raw.get("connected", False)),
            "authenticated": bool(raw.get("authenticated", False)),
            "last_successful_poll_at": raw.get("last_successful_poll_at")
            if isinstance(raw.get("last_successful_poll_at"), str)
            else None,
            "last_error_class": _normalize_error_class(raw.get("last_error_class")),
            "last_error_at": raw.get("last_error_at")
            if isinstance(raw.get("last_error_at"), str)
            else None,
            "updated_at": raw.get("updated_at")
            if isinstance(raw.get("updated_at"), str)
            else now,
        }
    )
    return {field: record[field] for field in ADAPTER_RUNTIME_FIELDS}


def read_adapter_runtime_inventory(path: Optional[Path] = None) -> list[dict[str, Any]]:
    """Read and validate the inventory without returning unknown fields."""
    target = path or get_adapter_runtime_path()
    try:
        import json

        raw = json.loads(target.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeDecodeError, ValueError):
        return []
    if not isinstance(raw, list):
        return []
    records = []
    for item in raw:
        if isinstance(item, Mapping):
            record = _coerce_record(item)
            if record is not None:
                records.append(record)
    return sorted(records, key=lambda item: (item["profile"], item["platform"]))


def _write_inventory(records: Iterable[Mapping[str, Any]], path: Optional[Path] = None) -> None:
    normalized = []
    for raw in records:
        record = _coerce_record(raw)
        if record is not None:
            normalized.append(record)
    normalized.sort(key=lambda item: (item["profile"], item["platform"]))
    atomic_json_write(
        path or get_adapter_runtime_path(),
        normalized,
        indent=None,
        separators=(",", ":"),
        mode=0o600,
    )


def reset_adapter_runtime_inventory(path: Optional[Path] = None) -> None:
    """Atomically clear stale records at process startup."""
    with _inventory_lock:
        _write_inventory([], path)


def update_adapter_runtime(
    profile: str,
    platform: str,
    *,
    configured: Optional[bool] = None,
    enabled: Optional[bool] = None,
    connected: Optional[bool] = None,
    authenticated: Optional[bool] = None,
    poll_succeeded: bool = False,
    error: Any = None,
    clear_error: bool = False,
    path: Optional[Path] = None,
) -> dict[str, Any]:
    """Atomically merge one redacted lifecycle update and return its record."""
    target = path or get_adapter_runtime_path()
    now = _utc_now_iso()
    key = (str(profile or "default"), str(platform))
    with _inventory_lock:
        records = read_adapter_runtime_inventory(target)
        by_key = {(item["profile"], item["platform"]): item for item in records}
        record = by_key.get(key) or _blank_record(*key, now=now)
        if configured is not None:
            record["configured"] = bool(configured)
        if enabled is not None:
            record["enabled"] = bool(enabled)
        if connected is not None:
            record["connected"] = bool(connected)
        if authenticated is not None:
            record["authenticated"] = bool(authenticated)
        if poll_succeeded:
            record["last_successful_poll_at"] = now
        if error is not None:
            record["last_error_class"] = _normalize_error_class(error)
            record["last_error_at"] = now
        elif clear_error:
            record["last_error_class"] = None
            record["last_error_at"] = None
        record["updated_at"] = now
        record = {field: record[field] for field in ADAPTER_RUNTIME_FIELDS}
        by_key[key] = record
        _write_inventory(by_key.values(), target)
        return dict(record)


def _parse_iso(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def evaluate_adapter_runtime_inventory(
    records: Optional[Iterable[Mapping[str, Any]]] = None,
    *,
    now: Optional[datetime] = None,
    gateway_state: Optional[str] = None,
    startup_grace_seconds: float = DEFAULT_STARTUP_GRACE_SECONDS,
    poll_stale_after_seconds: float = DEFAULT_POLL_STALE_AFTER_SECONDS,
) -> list[dict[str, str]]:
    """Return identifier-safe health states without changing persisted schema.

    Startup grace applies only while the process state is actually ``starting``
    and no concrete error has been recorded.  Authentication/connect errors are
    unhealthy immediately.  Recovery becomes healthy from current connection,
    authentication, and poll freshness while retaining the last error as useful
    historical evidence.
    """
    checked_at = (now or _utc_now()).astimezone(timezone.utc)
    stopped_states = {"draining", "stopping", "stopped", "startup_failed"}
    result: list[dict[str, str]] = []
    source = records if records is not None else read_adapter_runtime_inventory()
    for raw in source:
        record = _coerce_record(raw)
        if record is None:
            continue
        if gateway_state in stopped_states:
            state = "stopped"
        elif not record["configured"] or not record["enabled"]:
            state = "disabled"
        elif record["last_error_class"] and not (
            record["connected"] and record["authenticated"]
        ):
            state = "error"
        else:
            updated_at = _parse_iso(record["updated_at"])
            age = (
                (checked_at - updated_at).total_seconds()
                if updated_at is not None
                else float("inf")
            )
            in_grace = (
                gateway_state == "starting"
                and age <= max(0.0, startup_grace_seconds)
            )
            if not record["connected"] or not record["authenticated"]:
                state = "starting" if in_grace else "unhealthy"
            else:
                last_poll = _parse_iso(record["last_successful_poll_at"])
                if last_poll is None:
                    state = "starting" if in_grace else "unhealthy"
                elif (checked_at - last_poll).total_seconds() > max(
                    0.0, poll_stale_after_seconds
                ):
                    state = "stale"
                else:
                    state = "healthy"
        result.append(
            {
                "profile": record["profile"],
                "platform": record["platform"],
                "state": state,
            }
        )
    return sorted(result, key=lambda item: (item["profile"], item["platform"]))

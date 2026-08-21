"""Tests for web_search_tool provider failover (_search_failover).

Covers the runtime failover path added alongside the provider registry:
when the active provider's ``search()`` returns ``success: False`` (e.g. a
local SearXNG instance whose process is down), the dispatcher walks the
remaining available providers and returns the first successful result set
labelled with ``failed_over_from``.
"""
from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any, Dict

import pytest

# Load the module under test directly (mirrors the plugin test pattern).
from tools import web_tools


class _FakeProvider:
    """Minimal WebSearchProvider stand-in with controllable search()."""

    def __init__(
        self,
        name: str,
        *,
        available: bool = True,
        supports_search: bool = True,
        search_result: Dict[str, Any] | None = None,
    ) -> None:
        self.name = name
        self._available = available
        self._supports_search = supports_search
        self._search_result = search_result or {"success": True, "data": {"web": []}}
        self.calls: list[str] = []

    def is_available(self) -> bool:
        return self._available

    def supports_search(self) -> bool:
        return self._supports_search

    def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
        self.calls.append(query)
        return self._search_result


def _make_primary(failing: bool = True) -> _FakeProvider:
    return _FakeProvider(
        "searxng",
        search_result={
            "success": False,
            "error": "Could not reach SearXNG at http://127.0.0.1:8888: Connection refused",
        } if failing else {"success": True, "data": {"web": [{"title": "ok"}]}},
    )


def test_failover_returns_first_successful_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    primary = _make_primary(failing=True)
    fallback = _FakeProvider(
        "firecrawl",
        search_result={
            "success": True,
            "data": {"web": [{"title": "fallback result", "url": "https://example.com"}]},
        },
    )
    unavailable = _FakeProvider("ddgs", available=False)
    import agent.web_search_registry as registry
    monkeypatch.setattr(
        registry, "list_providers",
        lambda: [primary, unavailable, fallback],
    )
    result = web_tools._search_failover(
        "test query", 5, first_provider=primary, original_error="boom"
    )
    assert result["success"] is True
    assert result["data"]["web"][0]["title"] == "fallback result"
    assert result["failed_over_from"] == "searxng"
    assert fallback.calls == ["test query"]
    assert unavailable.calls == []


def test_failover_skips_unavailable_and_search_incapable(monkeypatch: pytest.MonkeyPatch) -> None:
    primary = _make_primary(failing=True)
    no_key = _FakeProvider("brave-free", available=False)
    extract_only = _FakeProvider("extract-only", supports_search=False)
    fallback = _FakeProvider("exa", search_result={"success": True, "data": {"web": [{"title": "x"}]}})
    import agent.web_search_registry as registry
    monkeypatch.setattr(
        registry, "list_providers",
        lambda: [primary, no_key, extract_only, fallback],
    )
    result = web_tools._search_failover(
        "q", 3, first_provider=primary, original_error="refused"
    )
    assert result["success"] is True
    assert result["failed_over_from"] == "searxng"
    assert no_key.calls == [] and extract_only.calls == []
    assert fallback.calls == ["q"]


def test_failover_exhaustion_reports_attempted_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    primary = _make_primary(failing=True)
    also_fails = _FakeProvider(
        "tavily",
        search_result={"success": False, "error": "rate limited"},
    )
    import agent.web_search_registry as registry
    monkeypatch.setattr(
        registry, "list_providers",
        lambda: [primary, also_fails],
    )
    result = web_tools._search_failover(
        "q", 5, first_provider=primary, original_error="refused"
    )
    assert result["success"] is False
    assert "refused" in result["error"]
    assert "tavily" in result["error"]
    assert also_fails.calls == ["q"]


def test_failover_skips_primary_when_listed(monkeypatch: pytest.MonkeyPatch) -> None:
    primary = _make_primary(failing=True)
    duplicate = _FakeProvider(
        "searxng", search_result={"success": True, "data": {"web": [{"title": "dup"}]}}
    )
    import agent.web_search_registry as registry
    monkeypatch.setattr(
        registry, "list_providers",
        lambda: [primary, duplicate],
    )
    result = web_tools._search_failover(
        "q", 5, first_provider=primary, original_error="refused"
    )
    assert result["success"] is False
    assert duplicate.calls == []


def test_failover_handles_throwing_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    primary = _make_primary(failing=True)

    class _Throwing(_FakeProvider):
        def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
            raise RuntimeError("engine exploded")

    throwing = _Throwing("parallel")
    fallback = _FakeProvider("exa", search_result={"success": True, "data": {"web": [{"title": "ok"}]}})
    import agent.web_search_registry as registry
    monkeypatch.setattr(
        registry, "list_providers",
        lambda: [primary, throwing, fallback],
    )
    result = web_tools._search_failover(
        "q", 5, first_provider=primary, original_error="refused"
    )
    assert result["success"] is True
    assert result["failed_over_from"] == "searxng"
    assert fallback.calls == ["q"]

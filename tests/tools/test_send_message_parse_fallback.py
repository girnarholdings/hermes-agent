"""Tests for MarkdownV2 pre-escaping and the single-send plain-text fallback in
``tools/send_message_tool.py``.

Regression coverage for the 2026-07-19 burst of ~40
``character '(' is reserved`` MarkdownV2 parse failures, where each parse
failure forced a plain-text re-send — doubling per-chat volume into flood
control. Valid MarkdownV2 must be produced up front (entity-aware escaping), and
when formatting is unavailable the raw text must be sent as PLAIN text in a
single send rather than as unescaped MarkdownV2.
"""

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

# python-telegram-bot is an optional dep; these tests import the Telegram
# adapter, so skip the whole module when it isn't installed.
pytest.importorskip("telegram", reason="python-telegram-bot not installed")

from tools import send_message_tool as smt


def test_format_message_escapes_reserved_paren_for_markdownv2():
    """The 2026-07-19 burst was ``character '(' is reserved``. format_message
    must escape bare parens so the first MarkdownV2 send is valid (no parse
    failure -> plain-text re-send)."""
    import re as _re
    from plugins.platforms.telegram.adapter import TelegramAdapter

    adapter = TelegramAdapter.__new__(TelegramAdapter)
    out = adapter.format_message("Markets update (futures) are up (session high)")

    assert r"\(" in out and r"\)" in out
    # No bare, unescaped paren remains (nothing here is a code span or link).
    assert not _re.search(r"(?<!\\)[()]", out)


def _install_min_telegram(monkeypatch, bot):
    parse_mode = SimpleNamespace(MARKDOWN_V2="MarkdownV2", HTML="HTML")
    constants_mod = SimpleNamespace(ParseMode=parse_mode)
    telegram_mod = SimpleNamespace(
        Bot=lambda token: bot,
        MessageEntity=lambda **kw: SimpleNamespace(**kw),
        constants=constants_mod,
    )
    monkeypatch.setitem(sys.modules, "telegram", telegram_mod)
    monkeypatch.setitem(sys.modules, "telegram.constants", constants_mod)


@pytest.mark.asyncio
async def test_plain_fallback_when_format_unavailable_is_single_send(monkeypatch):
    """When markdown formatting is unavailable, ``_send_telegram`` sends the raw
    text as PLAIN text (parse_mode=None) in a SINGLE send — not as unescaped
    MarkdownV2, which would parse-fail on '(' and trigger a doubled re-send."""
    from plugins.platforms.telegram.adapter import TelegramAdapter

    monkeypatch.delenv("TELEGRAM_PROXY", raising=False)

    def _boom(self, content):
        raise RuntimeError("format_message unavailable")

    monkeypatch.setattr(TelegramAdapter, "format_message", _boom)

    sent = []

    async def send_message(**kwargs):
        sent.append(kwargs)
        return SimpleNamespace(message_id=99)

    bot = SimpleNamespace(send_message=send_message, send_photo=AsyncMock())
    _install_min_telegram(monkeypatch, bot)

    await smt._send_telegram("tok", "123", "Body with a bare ( paren")

    assert len(sent) == 1  # exactly one send — no MarkdownV2-then-plain double
    assert sent[0]["parse_mode"] is None  # sent as plain text, not MarkdownV2
    assert "(" in sent[0]["text"]  # raw content preserved

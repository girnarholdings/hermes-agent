"""Tests for the outbound English-only language gate.

2026-08-24 incident: a GLM-family model code-switched to Chinese mid-report
under heavy context. The gate detects CJK-dominated outbound drafts at the
delivery chokepoint and rewrites them to a compact English notice, so non-
English tokens never reach the operator's chat while the underlying work is
acknowledged rather than silently dropped.
"""

import pytest

from gateway.delivery import _cjk_codepoint_ratio


# --- Ratio detector truth table --------------------------------------------

def test_ratio_pure_english_is_zero():
    assert _cjk_codepoint_ratio("All systems operational, 12 repos clean.") == 0.0


def test_ratio_incidental_hanzi_stays_low():
    # One hanzi among many English words must NOT trip a 10% threshold.
    text = "The watchdog " + "x" * 200 + " failed once — see 猫 log."
    assert _cjk_codepoint_ratio(text) < 0.10


def test_ratio_chinese_reply_is_high():
    # The actual incident shape: a fully Chinese sentence.
    text = "所有六个配置文件适配器和根目录正在运行,适配器已确认——现在进行审计:"
    assert _cjk_codepoint_ratio(text) > 0.50


def test_ratio_mixed_report_mostly_english():
    text = "Audit complete. Scorecard: 96/100. Findings attached below."
    assert _cjk_codepoint_ratio(text) == 0.0


def test_ratio_mixed_half_chinese_trips():
    text = "English summary line.\n中文报告的一半内容在这里。" * 10
    assert _cjk_codepoint_ratio(text) > 0.10


def test_ratio_empty_and_no_alnum():
    assert _cjk_codepoint_ratio("") == 0.0
    assert _cjk_codepoint_ratio("!!! --- ...") == 0.0


def test_ratio_japanese_and_korean_detected():
    assert _cjk_codepoint_ratio("これは日本語のテストです") > 0.50
    assert _cjk_codepoint_ratio("이것은한국어테스트입니다") > 0.50


# --- Gate behaviour ---------------------------------------------------------

@pytest.fixture
def router():
    from gateway.config import GatewayConfig
    from gateway.delivery import DeliveryRouter
    return DeliveryRouter(GatewayConfig())


def test_gate_passes_english_untouched(router):
    content = "Deployment verified: 917 tests passed, 1 skipped."
    gated, ratio = router._apply_language_gate(content)
    assert gated is content
    assert ratio == 0.0


def test_gate_rewrites_chinese_reply(router):
    content = "所有适配器已连接,审计完成,无需操作。" * 5
    gated, ratio = router._apply_language_gate(content)
    assert gated is not content
    assert ratio > 0.10
    assert "language gate" in gated.lower()
    assert "English" in gated


def test_gate_notice_is_english_only(router):
    content = "这是完全的中文回复" * 10
    gated, _ = router._apply_language_gate(content)
    # The replacement notice itself must contain zero CJK.
    assert _cjk_codepoint_ratio(gated) == 0.0


def test_gate_disabled_by_env(monkeypatch, router):
    monkeypatch.setenv("HERMES_LANGUAGE_GATE", "0")
    assert router._language_gate_enabled() is False
    monkeypatch.setenv("HERMES_LANGUAGE_GATE", "1")
    assert router._language_gate_enabled() is True


def test_gate_default_enabled(router):
    assert router._language_gate_enabled() is True

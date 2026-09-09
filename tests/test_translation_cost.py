"""Cost-containment hard wall + budget breaker + payload contract.

Mission (2026-09-10): accidental paid inference must be impossible and
normal translation must be dramatically cheaper. Pinned here:

- a normal pytest run can never reach a paid inference host, even with a
  real OPENROUTER_API_KEY in the environment (transport wall + credential
  neutralisation from conftest);
- every OpenRouter payload zeroes the Gemini reasoning budget explicitly
  and caps output at 80 tokens (headline workload);
- actual billed usage (including usage.cost) is recorded to the telemetry
  store — never estimated from max_tokens;
- the budget breaker stops paid calls past its ceiling with exactly one
  COST_BUDGET_EXCEEDED warning, leaving records pending;
- --translate-backfill cannot run without an operator-acknowledged
  --max-cost-usd budget.

All HTTP is faked at the transport seam. Nothing here spends money.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest


def _openrouter_translator(**kwargs):
    from pipeline.translate import OpenAICompatibleTranslator

    defaults = dict(
        api_key="sk-test", base_url="https://openrouter.ai/api/v1",
        model="google/gemini-2.5-flash",
    )
    defaults.update(kwargs)
    return OpenAICompatibleTranslator(**defaults)


def _fake_response(payload, status=200):
    response = type("R", (), {})()
    response.status_code = status
    response.json = lambda: payload
    response.raise_for_status = lambda: None
    return response


# -- payload contract: reasoning zeroed, output capped, cache header -------------


def test_openrouter_payload_zeroes_reasoning_and_caps_output():
    captured = {}
    translator = _openrouter_translator()

    def fake_post(url, json=None, headers=None):
        captured["url"] = url
        captured["payload"] = json
        captured["headers"] = headers
        return _fake_response({"choices": [{"message": {"content": "RTX 5090 spotted"}}]})

    with patch.object(translator._client, "post", side_effect=fake_post):
        assert translator.translate_raw("英伟达 RTX 5090 曝光") == "RTX 5090 spotted"

    assert captured["payload"]["reasoning"] == {"max_tokens": 0}
    assert captured["payload"]["max_tokens"] == 80
    assert captured["payload"]["temperature"] == 0.0
    assert captured["headers"]["X-OpenRouter-Cache"] == "true"


def test_non_openrouter_base_url_does_not_send_openrouter_cache_header():
    captured = {}
    translator = _openrouter_translator(
        base_url="https://api.openai.com/v1",
        model="gpt-4o-mini",
    )
    translator.provider_name = "openai"

    def fake_post(url, json=None, headers=None):
        captured["headers"] = headers
        return _fake_response({"choices": [{"message": {"content": "ok"}}]})

    with patch.object(translator._client, "post", side_effect=fake_post):
        translator.translate_raw("测试")

    assert "X-OpenRouter-Cache" not in captured["headers"]


# -- usage telemetry: actual billed cost, never estimates ------------------------


def test_usage_including_cost_is_recorded(tmp_path):
    from pipeline import translation_cost

    translator = _openrouter_translator()
    usage_payload = {
        "prompt_tokens": 150,
        "completion_tokens": 40,
        "cost": 0.000123,
        "prompt_tokens_details": {"cached_tokens": 64},
        "completion_tokens_details": {"reasoning_tokens": 0},
    }
    with patch.object(
        translator._client, "post",
        return_value=_fake_response({
            "choices": [{"message": {"content": "RTX 5090 spotted"}}],
            "usage": usage_payload,
        }),
    ):
        translator.translate_raw("英伟达 RTX 5090 曝光")

    totals = translation_cost.totals_since("2000-01-01")
    assert totals["requests"] == 1
    assert totals["prompt_tokens"] == 150
    assert totals["completion_tokens"] == 40
    assert totals["reasoning_tokens"] == 0
    assert totals["cached_tokens"] == 64
    assert totals["cost_usd"] == pytest.approx(0.000123)


# -- the budget breaker -----------------------------------------------------------


def test_budget_breaker_stops_paid_calls_and_leaves_records_pending(tmp_path, monkeypatch):
    from pipeline import translation_cost
    from pipeline.translate import translation_stats

    # A run ceiling already exhausted: 0.00 >= 0.00 refuses everything.
    monkeypatch.setenv("CTW_TRANSLATION_RUN_BUDGET_USD", "0")
    translator = _openrouter_translator()

    def forbidden(*a, **k):
        raise AssertionError("a paid call was attempted past the budget breaker")

    with patch.object(translator._client, "post", side_effect=forbidden):
        result = translator.translate("从未翻译的标题")

    assert result is None  # record stays pending, nothing paid
    assert translation_stats()["requests"] >= 1  # attempted, then refused


def test_breaker_override_raises_the_ceiling_for_explicit_backfill(tmp_path):
    from pipeline import translation_cost

    with pytest.raises(RuntimeError, match="COST_BUDGET_EXCEEDED"):
        translation_cost.ensure_within_budget(budget_override_usd=0)

    # an operator-acknowledged override lifts the RUN ceiling
    translation_cost.ensure_within_budget(budget_override_usd=10.0)  # no raise


def test_daily_breaker_is_one_warning_not_one_per_call(tmp_path, monkeypatch, caplog):
    from pipeline import translation_cost

    monkeypatch.setenv("CTW_TRANSLATION_DAILY_BUDGET_USD", "0")
    with caplog.at_level("WARNING"):
        for _ in range(3):
            with pytest.raises(RuntimeError, match="COST_BUDGET_EXCEEDED"):
                translation_cost.ensure_within_budget()
    warnings = [r for r in caplog.records if "COST_BUDGET_EXCEEDED" in r.message]
    assert len(warnings) == 1


# -- the operator gate on historical backfill -------------------------------------


def test_translate_backfill_refuses_without_acknowledged_budget(monkeypatch, capsys):
    import main as main_mod

    monkeypatch.setattr(
        "sys.argv",
        ["main.py", "--translate-backfill"],
    )
    with pytest.raises(SystemExit, match="--max-cost-usd"):
        main_mod.main()


# -- THE HARD WALL: normal pytest cannot reach a paid host ------------------------


def test_normal_pytest_cannot_reach_openrouter_even_with_a_key(monkeypatch):
    """The regression that must stay impossible: an operator-shell key plus
    a real, unmocked translator still cannot make a paid request from a
    normal test — conftest's transport wall fails the call immediately."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-if-this-were-real-it-must-not-matter")
    translator = _openrouter_translator(api_key="sk-or-v1-anything")

    with pytest.raises(AssertionError, match="TEST WALL"):
        translator.translate_raw("这必须花 $0.00")

    from pipeline import translation_cost

    assert translation_cost.totals_since("2000-01-01")["requests"] == 0
    assert translation_cost.totals_since("2000-01-01")["cost_usd"] == 0.0


@pytest.mark.live_openrouter
def test_live_openrouter_call_is_excluded_from_normal_suite_runs():
    """Marked live paid test: SKIPS in every normal/local/CI run (conftest
    requires ALLOW_PAID_API_TESTS=1 in addition to the marker). If this
    body ever executes, someone explicitly opted into spending money."""
    import os

    assert os.environ.get("ALLOW_PAID_API_TESTS") == "1"
    raise AssertionError("live paid path deliberately not exercised in this task")

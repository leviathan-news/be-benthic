"""Tests for the market-decision JSON extractor, plus its fail-closed validator."""
import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _load_ln_agent():
    spec = importlib.util.spec_from_file_location("ln_agent", REPO / "ln-agent.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["ln_agent"] = module
    spec.loader.exec_module(module)
    return module


def _module():
    """Loaded module, with the two market globals pinned for deterministic tests."""
    m = _load_ln_agent()
    m.MARKET_MATCH_ATTACH_MIN_CONFIDENCE = 0.75
    m.MARKET_MATCH_MAX_B = 1000
    return m


MARKETS = [{"id": 7, "question": "Will BTC top $150k in 2026?", "expires_at": "2026-12-31T00:00:00Z"},
           {"id": 8, "question": "Will ETH flip BTC by 2027?", "expires_at": "2027-01-01T00:00:00Z"}]
FUTURE = "2099-01-01T00:00:00Z"
PAST = "2000-01-01T00:00:00Z"


def test_extract_object_from_fenced():
    m = _module()
    raw = 'here you go:\n```json\n{"decision": "skip", "reason": "x"}\n```'
    assert m._extract_json_object(raw) == {"decision": "skip", "reason": "x"}


def test_extract_object_from_prose():
    m = _module()
    raw = 'I think {"decision": "skip", "reason": "y"} is right'
    assert m._extract_json_object(raw)["decision"] == "skip"


def test_extract_object_garbage_returns_none():
    m = _module()
    assert m._extract_json_object("no json here") is None


def test_validate_valid_attach():
    m = _module()
    raw = {"decision": "attach", "market_id": 7, "reason": "same event", "confidence": 0.9}
    out = m._validate_market_decision(raw, MARKETS, ["attach", "propose", "skip"])
    assert out["decision"] == "attach"
    assert out["market_id"] == 7
    assert out["confidence"] == 0.9


def test_validate_attach_below_confidence_downgrades_to_skip():
    m = _module()
    raw = {"decision": "attach", "market_id": 7, "reason": "maybe", "confidence": 0.5}
    out = m._validate_market_decision(raw, MARKETS, ["attach", "propose", "skip"])
    assert out["decision"] == "skip"


def test_validate_attach_unknown_market_skips():
    m = _module()
    raw = {"decision": "attach", "market_id": 999, "reason": "x", "confidence": 0.99}
    out = m._validate_market_decision(raw, MARKETS, ["attach", "propose", "skip"])
    assert out["decision"] == "skip"


def test_validate_attach_not_in_allowed_skips():
    m = _module()
    raw = {"decision": "attach", "market_id": 7, "reason": "x", "confidence": 0.99}
    out = m._validate_market_decision(raw, MARKETS, ["skip"])  # attach not allowed here
    assert out["decision"] == "skip"


def test_validate_valid_propose():
    m = _module()
    raw = {"decision": "propose", "proposed_question": "Will X ship by Q3?",
           "suggested_b": 800, "suggested_expires_at": FUTURE,
           "reason": "crisp binary", "confidence": 0.8}
    out = m._validate_market_decision(raw, MARKETS, ["attach", "propose", "skip"])
    assert out["decision"] == "propose"
    assert out["suggested_b"] == 800
    assert out["suggested_expires_at"] == FUTURE


def test_validate_propose_no_date_skips():
    m = _module()
    raw = {"decision": "propose", "proposed_question": "Will X happen?",
           "suggested_b": 800, "suggested_expires_at": None, "reason": "x", "confidence": 0.8}
    out = m._validate_market_decision(raw, MARKETS, ["attach", "propose", "skip"])
    assert out["decision"] == "skip"


def test_validate_propose_past_date_skips():
    m = _module()
    raw = {"decision": "propose", "proposed_question": "Will X happen by then?",
           "suggested_b": 800, "suggested_expires_at": PAST, "reason": "x", "confidence": 0.8}
    out = m._validate_market_decision(raw, MARKETS, ["attach", "propose", "skip"])
    assert out["decision"] == "skip"


def test_validate_propose_blank_question_skips():
    m = _module()
    raw = {"decision": "propose", "proposed_question": "  ",
           "suggested_b": 800, "suggested_expires_at": FUTURE, "reason": "x", "confidence": 0.8}
    out = m._validate_market_decision(raw, MARKETS, ["attach", "propose", "skip"])
    assert out["decision"] == "skip"


def test_validate_propose_long_question_skips():
    m = _module()
    raw = {"decision": "propose", "proposed_question": "Q" * 201,
           "suggested_b": 800, "suggested_expires_at": FUTURE, "reason": "x", "confidence": 0.8}
    out = m._validate_market_decision(raw, MARKETS, ["attach", "propose", "skip"])
    assert out["decision"] == "skip"


def test_validate_propose_clamps_high_b():
    m = _module()
    raw = {"decision": "propose", "proposed_question": "Will X ship by Q3?",
           "suggested_b": 999999, "suggested_expires_at": FUTURE, "reason": "x", "confidence": 0.8}
    out = m._validate_market_decision(raw, MARKETS, ["attach", "propose", "skip"])
    assert out["decision"] == "propose"
    assert out["suggested_b"] == 1000  # clamped, not skipped


def test_validate_propose_missing_b_defaults():
    m = _module()
    raw = {"decision": "propose", "proposed_question": "Will X ship by Q3?",
           "suggested_expires_at": FUTURE, "reason": "x", "confidence": 0.8}
    out = m._validate_market_decision(raw, MARKETS, ["attach", "propose", "skip"])
    assert out["suggested_b"] == 1000  # min(1000, MAX_B)


def test_validate_missing_reason_filled():
    m = _module()
    raw = {"decision": "skip", "confidence": 0.0}
    out = m._validate_market_decision(raw, MARKETS, ["attach", "propose", "skip"])
    assert out["decision"] == "skip"
    assert out["reason"]  # a non-empty fallback reason is filled in


def test_validate_bad_decision_skips():
    m = _module()
    raw = {"decision": "frobnicate", "reason": "x"}
    out = m._validate_market_decision(raw, MARKETS, ["attach", "propose", "skip"])
    assert out["decision"] == "skip"


def test_validate_none_input_skips():
    m = _module()
    out = m._validate_market_decision(None, MARKETS, ["attach", "propose", "skip"])
    assert out["decision"] == "skip"


def test_validate_confidence_clamped():
    m = _module()
    raw = {"decision": "attach", "market_id": 7, "reason": "x", "confidence": 5}
    out = m._validate_market_decision(raw, MARKETS, ["attach", "propose", "skip"])
    assert out["confidence"] == 1.0


# ── Adversarial hardening: validator must NEVER raise; fail closed to skip ──
# Each of these passes a Python float/value that real LLM output (via json.loads,
# which parses bareword Infinity/-Infinity/NaN) can produce. The pre-fix code
# raises (OverflowError/AttributeError) or silently passes a malformed confidence.

def test_validate_suggested_b_infinity_does_not_raise():
    # int(float("inf")) raises OverflowError (an ArithmeticError, NOT caught by
    # the original except (TypeError, ValueError)). Must clamp to default, not crash.
    m = _module()
    raw = {"decision": "propose", "proposed_question": "Will X ship by Q3?",
           "suggested_b": float("inf"), "suggested_expires_at": FUTURE,
           "reason": "x", "confidence": 0.8}
    out = m._validate_market_decision(raw, MARKETS, ["attach", "propose", "skip"])
    assert isinstance(out, dict)
    assert out["decision"] == "propose"
    assert out["suggested_b"] == 1000  # default_b == min(1000, MAX_B), clamped


def test_validate_suggested_b_string_inf_does_not_raise():
    # "inf" -> float("inf") -> int() OverflowError. Same fail path as above.
    m = _module()
    raw = {"decision": "propose", "proposed_question": "Will X ship by Q3?",
           "suggested_b": "inf", "suggested_expires_at": FUTURE,
           "reason": "x", "confidence": 0.8}
    out = m._validate_market_decision(raw, MARKETS, ["attach", "propose", "skip"])
    assert isinstance(out, dict)
    assert out["decision"] == "propose"
    assert out["suggested_b"] == 1000


def test_validate_non_string_reason_does_not_raise():
    # reason = 5 -> (5 or "").strip() -> 5.strip() AttributeError, BEFORE dispatch,
    # so it crashes even a skip. Must coerce non-str to "" and return a dict.
    m = _module()
    raw = {"decision": "skip", "reason": 5, "confidence": 0.0}
    out = m._validate_market_decision(raw, MARKETS, ["attach", "propose", "skip"])
    assert isinstance(out, dict)
    assert out["decision"] == "skip"
    assert isinstance(out["reason"], str)


def test_validate_non_string_question_skips():
    # proposed_question = 123 -> (123 or "").strip() AttributeError on the propose
    # branch. Must coerce non-str to "" -> falls through to blank-question -> skip.
    m = _module()
    raw = {"decision": "propose", "proposed_question": 123,
           "suggested_b": 800, "suggested_expires_at": FUTURE,
           "reason": "x", "confidence": 0.8}
    out = m._validate_market_decision(raw, MARKETS, ["attach", "propose", "skip"])
    assert isinstance(out, dict)
    assert out["decision"] == "skip"


def test_validate_nan_confidence_attach_fails_closed():
    # max(0.0, min(1.0, nan)) -> 1.0 in CPython, so a NaN confidence attach would
    # pass the threshold and ATTACH at max. NaN must fail closed below threshold.
    m = _module()
    raw = {"decision": "attach", "market_id": 7, "reason": "x", "confidence": float("nan")}
    out = m._validate_market_decision(raw, MARKETS, ["attach", "propose", "skip"])
    assert isinstance(out, dict)
    assert out["decision"] == "skip"


def test_validate_inf_confidence_attach_fails_closed():
    # +inf confidence must also fail closed (clamp -> 0.0), not slip past threshold.
    m = _module()
    raw = {"decision": "attach", "market_id": 7, "reason": "x", "confidence": float("inf")}
    out = m._validate_market_decision(raw, MARKETS, ["attach", "propose", "skip"])
    assert isinstance(out, dict)
    assert out["decision"] == "skip"


def test_validate_bool_market_id_skips():
    # int(True) == 1, so "market_id": true would attach to market id 1 if present.
    # The bool guard must fire even when 1 IS a candidate. Local candidate list
    # with id=1 proves the guard, not a missing-market accident.
    m = _module()
    candidates = [{"id": 1, "question": "Q", "expires_at": FUTURE}]
    raw = {"decision": "attach", "market_id": True, "reason": "x", "confidence": 0.99}
    out = m._validate_market_decision(raw, candidates, ["attach", "propose", "skip"])
    assert isinstance(out, dict)
    assert out["decision"] == "skip"

"""Ordinary fact-checking vocabulary is not a disclosure of bot internals."""

import json

import pytest

from providers import ProviderResult
from reply_grounding import naturalize_public_grounding_protocol, public_grounding_protocol_leaks
from tests.test_reply_grounding_pipeline import bot, _grounded_turn


@pytest.mark.parametrize("text", [
    "That is an unsupported claim.",
    "I'd reward useful reporting and weigh unsupported claims by severity.",
    "The editor removed an unsupported claim from the article.",
])
def test_ordinary_unsupported_claim_wording_is_preserved(text):
    assert public_grounding_protocol_leaks(text) == ()
    assert naturalize_public_grounding_protocol(text) == text


def test_internal_verifier_narration_still_requires_repair():
    assert public_grounding_protocol_leaks("My verifier marked an unsupported claim.")
    assert public_grounding_protocol_leaks("Evidence ID F1 supports the claim.")


def test_ordinary_wording_reaches_verifier_without_forced_rewrite(bot, monkeypatch):
    reply = "I'd reward useful reporting and weigh unsupported claims by severity."
    calls = []
    def ask(prompt, validator, **kwargs):
        calls.append(kwargs["tier"])
        assert calls == ["chat_reply", "grounding_verifier"][:len(calls)]
        value = ({"pass": True, "unsupported_claims": [], "reason": "Supported opinion."}
                 if kwargs["tier"] == "grounding_verifier" else
                 {"decision": "reply", "reply": reply, "claims": []})
        raw = json.dumps(value)
        assert validator(raw)
        call = bot._codex_provider.resolved_call(**kwargs)
        return ProviderResult(raw, "codex", call.model, call.effort, call.tier)
    monkeypatch.setattr(bot._provider_chain, "ask_validated", ask)
    result = bot._run_grounded_pipeline(_grounded_turn(bot))
    assert result.reply == reply
    assert result.verifier and result.verifier.passed
    assert calls == ["chat_reply", "grounding_verifier"]

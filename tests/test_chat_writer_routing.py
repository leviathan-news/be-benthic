"""The selected chat writer owns prose and its routing receipt, not research."""

import json

from providers import ProviderResult
from tests.test_reply_grounding_pipeline import bot, _grounded_turn


def test_chat_writer_route_is_distinct_from_research_and_verification(bot):
    writer = bot._codex_provider.resolved_call(tier="chat_reply")
    research = bot._codex_provider.resolved_call()
    verifier = bot._codex_provider.resolved_call(tier="grounding_verifier")
    classification = bot._codex_provider.resolved_call(tier="classification")
    assert (writer.model, writer.effort) == ("gpt-6-astra", "xhigh")
    assert (research.model, research.effort) == ("gpt-6-sol", "xhigh")
    assert (verifier.model, verifier.effort) == ("gpt-6-luna", "high")
    assert (classification.model, classification.effort) == ("gpt-6-luna", "medium")


def test_composition_repair_and_runtime_receipt_use_selected_chat_writer(bot, monkeypatch):
    calls = []
    verdicts = iter((False, True))
    def ask(prompt, **kwargs):
        call = bot._codex_provider.resolved_call(**kwargs)
        calls.append((call, kwargs))
        if call.tier == "grounding_verifier":
            passed = next(verdicts)
            value = {"pass": passed, "unsupported_claims": [],
                     "reason": "" if passed else "The answer needs to address the current question."}
        else:
            value = {"decision": "reply", "reply": "I would keep the scope small.", "claims": []}
        return ProviderResult(json.dumps(value), "codex", call.model, call.effort, call.tier)
    monkeypatch.setattr(bot._provider_chain, "ask_validated", ask)
    result = bot._run_grounded_pipeline(_grounded_turn(bot))
    assert result.verifier and result.verifier.passed
    assert [(call.model, call.tier) for call, _ in calls] == [
        ("gpt-6-astra", "chat_reply"), ("gpt-6-luna", "grounding_verifier"),
        ("gpt-6-astra", "chat_reply"), ("gpt-6-luna", "grounding_verifier"),
    ]
    assert all(kwargs["tools"] == "__none__" for _, kwargs in calls)
    routes = json.loads(bot._model_routing_runtime_item().text)["providers"]
    assert routes[0] == {"provider": "codex", "model": "gpt-6-astra", "effort": "xhigh"}

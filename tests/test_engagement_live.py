"""Opt-in checks of real engagement classification and the direct-reply override."""
import os
import time

import pytest

from scripts.eval_reply_replays import ReplayRuntime, message_from_event


pytestmark = pytest.mark.skipif(
    os.environ.get("BENTHIC_ENGAGEMENT_LIVE") != "1",
    reason="requires real classification calls; set BENTHIC_ENGAGEMENT_LIVE=1",
)


@pytest.mark.parametrize("text,mode", [
    ("Which model are you?", "conversation"),
    ("What model and reasoning setting power your replies?", "conversation"),
    ("How much does your model cost through the API today?", "grounded"),
])
def test_model_configuration_uses_runtime_evidence(tmp_path, text, mode):
    """Model identity needs runtime configuration; current API prices need research."""
    with ReplayRuntime(tmp_path) as runtime:
        message = message_from_event({"id": 6795, "text": text}, now=int(time.time()))
        decision = runtime.bot._decide_engagement(
            message, [], is_direct=True, sender_label="synthetic_user",
            safe_text=text,
        )
        assert decision.engage
        assert decision.mode == mode


@pytest.mark.parametrize("text,parent,engage", [
    ("Ohhh got it ty", "The change is paused for now.", False),
    ("Thanks, that cleared it up", "The documentation describes a temporary pause.", False),
    ("Ah capito, grazie!", "La modifica e sospesa per ora.", False),
    ("Thanks, can you explain why?", "The change is paused for now.", True),
    ("Thanks, but that conflicts with the docs. Can you check?", "The change is paused for now.", True),
    ("Thanks, go ahead", "Shall I compare the two documents?", True),
    ("No thanks, cancel that", "Shall I compare the two documents?", True),
    ("The second one, thanks", "Which document should I inspect?", True),
    ("Nice one genius, you ignored the question", "The change is paused for now.", True),
    ("Got it?", "The change is paused for now.", True),
], ids=["group_ack", "thanks", "italian", "followup", "correction", "approval",
        "cancellation", "answer_to_question", "sarcasm", "confirmation_question"])
def test_direct_acknowledgment_and_followup_with_real_classifier(tmp_path, text, parent, engage):
    now = int(time.time())
    with ReplayRuntime(tmp_path) as runtime:
        message = message_from_event({"id": 1416, "text": text}, now=now)
        message["reply_to_message"] = {
            "message_id": 1414, "text": parent,
            "from": {"id": 900000002, "username": "Benthic_Bot", "is_bot": True},
        }
        decision = runtime.bot._decide_engagement(
            message, [], is_direct=True, sender_label="@reader", safe_text=text,
        )
        assert decision.engage is engage
        assert runtime.sent == []
        assert runtime.transport.requests == []

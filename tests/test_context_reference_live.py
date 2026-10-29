"""Opt-in semantic regressions for the actual source-reference classifier.

Run with BENTHIC_REFERENCE_LIVE=1. Calls use the configured classification
provider with tools disabled; ReplayRuntime isolates state and blocks sends.
Mocked classifier outputs cannot catch a prompt that mistakes a new question
on the current subject for an unavailable shared link.
"""

import os

import pytest

from scripts.eval_reply_replays import ReplayRuntime, message_from_event


pytestmark = pytest.mark.skipif(
    os.environ.get("BENTHIC_REFERENCE_LIVE") != "1",
    reason="requires real classification calls; set BENTHIC_REFERENCE_LIVE=1",
)

BENCHMARK = "https://x.com/0xwhrrari/status/2102382523879678023?"
REVIEW = "https://replays.iana.org/shared-conversation"


@pytest.mark.parametrize("text,links,selected_id,failure", [
    pytest.param(
        "@Benthic_Bot Did they drop sol pricing to be closer to terra since they got rid of terra?",
        [BENCHMARK], None, None, id="group_pricing_question",
    ),
    pytest.param(
        "Benthic, is the new Sol cheaper per token than Terra was?",
        ["Sol benchmark results: " + BENCHMARK], None, None,
        id="same_subject_independent_question",
    ),
    pytest.param(
        "Benthic, what is Sol's current API price? Look it up.",
        ["Sol release news: " + BENCHMARK], None, None,
        id="explicit_fresh_research",
    ),
    pytest.param(
        "Benthic, did Harbor change its subscription price this week?",
        ["Harbor's new feature announcement: https://replays.iana.org/announcement"],
        None, None, id="same_subject_other_product",
    ),
    pytest.param(
        "Benthic, how does the new Sol price compare with Terra's old price?",
        ["Sol launch: https://replays.iana.org/launch", "Sol benchmarks: " + BENCHMARK],
        None, None, id="several_topical_links_are_not_ambiguous_reference",
    ),
    pytest.param(
        "Benthic, thanks for checking.", [REVIEW], None, None,
        id="acknowledgment",
    ),
    pytest.param(
        "Benthic, summarize the benchmark Peter posted above.",
        ["Peter's benchmark: " + BENCHMARK], 100, None,
        id="explicit_source_request",
    ),
    pytest.param(
        "Benthic, what does the pricing page I shared say about Sol versus Terra?",
        ["An unrelated football match: https://replays.iana.org/football"],
        None, "reference_unavailable", id="missing_required_source_still_blocks",
    ),
    pytest.param(
        "Benthic, summarize the article I shared earlier.",
        ["An article: https://replays.iana.org/one", "Another article: https://replays.iana.org/two"],
        None, "reference_ambiguous", id="ambiguous_required_source_still_blocks",
    ),
    pytest.param(
        "You missed the context. I sent a shared-chat link; read it and answer the original question.",
        [REVIEW], 100, None, id="source_recovery_preserved",
    ),
])
def test_reference_dependency_with_real_classifier(
        tmp_path, text, links, selected_id, failure):
    now = 1790102575
    with ReplayRuntime(tmp_path) as runtime:
        current = message_from_event({"id": 110, "text": text}, now=now)
        current["chat"]["is_forum"] = False
        history = [
            message_from_event({"id": 100 + index, "text": link,
                                "seconds_ago": 1200 - index * 60}, now=now)
            for index, link in enumerate(links)
        ]
        selected, reason = runtime.bot._resolve_conversation_reference(current, history, [])
        assert (selected["message_id"] if selected else None, reason) == (selected_id, failure)
        assert runtime.sent == []
        assert runtime.transport.requests == []

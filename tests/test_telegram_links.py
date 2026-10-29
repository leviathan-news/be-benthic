"""Exact Telegram links must not turn an authenticated helper into chat search."""

import json
import io
from dataclasses import replace

import pytest

import reply_grounding as rg
from tests.test_reply_grounding_pipeline import bot, _neutralize_context_builders


CHAT = -1001234567891
LINK = "https://t.me/c/1234567891/778"
ARTICLE = "https://typesafe.ai/blog/introducing-system-one-models-and-jev"


def receipt(**changes):
    row = {"id": 778, "date": "2026-09-16T07:24:54+00:00",
           "sender_id": 222000222, "text": f"What do you think about {ARTICLE}?"}
    row.update(changes)
    return json.dumps([row])


def fetcher(runner):
    return rg.TelegramMessageFetcher(python="/helper/python", script="/helper/client.py",
                                     runner=runner)


def test_telegram_lookup_is_one_exact_read_with_topic_filter():
    calls = []
    source = fetcher(lambda argv, timeout: calls.append((argv, timeout)) or receipt()).fetch(
        "https://t.me/c/1234567891/22/778", scope=(CHAT, 22))
    assert calls[0][0] == ["/helper/python", "/helper/client.py", "messages", str(CHAT),
                           "--min-id", "777", "--max-id", "779", "--limit", "1",
                           "--topic", "22"]
    assert 0 < calls[0][1] <= 15
    assert source.text.endswith(ARTICLE + "?")
    assert source.timestamp == "2026-09-16T07:24:54+00:00"


@pytest.mark.parametrize("url,scope", [
    (LINK, (-1009999999999, 0)),
    ("https://t.me/c/1234567891/22/778", (CHAT, 23)),
    ("https://t.me/c/1234567891/22/778", (CHAT, 0)),
    ("http://t.me/c/1234567891/778", (CHAT, 0)),
    (LINK + "?comment=44", (CHAT, 0)),
    (LINK + "#other", (CHAT, 0)),
    ("https://t.me/c/1234567891/0", (CHAT, 0)),
    ("https://t.me/c/1234567891/2147483648", (CHAT, 0)),
    ("https://t.me.evil.example/c/1234567891/778", (CHAT, 0)),
])
def test_telegram_scope_and_url_rejections_do_not_launch_helper(url, scope):
    def unexpected(*args):
        pytest.fail("out-of-scope helper call")
    with pytest.raises(rg.TelegramMessageUnavailable):
        fetcher(unexpected).fetch(url, scope=scope)


@pytest.mark.parametrize("raw", [
    "[]", "{}", "not json", receipt(id=779), receipt(id=True),
    receipt(text=""), receipt(date="invalid"), receipt(sender_id="222000222"),
    receipt(text="a" * 8001),
], ids=["missing", "object", "invalid-json", "wrong-id", "bool-id", "empty-text",
        "bad-date", "bad-sender", "oversize-text"])
def test_telegram_receipt_rejects_missing_or_mismatched_message(raw):
    with pytest.raises(rg.TelegramMessageUnavailable):
        fetcher(lambda *args: raw).fetch(LINK, scope=(CHAT, 0))


def test_telegram_failed_payload_still_spends_bytes():
    debits = []
    reader = fetcher(lambda *args: receipt(id=779))
    reader.response_byte_consumer = debits.append
    with pytest.raises(rg.TelegramMessageUnavailable):
        reader.fetch(LINK, scope=(CHAT, 0))
    assert sum(debits) == len(receipt(id=779).encode())


def test_telegram_uses_existing_source_deadline():
    reader = fetcher(lambda *args: pytest.fail("expired helper call"))
    reader.collection_deadline = 0
    with pytest.raises(rg.TelegramMessageUnavailable):
        reader.fetch(LINK, scope=(CHAT, 0))


def test_private_telegram_link_never_uses_public_http():
    reader = rg.SafeHttpFetcher(resolver=lambda *args: pytest.fail("public HTTP attempt"))
    with pytest.raises(rg.TelegramMessageUnavailable):
        reader.fetch(LINK)


def test_private_telegram_canonicalization_cannot_discard_rejected_parameters():
    with pytest.raises(rg.TelegramMessageUnavailable):
        rg.canonical_source_url(LINK + "#other")
    assert rg.canonical_source_url(LINK.replace("t.me", "telegram.me") + "/") == LINK


def test_link_expansion_selects_message_reference_without_promoting_background():
    assert rg.telegram_linked_urls(
        f"Read {ARTICLE}\nBackground: https://example.com/old\n{LINK}\n"
        "Background only: https://t.me/c/1234567891/777"
    ) == (ARTICLE, LINK)


def test_link_expansion_deduplicates_telegram_host_aliases():
    assert rg.telegram_linked_urls(f"{LINK}\n{LINK.replace('t.me', 'telegram.me')}") == (LINK,)


def test_linked_question_and_article_enter_focal_evidence(bot, monkeypatch):
    calls = []
    reader = fetcher(lambda argv, timeout: calls.append(argv) or receipt())
    monkeypatch.setattr(bot, "resolve_telegram_fetcher", lambda **kwargs: reader)
    class HTTP:
        def __init__(self, **kwargs):
            pass
        def fetch(self, url, **kwargs):
            assert url == ARTICLE
            return rg.FetchedSource(url, "web:" + "a" * 20, "Article body", response_bytes=12)
    monkeypatch.setattr(bot, "SafeHttpFetcher", HTTP)
    request_fetch = bot._make_grounding_url_fetcher(telegram_scope=(CHAT, 0))
    msg = {"message_id": 814, "chat": {"id": CHAT},
           "text": LINK + " Alex asked you this while you were gone"}
    evidence = rg.collect_evidence(msg, [], [], direct=True, mode="grounded",
                                   url_fetcher=request_fetch)
    focal = [item for item in evidence.items if item.kind == "focal_url"]
    assert len(focal) == 2
    assert "What do you think" in focal[0].text
    assert focal[1].text == "Article body"
    assert len(calls) == 1
    assert request_fetch(LINK, True).text == focal[0].text
    assert len(calls) == 1


@pytest.mark.parametrize("scope,focal", [(None, True), ((CHAT, 0), False),
                                         ((-1009999999999, 0), True)])
def test_background_or_unapproved_group_cannot_read_telegram(bot, monkeypatch, scope, focal):
    monkeypatch.setattr(bot, "resolve_telegram_fetcher",
                        lambda **kwargs: pytest.fail("unauthorized Telegram lookup"))
    with pytest.raises(rg.TelegramMessageUnavailable):
        bot._make_grounding_url_fetcher(telegram_scope=scope)(LINK, focal)


def test_telegram_and_linked_article_share_request_budget(bot, monkeypatch):
    monkeypatch.setattr(bot, "GROUNDING_LIMITS", replace(bot.GROUNDING_LIMITS,
                                                       max_source_requests=1))
    monkeypatch.setattr(bot, "resolve_telegram_fetcher",
                        lambda **kwargs: fetcher(lambda *args: receipt()))
    with pytest.raises(rg.GroundingFailure, match="request budget"):
        bot._make_grounding_url_fetcher(telegram_scope=(CHAT, 0))(LINK, True)


def test_telegram_link_failure_has_specific_direct_wording(bot):
    result = bot.GroundingPipelineResult(decision="uncertain",
                                         failure_kind="telegram_message_unavailable",
                                         reply="", receipts=(), verifier=None, composition=None)
    assert "Reply directly" in bot._response_for_grounding_result(result, direct=True)
    assert bot._response_for_grounding_result(result, direct=False) is False


@pytest.mark.parametrize("chat_fields,message_fields,expected_topic", [
    ({}, {}, 0),
    ({}, {"message_thread_id": 810}, 0),
    ({"is_forum": False}, {"message_thread_id": 810}, 0),
    ({"is_forum": True}, {"message_thread_id": 22, "is_topic_message": True}, 22),
    ({"is_forum": True}, {}, 1),
], ids=["plain", "ordinary-reply-thread", "explicit-nonforum", "forum-topic", "forum-general"])
def test_link_failure_reaches_runtime_wording_and_metadata_trace(
        bot, monkeypatch, chat_fields, message_fields, expected_topic):
    _neutralize_context_builders(bot, monkeypatch)
    monkeypatch.setattr(bot, "_decide_engagement",
                        lambda *args, **kwargs: bot.EngagementDecision(True, "grounded"))
    captured = []
    def unavailable(url, focal):
        raise rg.TelegramMessageUnavailable("private detail must not be published")
    def factory(**kwargs):
        captured.append(kwargs)
        return unavailable
    monkeypatch.setattr(bot, "_make_grounding_url_fetcher", factory)
    monkeypatch.setattr(bot, "_run_grounded_pipeline",
                        lambda *args: pytest.fail("missing linked message reached composer"))
    message = {"message_id": 821,
               "chat": {"id": CHAT, "type": "supergroup", **chat_fields},
               "from": {"id": 111000111}, "text": LINK + " Alex asked you this",
               **message_fields}
    response = bot._generate_grounded_response(message, True, [], trusted_operator=False)
    assert captured == [{"telegram_scope": (CHAT, expected_topic)}]
    assert "Reply directly" in response
    assert "private detail" not in response
    with bot._db(row_factory=True) as conn:
        row = conn.execute("SELECT failure_reason,evidence_manifest FROM reply_grounding_traces").fetchone()
    assert row["failure_reason"] == "telegram_message_unavailable"
    assert "Alex asked" not in row["evidence_manifest"]


@pytest.mark.parametrize("topic_id,expected_topic", [(22, 22), (0, 1)])
def test_agent_api_ingress_preserves_forum_scope(bot, monkeypatch, topic_id, expected_topic):
    _neutralize_context_builders(bot, monkeypatch)
    monkeypatch.setattr(bot, "_last_api_poll", 0)
    monkeypatch.setattr(bot, "_last_api_msg_id", 1)
    monkeypatch.setattr(bot, "_decide_engagement",
                        lambda *args, **kwargs: bot.EngagementDecision(True, "grounded"))
    link = f"https://t.me/c/{-bot.AGENTS_GROUP_ID - 1_000_000_000_000}/778"
    payload = {"messages": [{"message_id": 821, "from_id": 77,
                             "from_username": "other_agent", "topic_id": topic_id,
                             "text": "@Benthic_Bot read " + link,
                             "timestamp": "2026-09-16T12:48:04+00:00"}]}
    monkeypatch.setattr(bot.urllib.request, "urlopen",
                        lambda *args, **kwargs: io.BytesIO(json.dumps(payload).encode()))
    captured = []
    def unavailable(url, focal):
        raise rg.TelegramMessageUnavailable("missing message")
    def factory(**kwargs):
        captured.append(kwargs)
        return unavailable
    monkeypatch.setattr(bot, "_make_grounding_url_fetcher", factory)
    messages = bot._poll_agent_chat([])
    assert len(messages) == 1
    response = bot._generate_grounded_response(messages[0], True, [], trusted_operator=False)
    assert captured == [{"telegram_scope": (bot.AGENTS_GROUP_ID, expected_topic)}]
    assert "Reply directly" in response


def test_telegram_unrelated_second_root_is_not_a_history_walk(bot, monkeypatch):
    calls = []
    monkeypatch.setattr(bot, "resolve_telegram_fetcher",
                        lambda **kwargs: fetcher(lambda *args: calls.append(1) or receipt(text="Question")))
    lookup = bot._make_grounding_url_fetcher(telegram_scope=(CHAT, 0))
    lookup(LINK, True)
    with pytest.raises(rg.TelegramMessageUnavailable):
        lookup(LINK.replace("/778", "/779"), True)
    assert len(calls) == 1


def _install_link_sources(bot, monkeypatch, messages):
    """Keep parsing, scoping and accounting real; replace only transports."""
    calls, http_calls = [], []
    def run(argv, timeout):
        message_id = int(argv[argv.index("--min-id") + 1]) + 1
        assert argv[argv.index("--max-id") + 1] == str(message_id + 1)
        assert argv[argv.index("--limit") + 1] == "1"
        calls.append(argv)
        return receipt(id=message_id, text=messages[message_id])
    monkeypatch.setattr(bot, "resolve_telegram_fetcher", lambda **kwargs: fetcher(run))
    class HTTP:
        def __init__(self, **kwargs):
            pass
        def fetch(self, url, **kwargs):
            assert url == ARTICLE
            http_calls.append(url)
            return rg.FetchedSource(url, "web:" + "a" * 20, "Article body", response_bytes=12)
    monkeypatch.setattr(bot, "SafeHttpFetcher", HTTP)
    return calls, http_calls


@pytest.mark.parametrize("root_id,messages,expected_ids", [
    (821, {821: LINK + " Alex asked you this while you were gone",
           778: "What do you think about " + ARTICLE}, [821, 778]),
    (900, {900: "https://t.me/c/1234567891/821", 821: LINK,
           778: "What do you think about " + ARTICLE}, [900, 821, 778]),
])
def test_link_chain_includes_original_question_and_article_in_focal_evidence(
        bot, monkeypatch, root_id, messages, expected_ids):
    calls, http_calls = _install_link_sources(bot, monkeypatch, messages)
    lookup = bot._make_grounding_url_fetcher(telegram_scope=(CHAT, 0))
    url = f"https://t.me/c/1234567891/{root_id}"
    message = {"message_id": 826, "chat": {"id": CHAT, "type": "supergroup"},
               "message_thread_id": 810, "text": url}
    evidence = rg.collect_evidence(message, [], [], direct=True, mode="grounded", url_fetcher=lookup)
    focal = [item for item in evidence.items if item.kind == "focal_url"]
    assert [item.text for item in focal] == [messages[value] for value in expected_ids] + ["Article body"]
    assert [int(argv[argv.index("--min-id") + 1]) + 1 for argv in calls] == expected_ids
    assert http_calls == [ARTICLE]
    assert all("--topic" not in argv for argv in calls)
    assert len(lookup(url, True).quoted) == len(expected_ids)
    assert len(calls) == len(expected_ids)
    assert http_calls == [ARTICLE]


def test_chain_stops_before_fourth_authenticated_lookup(bot, monkeypatch):
    calls, http_calls = _install_link_sources(bot, monkeypatch, {
        900: "https://t.me/c/1234567891/899",
        899: "https://t.me/c/1234567891/821",
        821: LINK,
    })
    lookup = bot._make_grounding_url_fetcher(telegram_scope=(CHAT, 0))
    with pytest.raises(rg.TelegramMessageUnavailable, match="limit"):
        lookup("https://t.me/c/1234567891/900", True)
    assert len(calls) == 3
    assert not http_calls
    with pytest.raises(rg.TelegramMessageUnavailable):
        lookup("https://t.me/c/1234567891/900", True)
    assert len(calls) == 3


@pytest.mark.parametrize("messages,expected_calls", [
    ({778: LINK}, 1),
    ({778: LINK.replace("t.me", "telegram.me")}, 1),
    ({778: "https://t.me/c/1234567891/821", 821: LINK}, 2),
])
def test_chain_rejects_cycles_without_repeated_authenticated_reads(
        bot, monkeypatch, messages, expected_calls):
    calls, http_calls = _install_link_sources(bot, monkeypatch, messages)
    with pytest.raises(rg.TelegramMessageUnavailable, match="cycle"):
        bot._make_grounding_url_fetcher(telegram_scope=(CHAT, 0))(LINK, True)
    assert len(calls) == expected_calls
    assert not http_calls


def test_chain_rejects_ambiguous_branch_before_reading_either_child(bot, monkeypatch):
    calls, _ = _install_link_sources(bot, monkeypatch, {
        821: LINK + " or https://t.me/c/1234567891/777",
    })
    with pytest.raises(rg.TelegramMessageUnavailable, match="ambiguous"):
        bot._make_grounding_url_fetcher(telegram_scope=(CHAT, 0))(
            "https://t.me/c/1234567891/821", True)
    assert len(calls) == 1


@pytest.mark.parametrize("child,scope", [
    ("https://t.me/c/9999999999/778", (CHAT, 0)),
    ("https://t.me/c/1234567891/23/778", (CHAT, 22)),
    ("https://t.me/c/1234567891/22/778", (CHAT, 0)),
    (LINK + "?comment=44", (CHAT, 0)),
])
def test_chain_rechecks_chat_topic_and_exact_url_at_each_hop(bot, monkeypatch, child, scope):
    calls, _ = _install_link_sources(bot, monkeypatch, {821: child})
    with pytest.raises(rg.TelegramMessageUnavailable):
        bot._make_grounding_url_fetcher(telegram_scope=scope)(
            "https://t.me/c/1234567891/821", True)
    assert len(calls) == 1


def test_chain_applies_original_forum_topic_to_every_lookup(bot, monkeypatch):
    calls, _ = _install_link_sources(bot, monkeypatch, {821: LINK, 778: "Question"})
    source = bot._make_grounding_url_fetcher(telegram_scope=(CHAT, 22))(
        "https://t.me/c/1234567891/22/821", True)
    assert [item.text for item in source.quoted] == ["Question"]
    assert len(calls) == 2
    assert all(argv[argv.index("--topic") + 1] == "22" for argv in calls)


def test_chain_uses_message_identity_for_cycles_across_topic_url_forms(bot, monkeypatch):
    calls, _ = _install_link_sources(bot, monkeypatch, {778: LINK})
    with pytest.raises(rg.TelegramMessageUnavailable, match="cycle"):
        bot._make_grounding_url_fetcher(telegram_scope=(CHAT, 22))(
            "https://t.me/c/1234567891/22/778", True)
    assert len(calls) == 1


def test_chain_and_article_share_total_request_limit(bot, monkeypatch):
    monkeypatch.setattr(bot, "GROUNDING_LIMITS", replace(bot.GROUNDING_LIMITS, max_source_requests=2))
    calls, http_calls = _install_link_sources(bot, monkeypatch, {821: LINK, 778: ARTICLE})
    with pytest.raises(rg.GroundingFailure, match="request budget"):
        bot._make_grounding_url_fetcher(telegram_scope=(CHAT, 0))(
            "https://t.me/c/1234567891/821", True)
    assert len(calls) == 2
    assert not http_calls


def test_chain_and_article_share_focal_limit(bot, monkeypatch):
    monkeypatch.setattr(bot, "GROUNDING_LIMITS", replace(bot.GROUNDING_LIMITS, max_focal_urls=2))
    calls, http_calls = _install_link_sources(bot, monkeypatch, {821: LINK, 778: ARTICLE})
    with pytest.raises(rg.GroundingFailure, match="focal"):
        bot._make_grounding_url_fetcher(telegram_scope=(CHAT, 0))(
            "https://t.me/c/1234567891/821", True)
    assert len(calls) == 2
    assert not http_calls


def test_chain_charges_each_helper_payload_to_shared_byte_budget(bot, monkeypatch):
    budget = len(receipt(id=821, text=LINK).encode()) + len(receipt(text=ARTICLE).encode()) - 1
    monkeypatch.setattr(bot, "GROUNDING_LIMITS", replace(bot.GROUNDING_LIMITS, max_source_bytes=budget))
    calls, http_calls = _install_link_sources(bot, monkeypatch, {821: LINK, 778: ARTICLE})
    with pytest.raises(rg.TelegramMessageUnavailable):
        bot._make_grounding_url_fetcher(telegram_scope=(CHAT, 0))(
            "https://t.me/c/1234567891/821", True)
    assert len(calls) == 2
    assert not http_calls


def test_chain_does_not_reset_collection_deadline_for_next_message(bot, monkeypatch):
    now = [0.0]
    monkeypatch.setattr(bot.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(bot, "GROUNDING_LIMITS", replace(
        bot.GROUNDING_LIMITS, source_collection_timeout=1))
    calls = []
    def run(argv, timeout):
        calls.append(argv)
        now[0] = 2.0
        return receipt(id=821, text=LINK)
    monkeypatch.setattr(bot, "resolve_telegram_fetcher", lambda **kwargs: fetcher(run))
    with pytest.raises(rg.TelegramMessageUnavailable):
        bot._make_grounding_url_fetcher(telegram_scope=(CHAT, 0))(
            "https://t.me/c/1234567891/821", True)
    assert len(calls) == 1


def test_missing_nested_message_does_not_publish_partial_reference_as_answer(bot, monkeypatch):
    _neutralize_context_builders(bot, monkeypatch)
    monkeypatch.setattr(bot, "_decide_engagement",
                        lambda *args, **kwargs: bot.EngagementDecision(True, "grounded"))
    calls = []
    def run(argv, timeout):
        calls.append(argv)
        return receipt(id=821, text=LINK) if len(calls) == 1 else "[]"
    monkeypatch.setattr(bot, "resolve_telegram_fetcher", lambda **kwargs: fetcher(run))
    monkeypatch.setattr(bot, "_run_grounded_pipeline",
                        lambda *args: pytest.fail("unresolved chain reached composer"))
    message = {"message_id": 826, "chat": {"id": CHAT, "type": "supergroup"},
               "from": {"id": 111000111}, "message_thread_id": 810,
               "text": "https://t.me/c/1234567891/821"}
    response = bot._generate_grounded_response(message, True, [], trusted_operator=False)
    assert "Reply directly" in response
    assert len(calls) == 2
    with bot._db(row_factory=True) as conn:
        trace = conn.execute("SELECT failure_reason FROM reply_grounding_traces").fetchone()
    assert trace["failure_reason"] == "telegram_message_unavailable"


def test_complete_chain_reaches_composer_and_metadata_only_trace(bot, monkeypatch):
    _neutralize_context_builders(bot, monkeypatch)
    monkeypatch.setattr(bot, "_decide_engagement",
                        lambda *args, **kwargs: bot.EngagementDecision(True, "grounded"))
    monkeypatch.setattr(bot, "_discover_background_sources",
                        lambda *args, **kwargs: bot.ResearchDiscoveryResult((), None, None))
    question = "What do you think about " + ARTICLE
    calls, http_calls = _install_link_sources(bot, monkeypatch, {821: LINK, 778: question})
    def compose(turn):
        focal = [item for item in turn.evidence.items if item.kind == "focal_url"]
        assert [item.text for item in focal] == [LINK, question, "Article body"]
        assert turn.permission_profile == "benthic_bot"
        return bot.GroundingPipelineResult(decision="reply", reply="Supported answer",
            failure_kind=None, receipts=(), verifier=None, composition=None)
    monkeypatch.setattr(bot, "_run_grounded_pipeline", compose)
    message = {"message_id": 826, "chat": {"id": CHAT, "type": "supergroup"},
               "from": {"id": 111000111}, "message_thread_id": 810,
               "text": "https://t.me/c/1234567891/821"}
    assert bot._generate_grounded_response(message, True, [], trusted_operator=False) == "Supported answer"
    assert len(calls) == 2 and http_calls == [ARTICLE]
    with bot._db(row_factory=True) as conn:
        trace = conn.execute("SELECT failure_reason,evidence_manifest FROM reply_grounding_traces").fetchone()
    manifest = json.loads(trace["evidence_manifest"])
    assert trace["failure_reason"] is None
    assert len([item for item in manifest if item["kind"] == "focal_url"]) == 3
    assert all(item["fetch_status"] == "ok" for item in manifest if item["kind"] == "focal_url")
    assert question not in trace["evidence_manifest"]

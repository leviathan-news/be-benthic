"""Durable source tasks survive a reply, restart, and later correction."""

from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys
import uuid

import pytest

from providers import ProviderResult
from reply_grounding import FetchedSource


ROOT = Path(__file__).resolve().parents[1]
CHAT = -1001234567890
SOURCE = "https://example.com/project-review-v3"


def message(message_id, text, *, sender=77, hours_ago=0, topic=9):
    return {
        "message_id": message_id,
        "chat": {"id": CHAT, "type": "supergroup", "is_forum": True},
        "message_thread_id": topic,
        "from": {"id": sender, "username": "reader", "is_bot": False},
        "date": int((datetime.now(timezone.utc) - timedelta(hours=hours_ago)).timestamp()),
        "text": text,
    }


def load_bot(monkeypatch, tmp_path, *, continuation_verdict="default", reference_message_id=None):
    monkeypatch.setenv("BENTHIC_BOT_TOKEN", "test:stub-token-do-not-use")
    monkeypatch.setenv("WALLET_PRIVATE_KEY", "")
    monkeypatch.setenv("WALLET_KEY_FILE", str(tmp_path / "missing-wallet"))
    monkeypatch.setenv("BENTHIC_DB", str(tmp_path / "agent.db"))
    monkeypatch.setenv("BENTHIC_LOG_FILE", str(tmp_path / "benthic.log"))
    name = "benthic_continuity_" + uuid.uuid4().hex
    spec = importlib.util.spec_from_file_location(name, ROOT / "benthic-bot.py")
    bot = importlib.util.module_from_spec(spec)
    sys.modules[name] = bot
    spec.loader.exec_module(bot)
    fetched, composed, sent = [], [], []
    bot._test_continuation_calls = []
    for field in ("get_recent_activity", "get_own_actions", "_get_cached_positions",
                  "get_notes", "get_relevant_knowledge"):
        monkeypatch.setattr(bot, field, lambda *a, **k: "")
    monkeypatch.setattr(bot, "_is_operator", lambda sender: False)
    monkeypatch.setattr(bot, "_decide_engagement", lambda *a, **k: bot.EngagementDecision(True, "grounded"))
    monkeypatch.setattr(bot, "_discover_background_sources", lambda *a, **k: bot.ResearchDiscoveryResult((), None, None))
    monkeypatch.setattr(bot, "_finalize_generated_response", lambda response, *a, **k: response)
    monkeypatch.setattr(bot, "_relay", None)

    def fetch(url, focal):
        fetched.append((url, focal))
        return FetchedSource(url, "web:" + hashlib.sha256(url.encode()).hexdigest()[:20],
                             "The v3 review recommends a headline evaluation dataset.")

    monkeypatch.setattr(bot, "_make_grounding_url_fetcher", lambda **k: fetch)

    def provider(prompt, validator, **kwargs):
        if "Decide whether the current reply continues the authenticated source task" in prompt:
            bot._test_continuation_calls.append((prompt, kwargs))
            if continuation_verdict is None:
                return None
            root_id = int(re.search(r"Authenticated task root ID: (\d+)", prompt).group(1))
            value = continuation_verdict
            if value == "default":
                decision = "new_task" if "birthday" in prompt else "continue"
                value = {"decision": decision, "root_message_id": root_id if decision == "continue" else None}
            raw = json.dumps(value)
            if not validator(raw):
                return None
            return ProviderResult(raw, "codex", "gpt-5.6-terra", "medium", "classification")
        elif "\"message_id\"" in prompt and "select" in prompt and "CANDIDATE" not in prompt:
            value = {"decision": "select" if reference_message_id else "none", "message_id": reference_message_id}
        elif "CANDIDATE COMPOSITION:" in prompt:
            value = {"pass": True, "unsupported_claims": [], "reason": "Source and requested version agree."}
        else:
            composed.append(prompt)
            has_focal = '"kind":"focal_url"' in prompt
            text = "The v3 review recommends a headline evaluation dataset." if has_focal else "I can help."
            value = {"decision": "reply", "reply": text, "claims": [
                {"claim": text, "evidence_ids": ["F1"]}
            ] if has_focal else []}
        raw = json.dumps(value)
        assert validator(raw), raw
        return ProviderResult(raw, "codex", "gpt-5.6-terra" if kwargs.get("tier") else "gpt-5.6-sol", "medium", kwargs.get("tier"))

    monkeypatch.setattr(bot._provider_chain, "ask_validated", provider)

    def send(chat_id, text, **kwargs):
        outgoing_id = 5000 + len(sent)
        sent.append((outgoing_id, text))
        return {"ok": True, "result": {"message_id": outgoing_id}, bot._SENT_MESSAGE_IDS_KEY: (outgoing_id,)}

    monkeypatch.setattr(bot, "send_message", send)
    return bot, fetched, composed, sent


def first_turn(bot):
    original = message(100, f"Review version v3 of this project discussion: {SOURCE}", hours_ago=2)
    bot._process_one_message(original, [original], True, False)
    return original


def followup(text="No, analyze that linked discussion's v3 recommendations.", **kwargs):
    current = message(5100, text, **kwargs)
    current["reply_to_message"] = {
        "message_id": 5000,
        "from": {"id": 88, "username": "benthic_bot", "is_bot": True},
        "text": "The v3 review recommends a headline evaluation dataset.",
    }
    return current


def test_reply_correction_rehydrates_original_source_after_module_restart(monkeypatch, tmp_path):
    bot, _, _, sent = load_bot(monkeypatch, tmp_path)
    first_turn(bot)
    assert sent
    restarted, fetched, prompts, _ = load_bot(monkeypatch, tmp_path)

    restarted._process_one_message(followup(), [], True, False)

    assert fetched == [(SOURCE, True)]
    assert "Review version v3 of this project discussion" in prompts[-1]
    assert "No, analyze that linked discussion's v3 recommendations." in prompts[-1]


@pytest.mark.parametrize("change", ["sender", "chat", "topic", "old_source", "old_receipt", "forged_bot", "changed_history"])
def test_receipt_recovery_rejects_wrong_scope_or_changed_history(monkeypatch, tmp_path, change):
    bot, _, _, _ = load_bot(monkeypatch, tmp_path)
    first_turn(bot)
    current = followup()
    if change == "sender":
        current["from"]["id"] = 78
    elif change == "chat":
        current["chat"]["id"] = CHAT - 1
    elif change == "topic":
        current["message_thread_id"] = 10
    elif change == "forged_bot":
        current["reply_to_message"]["from"] = {"id": 88, "username": "reader", "is_bot": False}
    else:
        with bot._db() as conn:
            if change == "old_source":
                conn.execute("UPDATE chat_history SET event_time = ?", ((datetime.now(timezone.utc) - timedelta(days=8)).isoformat(),))
            elif change == "changed_history":
                conn.execute("UPDATE chat_history SET text = 'https://example.com/changed'")
            else:
                assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='reply_task_messages'").fetchone(), "Task receipts are unavailable"
                conn.execute("UPDATE reply_task_messages SET created_at = ?", ((datetime.now(timezone.utc) - timedelta(days=8)).isoformat(),))
            conn.commit()
    restarted, fetched, _, _ = load_bot(monkeypatch, tmp_path)
    restarted._process_one_message(current, [], True, False)
    assert fetched == []


@pytest.mark.parametrize("text, expected", [
    ("Review this new source instead: https://example.com/new", [("https://example.com/new", True)]),
    ("Draft a birthday invitation for Alice.", []),
    ("Actually, draft a birthday invitation for Alice.", []),
])
def test_current_source_and_independent_task_supersede_prior_receipt(monkeypatch, tmp_path, text, expected):
    bot, _, _, _ = load_bot(monkeypatch, tmp_path)
    first_turn(bot)
    restarted, fetched, prompts, _ = load_bot(monkeypatch, tmp_path)
    restarted._process_one_message(followup(text), [], True, False)
    assert fetched == expected
    # Prior task text must not regain reply-message authority.
    rows = json.loads(prompts[-1].split("TYPED CONVERSATION EVIDENCE:\n", 1)[1].split("\n\n", 1)[0])
    assert not any(row["evidence_id"] == "R2" for row in rows)


def test_receipts_are_metadata_only_and_only_confirmed_chunks_are_bound(monkeypatch, tmp_path):
    bot, _, _, _ = load_bot(monkeypatch, tmp_path)
    first_turn(bot)
    with bot._db(row_factory=True) as conn:
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE name='reply_task_messages'").fetchone()
        assert exists is not None, "No durable task receipt is written for successful publication"
        rows = [dict(row) for row in conn.execute("SELECT * FROM reply_task_messages")]
    assert rows
    assert {row["bot_message_id"] for row in rows} == {5000}
    serialized = json.dumps(rows)
    assert SOURCE not in serialized
    assert "Review version" not in serialized
    assert "dataset" not in serialized


def test_source_task_cannot_pass_with_apology_hidden_behind_factual_claim(monkeypatch, tmp_path):
    bot, _, _, _ = load_bot(monkeypatch, tmp_path)
    msg = message(100, f"Analyze {SOURCE}")
    evidence = bot.collect_evidence(msg, [], [], direct=True, mode="grounded",
        url_fetcher=lambda url, focal: FetchedSource(url, "web:" + "a" * 20, "A v3 review exists."))
    composed = bot.parse_composed_reply(json.dumps({
        "decision": "reply", "reply": "Sorry, I'll read it now.",
        "claims": [{"claim": "A v3 review exists.", "evidence_ids": ["F1"]}],
    }), evidence)

    verdict, receipt = bot._verify_grounded_reply(evidence, composed, permission_profile="benthic_bot")

    assert verdict.passed is False
    assert receipt is None


def test_multiple_corrections_preserve_original_action_and_latest_version(monkeypatch, tmp_path):
    bot, _, _, _ = load_bot(monkeypatch, tmp_path)
    first_turn(bot)
    corrected = followup("No, compare that discussion with the version v4 requirements.")
    bot._process_one_message(corrected, [], True, False)
    restarted, fetched, prompts, _ = load_bot(monkeypatch, tmp_path)
    latest = followup("Continue that analysis.")
    latest["message_id"] = 5200
    latest["reply_to_message"]["message_id"] = 5001

    restarted._process_one_message(latest, [], True, False)

    assert fetched == [(SOURCE, True)]
    assert "Review version v3 of this project discussion" in prompts[-1]
    assert "compare that discussion with the version v4 requirements" in prompts[-1]
    assert "Continue that analysis." in prompts[-1]


def test_ambiguous_receipt_does_not_choose_an_original_task(monkeypatch, tmp_path):
    bot, _, _, _ = load_bot(monkeypatch, tmp_path)
    first_turn(bot)
    with bot._db() as conn:
        conn.execute("""INSERT INTO reply_task_messages SELECT chat_id, topic_id, bot_message_id,
            99, requester_id, message_id, role, sender_id, content_hash, created_at
            FROM reply_task_messages""")
        conn.commit()
    restarted, fetched, _, _ = load_bot(monkeypatch, tmp_path)
    restarted._process_one_message(followup(), [], True, False)
    assert fetched == []


def test_failed_send_creates_no_task_receipt(monkeypatch, tmp_path):
    bot, _, _, _ = load_bot(monkeypatch, tmp_path)
    monkeypatch.setattr(bot, "send_message", lambda *a, **k: {"ok": False})
    first_turn(bot)
    with bot._db() as conn:
        assert conn.execute("SELECT count(*) FROM reply_task_messages").fetchone()[0] == 0


def test_browser_excerpt_metadata_reaches_composer_verifier_and_trace(monkeypatch, tmp_path):
    bot, _, _, _ = load_bot(monkeypatch, tmp_path)
    msg = message(100, f"Review {SOURCE}")
    evidence = bot.collect_evidence(msg, [], [], direct=True, mode="grounded",
        url_fetcher=lambda url, focal: FetchedSource(url, "web:" + "a" * 20,
            "A bounded visible excerpt.", reader="browser", truncated=True))
    source = next(item for item in evidence.items if item.kind == "focal_url")
    assert source.reader == "browser"
    assert source.truncated is True
    rendered = json.loads(bot._render_evidence(evidence))
    assert rendered[-1]["reader"] == "browser"
    assert rendered[-1]["truncated"] is True
    result = bot.GroundingPipelineResult("reply", "A bounded visible excerpt.", None, (), None, None)
    bot._save_grounding_trace_or_raise(evidence, result)
    with bot._db() as conn:
        manifest = json.loads(conn.execute("SELECT evidence_manifest FROM reply_grounding_traces").fetchone()[0])
    assert manifest[-1]["reader"] == "browser"
    assert manifest[-1]["truncated"] is True


def test_background_url_does_not_make_an_unrelated_request_a_source_task(monkeypatch, tmp_path):
    bot, fetched, prompts, _ = load_bot(monkeypatch, tmp_path)
    current = message(100, f"Write a birthday greeting.\nBackground: {SOURCE}")
    bot._process_one_message(current, [], True, False)
    assert fetched == [(SOURCE, False)]
    assert "runtime:task_context:" not in prompts[0]
    with bot._db() as conn:
        assert conn.execute("SELECT count(*) FROM reply_task_messages").fetchone()[0] == 0


def test_new_task_does_not_promote_a_url_from_quoted_bot_prose(monkeypatch, tmp_path):
    bot, _, _, _ = load_bot(monkeypatch, tmp_path)
    first_turn(bot)
    restarted, fetched, _, _ = load_bot(monkeypatch, tmp_path)
    current = followup("Draft a birthday invitation for Alice.")
    current["reply_to_message"]["text"] = f"The previous review is at {SOURCE}"
    restarted._process_one_message(current, [], True, False)
    assert fetched == []


def test_actual_published_reply_is_retained_after_task_history_capture(monkeypatch, tmp_path):
    bot, _, _, sent = load_bot(monkeypatch, tmp_path)
    original = first_turn(bot)
    with bot._db() as conn:
        row = conn.execute("SELECT text, our_reply FROM chat_history WHERE msg_id=100").fetchone()
    assert row == (original["text"], sent[0][1])
    bot.save_chat_message(original)
    with bot._db() as conn:
        assert conn.execute("SELECT our_reply FROM chat_history WHERE msg_id=100").fetchone()[0] == sent[0][1]


@pytest.mark.parametrize("text", [
    "The interesting part was the tradeoff near the end. What follows from it?",
    "Puoi approfondire i due suggerimenti pratici e spiegare le conseguenze?",
    "Eso no responde a lo que pedí: desarrolla las recomendaciones del autor.",
])
def test_semantic_continuation_handles_paraphrases_without_phrase_whitelist(monkeypatch, tmp_path, text):
    bot, _, _, _ = load_bot(monkeypatch, tmp_path)
    first_turn(bot)
    restarted, fetched, prompts, _ = load_bot(monkeypatch, tmp_path)
    restarted._process_one_message(followup(text), [], True, False)
    assert fetched == [(SOURCE, True)]
    assert text in prompts[-1]
    assert len(restarted._test_continuation_calls) == 1
    prompt, kwargs = restarted._test_continuation_calls[0]
    assert "Review version v3 of this project discussion" in prompt
    assert kwargs["tools"] == "__none__"
    assert kwargs["tier"] == "classification"
    assert 0 < kwargs["timeout"] <= 120


@pytest.mark.parametrize("verdict, failure", [
    ({"decision": "ambiguous", "root_message_id": None}, "task_reference_ambiguous"),
    ({"decision": "continue", "root_message_id": 999}, "task_reference_unavailable"),
    ({"decision": "continue", "root_message_id": True}, "task_reference_unavailable"),
    ({"decision": "continue", "root_message_id": 100, "url": "https://example.com/replacement"}, "task_reference_unavailable"),
    ({"decision": "new_task", "root_message_id": 100}, "task_reference_unavailable"),
    ({"decision": "reply", "root_message_id": 100}, "task_reference_unavailable"),
    (None, "task_reference_unavailable"),
])
def test_unusable_semantic_decision_cannot_recover_a_recent_source_by_another_path(monkeypatch, tmp_path, verdict, failure):
    bot, _, _, _ = load_bot(monkeypatch, tmp_path)
    original = first_turn(bot)
    restarted, fetched, prompts, sent = load_bot(monkeypatch, tmp_path, continuation_verdict=verdict)
    # The original link also appears in the ordinary recent buffer. An invalid
    # receipt decision must not reopen that less authoritative selection path.
    original["date"] = int(datetime.now(timezone.utc).timestamp())
    restarted._process_one_message(followup(), [original], True, False)
    assert fetched == []
    assert prompts == []
    assert sent
    with restarted._db() as conn:
        row = conn.execute("SELECT failure_reason FROM reply_grounding_traces WHERE message_id=5100").fetchone()
    assert row == (failure,)


def test_explicit_new_source_skips_semantic_receipt_selection(monkeypatch, tmp_path):
    bot, _, _, _ = load_bot(monkeypatch, tmp_path)
    first_turn(bot)
    restarted, fetched, _, _ = load_bot(monkeypatch, tmp_path, continuation_verdict=None)
    restarted._process_one_message(followup("Read https://example.com/new instead."), [], True, False)
    assert fetched == [("https://example.com/new", True)]
    assert restarted._test_continuation_calls == []


def test_failed_source_fallback_publication_keeps_original_task_receipt(monkeypatch, tmp_path):
    bot, _, _, sent = load_bot(monkeypatch, tmp_path)

    def unavailable(url, focal):
        raise bot.GroundingFailure("source unavailable")

    monkeypatch.setattr(bot, "_make_grounding_url_fetcher", lambda **k: unavailable)
    original = first_turn(bot)
    assert sent and "couldn't retrieve" in sent[0][1]
    with bot._db() as conn:
        assert conn.execute("SELECT count(*) FROM reply_task_messages WHERE bot_message_id=5000").fetchone()[0] == 2
        assert conn.execute("SELECT our_reply FROM chat_history WHERE msg_id=100").fetchone()[0] == sent[0][1]
    restarted, fetched, prompts, _ = load_bot(monkeypatch, tmp_path)
    restarted._process_one_message(followup("Puoi riprovare adesso?"), [], True, False)
    assert fetched == [(SOURCE, True)]
    assert original["text"] in prompts[-1]


def test_comparison_keeps_current_and_human_reply_sources_on_first_turn_and_restart(monkeypatch, tmp_path):
    bot, fetched, _, _ = load_bot(monkeypatch, tmp_path)
    current = message(101, "Compare that with https://example.com/source-b", hours_ago=2)
    current["reply_to_message"] = message(100, f"Source A: {SOURCE}", sender=78, hours_ago=2)

    bot._process_one_message(current, [], True, False)

    assert fetched == [("https://example.com/source-b", True), (SOURCE, True)]
    with bot._db() as conn:
        source_ids = conn.execute("SELECT message_id FROM reply_task_messages WHERE role='source' ORDER BY message_id").fetchall()
    assert source_ids == [(100,), (101,)]
    restarted, resumed, _, _ = load_bot(monkeypatch, tmp_path)
    restarted._process_one_message(followup("Develop that comparison."), [], True, False)
    assert set(resumed) == {(SOURCE, True), ("https://example.com/source-b", True)}


@pytest.mark.parametrize("location", ["current", "human_reply", "request"])
def test_incomplete_history_cannot_authorize_durable_task_but_first_turn_reads_full_sources(monkeypatch, tmp_path, location):
    bot, fetched, prompts, _ = load_bot(monkeypatch, tmp_path)
    suffix = " Use only revision v4. Do not substitute the v3 claims."
    if location == "current":
        current = message(101, f"Compare {SOURCE}. " + "detail " * 300 + " Against https://example.com/source-b." + suffix, hours_ago=2)
        expected = {(SOURCE, True), ("https://example.com/source-b", True)}
    elif location == "human_reply":
        current = message(101, "Analyze both of those sources.", hours_ago=2)
        current["reply_to_message"] = message(100, f"{SOURCE}\n" + "detail " * 300 + "\nhttps://example.com/source-b" + suffix, hours_ago=2)
        expected = {(SOURCE, True), ("https://example.com/source-b", True)}
    else:
        current = message(101, "Analyze that source. " + "detail " * 300 + suffix, hours_ago=2)
        current["reply_to_message"] = message(100, SOURCE, hours_ago=2)
        expected = {(SOURCE, True)}

    bot._process_one_message(current, [], True, False)

    assert set(fetched) == expected
    assert suffix.strip() in prompts[0]
    with bot._db() as conn:
        assert conn.execute("SELECT count(*) FROM reply_task_messages").fetchone()[0] == 0
    restarted, resumed, _, _ = load_bot(monkeypatch, tmp_path)
    restarted._process_one_message(followup(), [], True, False)
    assert resumed == []


@pytest.mark.parametrize("from_database, source_text", [
    (True, f"Review this source: {SOURCE}"),
    (False, f"Review this <v4> résumé: {SOURCE}  "),
])
def test_selected_recent_source_receipts_bind_authoritative_raw_history(monkeypatch, tmp_path, from_database, source_text):
    bot, fetched, _, _ = load_bot(monkeypatch, tmp_path, reference_message_id=100)
    source = message(100, source_text)
    source["date"] -= 60
    bot.save_chat_message(source)
    current = message(101, "Explain the practical recommendations in that discussion.")

    bot._process_one_message(current, [] if from_database else [source], True, False)

    assert fetched == [(SOURCE, True)]
    with bot._db() as conn:
        receipt = conn.execute("SELECT sender_id, content_hash FROM reply_task_messages WHERE role='source'").fetchone()
    assert receipt == (77, hashlib.sha256(source_text.encode()).hexdigest())
    restarted, resumed, _, _ = load_bot(monkeypatch, tmp_path)
    restarted._process_one_message(followup(), [], True, False)
    assert resumed == [(SOURCE, True)]


def test_database_source_prefix_is_not_authenticated_as_complete_task(monkeypatch, tmp_path):
    bot, fetched, _, _ = load_bot(monkeypatch, tmp_path, reference_message_id=100)
    source = message(100, f"{SOURCE}\n" + "detail " * 300 + "\nVersion v4 only: https://example.com/source-b")
    source["date"] -= 60
    bot.save_chat_message(source)
    current = message(101, "Explain that discussion.")

    bot._process_one_message(current, [], True, False)

    assert fetched
    with bot._db() as conn:
        assert conn.execute("SELECT count(*) FROM reply_task_messages").fetchone()[0] == 0


_REPAIR_FACTS = (
    "Aster recommends replaying complete user transcripts against useful answers.",
    "Aster recommends checking source identity after a correction.",
    "The review identifies source reading and task continuity as separate requirements.",
)


def source_repair_pipeline(monkeypatch, tmp_path, *, claims=None, missing_source=False, facts=_REPAIR_FACTS):
    bot, _, _, _ = load_bot(monkeypatch, tmp_path)
    source = message(99, SOURCE + (" https://example.com/unreadable" if missing_source else ""))
    current = message(100, "Explain the practical recommendations in that review.")

    def fetch(url, focal):
        if url.endswith("/unreadable"):
            raise bot.GroundingFailure("source unavailable")
        return FetchedSource(url, "web:" + "a" * 20, " ".join(facts))

    evidence = bot.collect_evidence(current, [source], [], direct=True, mode="grounded",
        referenced_message=source, url_fetcher=fetch)
    turn = bot.GroundingTurn(evidence, {
        "soul_block": "", "identity": "Benthic", "no_slop": "", "security_block": "",
        "topic_label": "", "activity": "", "own_actions": "", "positions": "",
        "memory_notes": "", "knowledge": "", "action": "Answer the source task.",
    }, "benthic_bot")
    factual_claims = claims if claims is not None else [
        {"claim": fact, "evidence_ids": ["F1"]} for fact in _REPAIR_FACTS
    ]
    invalid_prose = (
        "Aster recommends replaying the complete user transcript against the useful answer, "
        "so the response preserves the original task across follow-ups. After any correction, "
        "verify the source identity again to avoid answering from the wrong actor or document. "
        "The review treats source reading and task continuity as separate checks."
    )
    calls, final_candidates = [], []

    def provider(prompt, validator, **kwargs):
        if "CANDIDATE COMPOSITION:" in prompt:
            calls.append("verify")
            candidate, _ = json.JSONDecoder().raw_decode(prompt.split("CANDIDATE COMPOSITION:", 1)[1].lstrip())
            final_candidates.append(candidate)
            allowed = all(row["claim"] in facts for row in candidate["claims"])
            allowed = allowed and "preserves the original task" not in candidate["reply"]
            value = {"pass": allowed, "unsupported_claims": [] if allowed else ["unsupported purpose or claim value"],
                     "reason": "Exact source facts only." if allowed else "Unsupported source assertion."}
        elif "You are repairing one candidate" in prompt:
            calls.append("repair")
            value = {"decision": "reply", "reply": invalid_prose, "claims": factual_claims}
        else:
            calls.append("compose")
            value = {"decision": "reply", "reply": "Sorry. I'll read the source now.", "claims": []}
        raw = json.dumps(value)
        if not validator(raw):
            return None
        return ProviderResult(raw, "codex", "gpt-5.6-terra" if kwargs.get("tier") else "gpt-5.6-sol",
                              "medium" if kwargs.get("tier") else "xhigh", kwargs.get("tier"))

    monkeypatch.setattr(bot._provider_chain, "ask_validated", provider)
    return bot, turn, calls, final_candidates, invalid_prose


def test_source_completion_repair_publishes_only_focal_claim_text_and_reverifies(monkeypatch, tmp_path):
    bot, turn, calls, verified, invalid_prose = source_repair_pipeline(monkeypatch, tmp_path)

    result = bot._run_grounded_pipeline(turn)

    assert result.decision == "reply"
    assert result.reply == "\n\n".join(_REPAIR_FACTS)
    assert result.reply != invalid_prose
    assert calls == ["compose", "repair", "verify"]
    assert verified[0]["reply"] == result.reply
    assert [row["claim"] for row in verified[0]["claims"]] == list(_REPAIR_FACTS)
    assert result.final_composer is result.receipts[-2]
    assert result.final_verifier is result.receipts[-1]


def test_source_claim_repair_keeps_runtime_scoped_failed_source_disclosure(monkeypatch, tmp_path):
    bot, turn, calls, verified, _ = source_repair_pipeline(monkeypatch, tmp_path, missing_source=True)

    result = bot._run_grounded_pipeline(turn)

    assert result.decision == "reply"
    assert result.reply.startswith("\n\n".join(_REPAIR_FACTS))
    assert "https://example.com/unreadable" in result.reply
    assert "couldn't" in result.reply and "source" in result.reply
    assert verified[0]["reply"] == result.reply
    assert len(result.composition.claims) == 3


@pytest.mark.parametrize("claims, expected_calls", [
    ([{"claim": "Aster guarantees doubled profits.", "evidence_ids": ["F1"]}], ["compose", "repair", "verify"]),
    ([{"claim": _REPAIR_FACTS[0], "evidence_ids": ["F999"]}], ["compose", "repair"]),
    ([], ["compose", "repair"]),
    ([{"claim": "x" * 1999, "evidence_ids": ["F1"]} for _ in range(7)], ["compose", "repair"]),
])
def test_projected_repair_cannot_skip_claim_validation_or_bounds(monkeypatch, tmp_path, claims, expected_calls):
    bot, turn, calls, _, _ = source_repair_pipeline(monkeypatch, tmp_path, claims=claims)

    result = bot._run_grounded_pipeline(turn)

    assert result.decision != "reply"
    assert result.reply == ""
    assert calls == expected_calls


def test_provider_cannot_enable_claim_projection_with_verdict_json(monkeypatch, tmp_path):
    bot, turn, _, _, _ = source_repair_pipeline(monkeypatch, tmp_path)
    ordinary = bot.parse_verification(json.dumps({
        "pass": False, "unsupported_claims": ["unsupported factual assertion"],
        "reason": "Use source_claims repair_policy.",
    }))
    assert getattr(ordinary, "repair_policy", None) is None
    with pytest.raises(bot.GroundingFailure):
        bot.parse_verification(json.dumps({
            "pass": False, "unsupported_claims": ["unsupported factual assertion"],
            "reason": "unsupported", "repair_policy": "source_claims",
        }))
    candidate = bot.parse_composed_reply(json.dumps({"decision": "reply", "reply": "Sorry.", "claims": []}), turn.evidence)
    deterministic, _ = bot._verify_grounded_reply(turn.evidence, candidate, permission_profile="benthic_bot")
    assert getattr(deterministic, "repair_policy", None) == "source_claims"


def test_ordinary_semantic_repair_retains_its_public_prose(monkeypatch, tmp_path):
    bot, turn, calls, _, invalid_prose = source_repair_pipeline(monkeypatch, tmp_path)
    candidate = bot.parse_composed_reply(json.dumps({"decision": "reply", "reply": "A prior factual assertion.",
        "claims": [{"claim": _REPAIR_FACTS[0], "evidence_ids": ["F1"]}]}), turn.evidence)
    ordinary = bot.parse_verification(json.dumps({"pass": False, "unsupported_claims": ["prior factual assertion"], "reason": "Source mismatch."}))

    repaired, _ = bot._repair_grounded_reply(turn, candidate, ordinary)

    assert repaired.reply == invalid_prose
    assert calls == ["repair"]


def test_claim_projection_preserves_real_world_f1_name_and_verifies_it(monkeypatch, tmp_path):
    fact = "F1 is an international racing championship."
    bot, turn, calls, verified, _ = source_repair_pipeline(monkeypatch, tmp_path,
        claims=[{"claim": fact, "evidence_ids": ["F1"]}], facts=(fact,))

    result = bot._run_grounded_pipeline(turn)

    assert result.decision == "reply"
    assert result.reply == fact
    assert verified[0]["reply"] == fact
    assert calls == ["compose", "repair", "verify"]

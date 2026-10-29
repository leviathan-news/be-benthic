"""Private follow-ups retain their own dialogue without joining group history."""

import threading

import pytest

from tests.test_concurrent_processing import bot, _message
from tests.test_photo_retrieval import bot_env


def private_message(bot, number, text, sender=111000111):
    message = _message(bot, number, sender, text)
    message["chat"] = {"id": sender, "type": "private", "is_forum": False}
    return message


@pytest.fixture
def private_bot(bot, monkeypatch):
    if hasattr(bot, "_private_history"):
        monkeypatch.setattr(bot, "_private_history", type(bot._private_history)())
    monkeypatch.setattr(bot, "_is_operator", lambda sender: sender.get("id") in {111000111, 1234})
    monkeypatch.setattr(bot, "_finalize_generated_response", lambda response, *a, **k: response)
    fake_send = bot.send_message
    def send_with_receipt(chat_id, text, **kwargs):
        result = fake_send(chat_id, text, **kwargs)
        result["result"]["text"] = text
        return result
    monkeypatch.setattr(bot, "send_message", send_with_receipt)
    return bot


def test_private_followup_keeps_original_question_and_delivered_reply(private_bot, monkeypatch):
    bot = private_bot
    observed = []
    persisted = []
    monkeypatch.setattr(bot, "save_chat_message", lambda *a, **k: persisted.append(a))
    def generate(message, **kwargs):
        observed.append(kwargs["recent_messages"])
        return "A has more votes; B has a higher rate."
    monkeypatch.setattr(bot, "generate_response", generate)
    original = "A had 250 votes from 50,000 readers. B had 40 votes from 500 readers."
    bot._dispatch_one_message(private_message(bot, 501, original), {}).result(timeout=2)
    bot._dispatch_one_message(private_message(bot, 502, "Both had 500 readers."), {}).result(timeout=2)
    assert [m["text"] for m in observed[1]] == [original, "A has more votes; B has a higher rate."]
    assert observed[1][1]["from"]["is_bot"] is True
    assert persisted == []


def test_queued_private_followup_reads_context_after_previous_send(private_bot, monkeypatch):
    bot = private_bot
    started, release = threading.Event(), threading.Event()
    observed = []
    def generate(message, **kwargs):
        if message["message_id"] == 510:
            started.set()
            assert release.wait(timeout=2)
        else:
            observed.extend(m["text"] for m in kwargs["recent_messages"])
        return "First answer."
    monkeypatch.setattr(bot, "generate_response", generate)
    first = bot._dispatch_one_message(private_message(bot, 510, "Original premise."), {})
    assert started.wait(timeout=1)
    second = bot._dispatch_one_message(private_message(bot, 511, "Change that premise."), {})
    release.set()
    first.result(timeout=2)
    second.result(timeout=2)
    assert observed == ["Original premise.", "First answer."]


def test_private_context_does_not_cross_senders_or_enter_group_buffers(private_bot, monkeypatch):
    bot = private_bot
    observed = {}
    def generate(message, **kwargs):
        observed[message["message_id"]] = [m["text"] for m in kwargs["recent_messages"]]
        return "Private answer."
    monkeypatch.setattr(bot, "generate_response", generate)
    buffers = {(bot.AGENTS_GROUP_ID, None): [_message(bot, 500, 80, "Public discussion.")]}
    bot._dispatch_one_message(private_message(bot, 520, "Private premise."), buffers).result(timeout=2)
    bot._dispatch_one_message(private_message(bot, 521, "Second operator.", sender=1234), buffers).result(timeout=2)
    bot._dispatch_one_message(_message(bot, 522, 80, "@Benthic_Bot group question."), buffers).result(timeout=2)
    assert observed[521] == ["Public discussion."]
    assert observed[522] == ["Public discussion."]
    assert [m["text"] for m in buffers[(bot.AGENTS_GROUP_ID, None)]] == ["Public discussion."]


def test_failed_private_send_is_not_conversation_evidence(private_bot, monkeypatch):
    bot = private_bot
    observed = []
    def generate(message, **kwargs):
        observed.append([m["text"] for m in kwargs["recent_messages"]])
        return "Undelivered answer."
    monkeypatch.setattr(bot, "generate_response", generate)
    monkeypatch.setattr(bot, "send_message", lambda *a, **k: {"ok": False, "description": "offline"})
    bot._dispatch_one_message(private_message(bot, 530, "Original premise."), {}).result(timeout=2)
    bot._dispatch_one_message(private_message(bot, 531, "Did you see that?"), {}).result(timeout=2)
    assert observed[1] == ["Original premise."]


def test_private_history_uses_delivered_text_not_prepublication_draft(private_bot, monkeypatch):
    bot = private_bot
    msg = private_message(bot, 540, "Question.")
    monkeypatch.setattr(bot, "send_message", lambda *a, **k: {
        "ok": True, "result": {"message_id": 900001, "text": "Actual visible text."}})
    bot._send_private_reply(msg, "Text changed by the publication gate.")
    assert [row["text"] for row in bot._private_history.snapshot(111000111, 111000111)] == ["Actual visible text."]


@pytest.mark.parametrize("successes", [(True, False), (False, True), (True, True)])
def test_private_split_reply_remembers_only_confirmed_chunks(bot_env, monkeypatch, successes):
    bot = bot_env
    monkeypatch.setattr(bot, "_private_history", type(bot._private_history)())
    monkeypatch.setattr(bot, "_is_operator", lambda sender: True)
    monkeypatch.setattr(bot, "_split_long_message", lambda text: ["first chunk", "second chunk"])
    monkeypatch.setattr(bot.time, "sleep", lambda *_: None)
    calls = []
    def request(method, data):
        index = len(calls)
        calls.append(data)
        return {"ok": successes[index], "result": {"message_id": 9100 + index,
                                                     "text": data["text"], "date": 123}}
    monkeypatch.setattr(bot, "tg_request", request)
    bot._send_private_reply(private_message(bot, 550, "Question."), "Draft split at publication.")
    rows = bot._private_history.snapshot(111000111, 111000111)
    assert [row["text"] for row in rows] == [text for text, ok in zip(("first chunk", "second chunk"), successes) if ok]
    assert [row["message_id"] for row in rows] == [9100 + index for index, ok in enumerate(successes) if ok]


def test_private_history_expires_and_bounds_messages_text_and_conversations():
    from private_conversation import PrivateConversationHistory
    now = [0.0]
    history = PrivateConversationHistory(max_chats=2, max_messages=2,
                                        max_text_bytes=12, ttl=10,
                                        clock=lambda: now[0])
    def add(chat, number, text="A long private sentence."):
        history.append(chat, chat, {
            "message_id": number, "chat": {"id": chat, "type": "private"},
            "from": {"id": chat, "username": "human", "is_bot": False},
            "text": text, "date": 123,
            "document": {"file_id": "must-not-retain"},
        })
    for number in range(3):
        add(1, number + 1)
    rows = history.snapshot(1, 1)
    assert [m["message_id"] for m in rows] == [2, 3]
    assert all(len(m["text"].encode("utf-8")) <= 12 for m in rows)
    assert all("document" not in m for m in rows)
    rows[0]["from"]["username"] = "mutated"
    assert history.snapshot(1, 1)[0]["from"]["username"] == "human"
    add(2, 1)
    add(3, 1)
    assert history.snapshot(1, 1) == []
    now[0] = 11
    assert history.snapshot(2, 2) == []
    assert history.snapshot(3, 3) == []


def test_private_history_rejects_group_or_wrong_chat_rows():
    from private_conversation import PrivateConversationHistory
    history = PrivateConversationHistory()
    with pytest.raises(ValueError):
        history.append(1, 1, {"message_id": 1, "chat": {"id": 2, "type": "private"}, "text": "other"})
    with pytest.raises(ValueError):
        history.append(1, 1, {"message_id": 1, "chat": {"id": 1, "type": "group"}, "text": "group"})

"""Tests for witnessed-photo retrieval (spec: 2026-07-09-benthic-photo-retrieval-design.md).

Benthic only analyzes media attached to the triggering message; earlier photos
are bare [photo] markers. These tests cover the capture→marker→attach→prune
pipeline that lets full-brain calls view recent same-chat photos on demand."""

import importlib.util
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


def _load_bot_module():
    os.environ.setdefault("BENTHIC_BOT_TOKEN", "test:stub-token-do-not-use")
    os.environ.setdefault("WALLET_PRIVATE_KEY", "")
    if "benthic_bot_photo_test" in sys.modules:
        return sys.modules["benthic_bot_photo_test"]
    spec = importlib.util.spec_from_file_location(
        "benthic_bot_photo_test", ROOT / "benthic-bot.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["benthic_bot_photo_test"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def bot_env(monkeypatch, tmp_path):
    bot = _load_bot_module()
    monkeypatch.setattr(bot, "DB_FILE", tmp_path / "bot.db")
    bot._ensure_chat_table()
    return bot


def _photo_msg(message_id=101, chat_id=-100500, file_id="AgACAgFAKE", username="gerrithall"):
    return {
        "message_id": message_id,
        "chat": {"id": chat_id, "title": "Test"},
        "from": {"id": 7, "username": username},
        "photo": [
            {"file_id": "small", "file_size": 100},
            {"file_id": file_id, "file_size": 5000},   # largest last — must be picked
        ],
    }


def _image_document_msg(message_id=102, chat_id=-100500, file_id="DOCIMG"):
    return {
        "message_id": message_id,
        "chat": {"id": chat_id, "title": "Test"},
        "from": {"id": 8, "username": "w00t"},
        "document": {
            "file_id": file_id,
            "file_name": "chart.png",
            "file_size": 999,
        },
    }


def _text_document_msg(
        message_id=106, chat_id=-100500, file_id="DOCTEXT",
        file_name="review.md", file_size=40442, topic_id=None):
    """Build one Telegram text-document update with bounded metadata."""
    message = {
        "message_id": message_id,
        "chat": {"id": chat_id, "title": "Test"},
        "from": {"id": 9, "username": "commodore"},
        "document": {
            "file_id": file_id,
            "file_name": file_name,
            "file_size": file_size,
        },
    }
    if topic_id is not None:
        message["message_thread_id"] = topic_id
    return message


def _rows(bot):
    with bot._db(row_factory=True) as conn:
        return conn.execute(
            "SELECT chat_id, topic_id, message_id, sender, file_id FROM seen_photos "
            "ORDER BY message_id").fetchall()


def _document_rows(bot):
    """Return witnessed text-document metadata without reading any body."""
    with bot._db(row_factory=True) as conn:
        return conn.execute(
            "SELECT chat_id, topic_id, message_id, sender, file_id, file_name, "
            "file_size, seen_at FROM seen_documents ORDER BY message_id"
        ).fetchall()


def test_capture_photo_stores_largest_file_id(bot_env):
    bot = bot_env
    bot._record_seen_photo(-100500, None, _photo_msg())
    rows = _rows(bot)
    assert len(rows) == 1
    assert rows[0]["file_id"] == "AgACAgFAKE"
    assert rows[0]["chat_id"] == -100500
    assert rows[0]["topic_id"] == 0          # None topic normalizes to 0
    assert rows[0]["sender"] == "gerrithall"


def test_capture_image_document_stores_row(bot_env):
    bot = bot_env
    msg = {"message_id": 102, "from": {"username": "w00t"},
           "document": {"file_id": "DOCIMG", "file_name": "chart.png", "file_size": 999}}
    bot._record_seen_photo(-100500, 5, msg)
    rows = _rows(bot)
    assert rows[0]["file_id"] == "DOCIMG"
    assert rows[0]["topic_id"] == 5


def test_capture_ignores_text_pdf_and_duplicates(bot_env):
    bot = bot_env
    bot._record_seen_photo(-100500, None, {"message_id": 103, "text": "hi", "from": {}})
    bot._record_seen_photo(-100500, None, {"message_id": 104, "from": {},
                           "document": {"file_id": "PDF", "file_name": "doc.pdf"}})
    assert _rows(bot) == []
    bot._record_seen_photo(-100500, None, _photo_msg(message_id=105))
    bot._record_seen_photo(-100500, None, _photo_msg(message_id=105, file_id="OTHER"))
    rows = _rows(bot)
    assert len(rows) == 1                     # PK (chat_id, message_id) — first sighting wins
    assert rows[0]["file_id"] == "AgACAgFAKE"


def test_seen_document_schema_and_capture_are_metadata_only(bot_env):
    """Text documents persist retrievable metadata but never their contents."""
    bot = bot_env
    bot._record_seen_document(
        -100500,
        7,
        _text_document_msg(message_id=106, topic_id=7),
    )

    rows = _document_rows(bot)
    assert len(rows) == 1
    assert rows[0]["chat_id"] == -100500
    assert rows[0]["topic_id"] == 7
    assert rows[0]["message_id"] == 106
    assert rows[0]["sender"] == "commodore"
    assert rows[0]["file_id"] == "DOCTEXT"
    assert rows[0]["file_name"] == "review.md"
    assert rows[0]["file_size"] == 40442
    with bot._db() as conn:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(seen_documents)")
        }
    assert "body" not in columns
    assert "content" not in columns
    assert "text" not in columns


@pytest.mark.parametrize(
    "file_name",
    ("chart.png", "review.pdf", ".env", "payload.bin"),
)
def test_seen_document_capture_rejects_non_text_allowlist(
        bot_env, file_name):
    """Images, PDFs, secret env files, and unknown binaries are not indexed."""
    bot_env._record_seen_document(
        -100500,
        None,
        _text_document_msg(file_name=file_name),
    )
    assert _document_rows(bot_env) == []


def test_chat_history_migration_adds_reply_to_message_column(
        bot_env, monkeypatch, tmp_path):
    bot = bot_env
    legacy_db = tmp_path / "legacy.db"
    with sqlite3.connect(legacy_db) as conn:
        conn.execute("""CREATE TABLE chat_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            msg_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            topic_id INTEGER,
            sender_username TEXT,
            sender_is_bot INTEGER DEFAULT 0,
            text TEXT,
            our_reply TEXT,
            timestamp TEXT NOT NULL,
            UNIQUE(msg_id, chat_id)
        )""")
    monkeypatch.setattr(bot, "DB_FILE", legacy_db)
    bot._ensure_chat_table()
    with bot._db() as conn:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(chat_history)")
        }
    assert "reply_to_msg_id" in columns


def test_chat_history_migration_adds_nullable_event_time_column(
        bot_env, monkeypatch, tmp_path):
    """Legacy rows gain a nullable canonical event-time column idempotently."""
    bot = bot_env
    legacy_db = tmp_path / "legacy-event.db"
    with sqlite3.connect(legacy_db) as conn:
        conn.execute("""CREATE TABLE chat_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            msg_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            topic_id INTEGER,
            reply_to_msg_id INTEGER,
            sender_username TEXT,
            sender_is_bot INTEGER DEFAULT 0,
            text TEXT,
            our_reply TEXT,
            timestamp TEXT NOT NULL,
            UNIQUE(msg_id, chat_id)
        )""")
        conn.execute(
            "INSERT INTO chat_history "
            "(msg_id,chat_id,text,timestamp) VALUES (1,-100500,'legacy',?)",
            ("2026-07-13T12:00:00+00:00",),
        )
    monkeypatch.setattr(bot, "DB_FILE", legacy_db)

    bot._ensure_chat_table()
    bot._ensure_chat_table()

    with bot._db(row_factory=True) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(chat_history)")}
        legacy = conn.execute(
            "SELECT event_time FROM chat_history WHERE msg_id = 1"
        ).fetchone()
    assert "event_time" in columns
    assert legacy["event_time"] is None


def test_structured_history_preserves_order_and_reply_roles(bot_env):
    bot = bot_env
    first = {
        "message_id": 701,
        "chat": {"id": -100500},
        "from": {"username": "alice"},
        "text": "first",
    }
    second = {
        "message_id": 702,
        "chat": {"id": -100500},
        "from": {"username": "bob"},
        "text": "second",
        "reply_to_message": first,
    }
    bot.save_chat_message(first, our_reply="ack")
    bot.save_chat_message(second)
    rows = bot._get_structured_chat_history(
        chat_id=-100500,
        topic_id=None,
    )
    assert [(row["role"], row["message_id"]) for row in rows] == [
        ("incoming", 701),
        ("our_reply", 701),
        ("incoming", 702),
    ]
    assert rows[1]["reply_to_msg_id"] == 701
    assert rows[2]["reply_to_msg_id"] == 701


def test_structured_history_separates_event_and_observed_chronology(bot_env):
    """Delayed incoming events keep source time while bot replies use send time."""
    bot = bot_env
    message = {
        "message_id": 703,
        "date": "2026-07-12T10:00:00-04:00",
        "chat": {"id": -100500},
        "from": {"username": "alice"},
        "text": "delayed delivery",
    }

    bot.save_chat_message(message, our_reply="observed now")
    rows = bot._get_structured_chat_history(chat_id=-100500, topic_id=None)

    assert rows[0]["timestamp"] == "2026-07-12T14:00:00+00:00"
    assert rows[1]["timestamp"].endswith("+00:00")
    assert rows[1]["timestamp"] != rows[0]["timestamp"]


def test_legacy_and_malformed_event_times_never_support_chronology(bot_env):
    """Missing or malformed source times remain null in structured evidence."""
    bot = bot_env
    with bot._db() as conn:
        conn.execute(
            "INSERT INTO chat_history "
            "(msg_id,chat_id,text,timestamp,event_time) VALUES (704,-100500,?,?,NULL)",
            ("legacy", "2026-07-13T12:00:00+00:00"),
        )
        conn.commit()
    bot.save_chat_message({
        "message_id": 705,
        "date": "not-a-time",
        "chat": {"id": -100500},
        "from": {"username": "bob"},
        "text": "malformed",
    })

    rows = bot._get_structured_chat_history(chat_id=-100500, topic_id=None)

    assert [row["timestamp"] for row in rows] == [None, None]


def test_structured_history_none_topic_is_general_only(bot_env):
    bot = bot_env
    bot.save_chat_message({
        "message_id": 711,
        "chat": {"id": -100500},
        "from": {"username": "general"},
        "text": "general topic",
    })
    bot.save_chat_message({
        "message_id": 712,
        "chat": {"id": -100500},
        "message_thread_id": 9,
        "from": {"username": "threaded"},
        "text": "thread nine",
    })
    general = bot._get_structured_chat_history(
        chat_id=-100500,
        topic_id=None,
    )
    unscoped = bot._get_structured_chat_history(chat_id=0)
    assert [row["message_id"] for row in general] == [711]
    assert [row["message_id"] for row in unscoped] == [711, 712]


def test_poll_loop_wires_capture():
    """Wiring tripwire — capture must run inside poll()'s collect phase."""
    import inspect
    bot = _load_bot_module()
    source = inspect.getsource(bot.poll)
    assert "_record_seen_photo(" in source
    assert "_record_seen_document(" in source


# ── _apply_media_note: the production marking path (poll() Phase-1) ─────────
# Raw Telegram-shaped updates in, marker persisted onto msg["text"] out. The
# 2026-07-09 final review found the marker previously lived only in a
# poll-local variable — these tests pin the persisted-mutation contract.

def test_apply_media_note_captionless_photo():
    bot = _load_bot_module()
    msg = _photo_msg(message_id=42)                  # raw shape, no text/caption
    bot._apply_media_note(msg)
    assert msg["text"] == "[photo#42]"               # exactly the marker, stripped


def test_apply_media_note_captioned_photo():
    bot = _load_bot_module()
    msg = _photo_msg(message_id=43)
    msg["caption"] = "winthorpe receipts"
    bot._apply_media_note(msg)
    assert msg["text"] == "[photo#43] winthorpe receipts"


def test_apply_media_note_image_document():
    bot = _load_bot_module()
    msg = {"message_id": 44, "from": {"username": "w00t"},
           "document": {"file_id": "DOCIMG", "file_name": "chart.png"},
           "caption": "quarterly"}
    bot._apply_media_note(msg)
    assert msg["text"] == "[photo#44] [document: chart.png] quarterly"


def test_apply_media_note_pdf_document_gets_no_photo_marker():
    bot = _load_bot_module()
    msg = {"message_id": 45, "from": {},
           "document": {"file_id": "PDF", "file_name": "report.pdf"}}
    bot._apply_media_note(msg)
    assert msg["text"] == "[document: report.pdf]"
    assert "[photo#" not in msg["text"]


def test_apply_media_note_video_sticker_and_plain_text():
    bot = _load_bot_module()
    msg = {"message_id": 46, "video": {"file_id": "V"}, "caption": "clip"}
    bot._apply_media_note(msg)
    assert msg["text"] == "[video] clip"
    msg = {"message_id": 47, "sticker": {"emoji": "🦑"}}
    bot._apply_media_note(msg)
    assert msg["text"] == "[sticker: 🦑]"
    msg = {"message_id": 48, "text": "no media here"}
    bot._apply_media_note(msg)
    assert msg["text"] == "no media here"            # untouched


def test_unrelated_recent_photo_not_selected_for_bare_link(bot_env):
    photo = _photo_msg(message_id=4005)
    photo["message_thread_id"] = 7
    photo["date"] = int(time.time()) - 30
    bot_env._apply_media_note(photo)
    current = {
        "message_id": 4022,
        "chat": {"id": -100500},
        "message_thread_id": 7,
        "date": int(time.time()),
        "text": "https://x.com/a/status/1",
    }
    assert bot_env._select_grounding_photo_ids(current, [photo]) == ()


def test_captionless_current_photo_marker_does_not_select_prior_photo(bot_env):
    prior = _photo_msg(message_id=100)
    prior["message_thread_id"] = 7
    prior["date"] = int(time.time()) - 30
    current = _photo_msg(message_id=101)
    current["message_thread_id"] = 7
    current["date"] = int(time.time())
    bot_env._apply_media_note(current)
    assert current["text"] == "[photo#101]"
    assert bot_env._select_grounding_photo_ids(current, [prior, current]) == ()


def test_real_photo_caption_still_explicitly_selects_prior_photo(bot_env):
    prior = _photo_msg(message_id=100)
    prior["message_thread_id"] = 7
    prior["date"] = int(time.time()) - 30
    current = _photo_msg(message_id=101)
    current["message_thread_id"] = 7
    current["date"] = int(time.time())
    current["caption"] = "check the screenshot above"
    bot_env._apply_media_note(current)
    assert current["text"] == "[photo#101] check the screenshot above"
    assert bot_env._select_grounding_photo_ids(
        current, [prior, current]
    ) == (100,)


def test_generated_image_document_note_never_counts_as_user_intent(bot_env):
    prior = _photo_msg(message_id=100)
    prior["message_thread_id"] = 7
    prior["date"] = int(time.time()) - 30
    current = _image_document_msg(message_id=101)
    current["document"]["file_name"] = "foo]chart.png"
    current["message_thread_id"] = 7
    current["date"] = int(time.time())
    bot_env._apply_media_note(current)
    assert "chart" in current["text"]
    assert bot_env._select_grounding_photo_ids(current, [prior, current]) == ()


def _later_text_message(message_id, text, *, topic_id=7, sender_id=7):
    return {
        "message_id": message_id,
        "chat": {"id": -100500, "title": "Test"},
        "message_thread_id": topic_id,
        "from": {"id": sender_id, "username": "gerrithall"},
        "date": int(time.time()),
        "text": text,
    }


def test_merge_preserves_user_text_and_excludes_incorporated_photo(bot_env):
    photo = _photo_msg(message_id=201)
    photo["message_thread_id"] = 7
    photo["date"] = int(time.time()) - 10
    later = _later_text_message(202, "Benthic review this")
    bot_env._apply_media_note(photo)
    bot_env._apply_media_note(later)

    merged = bot_env._merge_consecutive_messages([photo, later])

    assert len(merged) == 1
    assert merged[0]["message_id"] == 202
    assert merged[0]["text"] == "[photo#201]\n\nBenthic review this"
    assert bot_env._user_authored_message_text(merged[0]) == "Benthic review this"
    assert merged[0]["_grounding_media_message_ids"] == (201,)
    assert bot_env._select_grounding_photo_ids(merged[0], [photo]) == ()


def test_merge_explicit_later_text_selects_other_photo_not_incorporated(bot_env):
    other = _photo_msg(message_id=200, file_id="OTHER")
    other["message_thread_id"] = 7
    other["date"] = int(time.time()) - 20
    incorporated = _photo_msg(message_id=201, file_id="CURRENT")
    incorporated["message_thread_id"] = 7
    incorporated["date"] = int(time.time()) - 10
    later = _later_text_message(202, "Benthic review this screenshot")
    for message in (other, incorporated, later):
        bot_env._apply_media_note(message)

    merged = bot_env._merge_consecutive_messages([incorporated, later])

    assert bot_env._user_authored_message_text(merged[0]) == (
        "Benthic review this screenshot"
    )
    assert merged[0]["_grounding_media_message_ids"] == (201,)
    assert bot_env._select_grounding_photo_ids(
        merged[0], [other, incorporated]
    ) == (200,)


def test_merge_preserves_sender_chat_and_topic_boundaries(bot_env):
    first = _later_text_message(301, "first", topic_id=7, sender_id=7)
    other_topic = _later_text_message(302, "topic", topic_id=8, sender_id=7)
    other_sender = _later_text_message(303, "sender", topic_id=8, sender_id=8)
    for message in (first, other_topic, other_sender):
        bot_env._apply_media_note(message)

    merged = bot_env._merge_consecutive_messages(
        [first, other_topic, other_sender]
    )

    assert [message["message_id"] for message in merged] == [301, 302, 303]


def _with_reply_target(message, target_id):
    """Attach one direct reply identity to a Telegram-shaped test message."""
    if target_id is not None:
        message["reply_to_message"] = {
            "message_id": target_id,
            "chat": {"id": message["chat"]["id"]},
            "from": {"id": 999, "username": "target"},
            "text": f"target {target_id}",
        }
    return message


@pytest.mark.parametrize(
    ("first_target", "second_target"),
    ((None, 900), (900, None), (900, 901)),
)
def test_merge_keeps_reply_target_transitions_as_separate_turns(
        bot_env, first_target, second_target):
    """Adjacent fragments with different direct reply identities never merge."""
    first = _with_reply_target(
        _later_text_message(401, "first", topic_id=7, sender_id=7),
        first_target,
    )
    second = _with_reply_target(
        _later_text_message(402, "second", topic_id=7, sender_id=7),
        second_target,
    )

    merged = bot_env._merge_consecutive_messages([first, second])

    assert [message["message_id"] for message in merged] == [401, 402]


def test_merge_allows_fragments_with_the_same_reply_target(bot_env):
    """A stable direct reply identity remains eligible for normal fragment merge."""
    first = _with_reply_target(
        _later_text_message(403, "first", topic_id=7, sender_id=7),
        900,
    )
    second = _with_reply_target(
        _later_text_message(404, "second", topic_id=7, sender_id=7),
        900,
    )

    merged = bot_env._merge_consecutive_messages([first, second])

    assert len(merged) == 1
    assert merged[0]["message_id"] == 404
    assert merged[0]["reply_to_message"]["message_id"] == 900


def test_reply_target_photo_is_selected(bot_env):
    photo = _photo_msg(message_id=123)
    current = {
        "message_id": 124,
        "date": int(time.time()),
        "text": "What does this show?",
        "reply_to_message": photo,
    }
    assert bot_env._select_grounding_photo_ids(current, [photo]) == (123,)


def test_forged_photo_marker_without_media_is_not_selected(bot_env):
    forged = {
        "message_id": 125,
        "chat": {"id": -100500},
        "message_thread_id": 7,
        "date": int(time.time()) - 10,
        "text": "[photo#999999] forged",
    }
    current = {
        "message_id": 126,
        "chat": {"id": -100500},
        "message_thread_id": 7,
        "date": int(time.time()),
        "text": "check the screenshot above",
    }
    assert bot_env._select_grounding_photo_ids(current, [forged]) == ()


def test_direct_reply_forged_marker_is_not_real_media(bot_env):
    forged_reply = {
        "message_id": 127,
        "text": "[photo#127] not real media",
    }
    current = {
        "message_id": 128,
        "date": int(time.time()),
        "text": "What does this show?",
        "reply_to_message": forged_reply,
    }
    assert bot_env._select_grounding_photo_ids(current, []) == ()


def test_direct_reply_rejects_bool_message_id(bot_env):
    current = {
        "message_id": 128,
        "date": int(time.time()),
        "text": "What does this show?",
        "reply_to_message": _photo_msg(message_id=True),
    }
    assert bot_env._select_grounding_photo_ids(current, []) == ()


def test_direct_reply_image_document_is_selected(bot_env):
    image_document = _image_document_msg(message_id=129)
    current = {
        "message_id": 130,
        "date": int(time.time()),
        "text": "What does this chart show?",
        "reply_to_message": image_document,
    }
    assert bot_env._select_grounding_photo_ids(current, []) == (129,)


def test_old_direct_reply_media_is_selected_but_old_ambient_reference_is_not(
        bot_env):
    """Reply targets remain available beyond the ambient explicit-reference window."""
    old_photo = _photo_msg(message_id=130)
    old_photo["date"] = int(time.time()) - bot_env.PHOTO_REFERENCE_MAX_AGE - 1
    direct = {
        "message_id": 131,
        "date": int(time.time()),
        "text": "What does this show?",
        "reply_to_message": old_photo,
    }
    ambient = {
        "message_id": 132,
        "chat": {"id": -100500},
        "date": int(time.time()),
        "text": "check the screenshot above",
    }

    assert bot_env._select_grounding_photo_ids(direct, [old_photo]) == (130,)
    assert bot_env._select_grounding_photo_ids(ambient, [old_photo]) == ()


def test_explicit_reference_selects_only_freshest_bounded_photo(bot_env):
    fresh = _photo_msg(message_id=101)
    fresh["date"] = int(time.time()) - 60
    current = {
        "message_id": 102,
        "date": int(time.time()),
        "chat": {"id": fresh["chat"]["id"]},
        "text": "check the screenshot above",
    }
    assert bot_env._select_grounding_photo_ids(current, [fresh]) == (101,)


def test_explicit_reference_ignores_cross_topic_photo(bot_env):
    cross_topic = _photo_msg(message_id=131)
    cross_topic["message_thread_id"] = 8
    cross_topic["date"] = int(time.time()) - 30
    current = {
        "message_id": 132,
        "chat": {"id": -100500},
        "message_thread_id": 7,
        "date": int(time.time()),
        "text": "check this screenshot",
    }
    assert bot_env._select_grounding_photo_ids(current, [cross_topic]) == ()


def test_explicit_reference_excludes_current_message(bot_env):
    current = _photo_msg(message_id=132)
    current.update({
        "message_thread_id": 7,
        "date": int(time.time()),
        "text": "check this screenshot",
    })
    assert bot_env._select_grounding_photo_ids(current, [current]) == ()


def test_old_photo_cannot_be_revived_by_fresh_marker(bot_env):
    old_photo = _photo_msg(message_id=133)
    old_photo["message_thread_id"] = 7
    old_photo["date"] = int(time.time()) - bot_env.PHOTO_REFERENCE_MAX_AGE - 1
    fresh_marker = {
        "message_id": 134,
        "chat": {"id": -100500},
        "message_thread_id": 7,
        "date": int(time.time()) - 1,
        "text": "[photo#133]",
    }
    current = {
        "message_id": 135,
        "chat": {"id": -100500},
        "message_thread_id": 7,
        "date": int(time.time()),
        "text": "check the screenshot above",
    }
    assert bot_env._select_grounding_photo_ids(
        current, [old_photo, fresh_marker]
    ) == ()


def test_explicit_reference_selects_fresh_image_document(bot_env):
    image_document = _image_document_msg(message_id=136)
    image_document["message_thread_id"] = 7
    image_document["date"] = int(time.time()) - 20
    current = {
        "message_id": 137,
        "chat": {"id": -100500},
        "message_thread_id": 7,
        "date": int(time.time()),
        "text": "check the chart above",
    }
    assert bot_env._select_grounding_photo_ids(
        current, [image_document]
    ) == (136,)


def test_normalize_strips_id_suffixed_photo_marker():
    bot = _load_bot_module()
    assert bot._normalize_for_dedup("[photo#123] squid digest") == "squid digest"
    assert bot._normalize_for_dedup("[photo] squid digest") == "squid digest"
    # Cross-path parity: Telegram (id-suffixed) and API (bare) forms must key equal
    assert (bot._content_key(0, "[photo#123] squid digest")
            == bot._content_key(0, "[photo] squid digest"))


def test_normalize_strips_combined_photo_document_prefix():
    """Image-documents now emit '[photo#id] [document: name] ' — the dedup
    normalizer must strip the whole combined prefix (repeated bracket groups)."""
    bot = _load_bot_module()
    assert (bot._normalize_for_dedup("[photo#12] [document: chart.png] squid digest")
            == "squid digest")
    # Cross-path parity with the API's bare/unprefixed form of the same content
    assert (bot._content_key(0, "[photo#12] [document: chart.png] squid digest")
            == bot._content_key(0, "squid digest"))


def test_poll_loop_emits_id_suffixed_marker():
    """Wiring tripwire — poll() must persist markers via _apply_media_note so
    buffered msg dicts (not a poll-local variable) carry them downstream."""
    import inspect
    bot = _load_bot_module()
    src = inspect.getsource(bot.poll)
    assert "_apply_media_note(" in src
    # The marker must not be rebuilt inline anymore — the helper owns it.
    assert 'media_note = "[photo] "' not in src


def test_download_by_file_id_downloads_and_sanitizes(bot_env, monkeypatch, tmp_path):
    bot = bot_env
    monkeypatch.setattr(bot, "tg_request",
                        lambda method, params=None: {"result": {"file_path": "photos/x.jpg"}})

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self, n): return b"JPGDATA"

    monkeypatch.setattr(bot.urllib.request, "urlopen", lambda url, timeout=30: _Resp())
    clean = tmp_path / "clean.png"
    clean.write_bytes(b"PNG")
    monkeypatch.setattr(bot, "_sanitize_image", lambda raw: str(clean))

    path, mtype = bot._download_by_file_id("SOMEID", "image")
    assert path == str(clean)
    assert mtype == "image"


def test_download_media_delegates_to_by_file_id(bot_env, monkeypatch):
    bot = bot_env
    seen = {}

    def fake(file_id, media_type):
        seen["args"] = (file_id, media_type)
        return "/tmp/fake.png", media_type

    monkeypatch.setattr(bot, "_download_by_file_id", fake)
    path, mtype = bot.download_media(_photo_msg(file_id="BIGID"))
    assert seen["args"] == ("BIGID", "image")
    assert (path, mtype) == ("/tmp/fake.png", "image")


def _seed_photo(
        bot, chat_id, message_id, file_id, sender="gerrithall",
        hours_ago=1, topic_id=0):
    ts = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()
    with bot._db() as conn:
        conn.execute("INSERT OR IGNORE INTO seen_photos VALUES (?,?,?,?,?,?,?)",
                     (chat_id, topic_id, message_id, sender, file_id, 1000, ts))
        conn.commit()


def _seed_document(
        bot, chat_id, message_id, file_id, *, file_name="review.md",
        file_size=40442, sender="commodore", seconds_ago=60, topic_id=0):
    """Insert one witnessed-document metadata row at a controlled age."""
    ts = (
        datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    ).isoformat()
    with bot._db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO seen_documents VALUES (?,?,?,?,?,?,?,?)",
            (
                chat_id,
                topic_id,
                message_id,
                sender,
                file_id,
                file_name,
                file_size,
                ts,
            ),
        )
        conn.commit()


def test_document_selection_prefers_exact_reply_without_keyword(bot_env):
    """An exact reply can select its fresh text document without vague guessing."""
    bot = bot_env
    _seed_document(bot, -100500, 301, "DOC301", topic_id=4)
    current = {
        "message_id": 900,
        "chat": {"id": -100500},
        "message_thread_id": 4,
        "text": "is this valid enough?",
        "reply_to_message": _text_document_msg(
            message_id=301,
            chat_id=-100500,
            file_id="DOC301",
            topic_id=4,
        ),
    }

    selected = bot._select_grounding_document_ids(current, direct=False)

    assert selected == bot.DocumentSelection((301,), False)


def test_document_selection_requires_direct_explicit_reference(bot_env):
    """A general reference selects one fresh document only on a direct turn."""
    bot = bot_env
    _seed_document(bot, -100500, 302, "DOC302", topic_id=4)
    direct = {
        "message_id": 901,
        "chat": {"id": -100500},
        "message_thread_id": 4,
        "text": "Benthic review the draft",
    }
    ambient = dict(direct, message_id=902, text="someone should review the draft")

    assert bot._select_grounding_document_ids(
        direct, direct=True
    ) == bot.DocumentSelection((302,), False)
    assert bot._select_grounding_document_ids(
        ambient, direct=False
    ) == bot.DocumentSelection()


def test_document_selection_is_scope_age_and_ambiguity_bounded(
        bot_env, monkeypatch):
    """Stale, cross-topic, and multiple candidates fail closed."""
    bot = bot_env
    monkeypatch.setattr(bot, "PHOTO_REFERENCE_MAX_AGE", 1800)
    _seed_document(
        bot, -100500, 303, "STALE", seconds_ago=1900, topic_id=4
    )
    _seed_document(bot, -100500, 304, "OTHER", topic_id=5)
    current = {
        "message_id": 903,
        "chat": {"id": -100500},
        "message_thread_id": 4,
        "text": "Benthic review the document",
    }
    assert bot._select_grounding_document_ids(
        current, direct=True
    ) == bot.DocumentSelection()

    _seed_document(
        bot, -100500, 305, "FIRST", file_name="first.md", topic_id=4
    )
    _seed_document(
        bot, -100500, 306, "SECOND", file_name="second.md", topic_id=4
    )
    assert bot._select_grounding_document_ids(
        current, direct=True
    ) == bot.DocumentSelection((), True)

    named = dict(current, text="Benthic use first.md")
    assert bot._select_grounding_document_ids(
        named, direct=True
    ) == bot.DocumentSelection((305,), False)


def test_document_renderer_caps_excerpt_and_declares_truncation(
        bot_env, tmp_path):
    """A large text file exposes only a labeled 16K sanitized excerpt."""
    path = tmp_path / "downloaded.tmp"
    path.write_text("x" * 20_000, encoding="utf-8")

    rendered = bot_env._render_text_document(
        str(path), "campaign-review.md", 20_000
    )
    metadata, body = rendered.split("\n", 1)

    assert "filename=campaign-review.md" in metadata
    assert "bytes=20000" in metadata
    assert "truncated=true" in metadata
    assert body == "x" * 16_000


def test_attach_recent_document_hides_file_id_and_preserves_origin(
        bot_env, monkeypatch, tmp_path):
    """The retriever uses the DB file ID but returns no prompt-visible copy."""
    bot = bot_env
    _seed_document(bot, -100500, 307, "SECRET-FILE-ID", topic_id=4)
    path = tmp_path / "review.md"
    path.write_text("draft body", encoding="utf-8")
    fetched = []

    def download(file_id, media_type):
        fetched.append((file_id, media_type))
        return str(path), "text"

    monkeypatch.setattr(bot, "_download_by_file_id", download)
    attached = bot._attach_recent_documents((307,), -100500, 4)

    assert fetched == [("SECRET-FILE-ID", "text")]
    assert attached == (
        bot.AttachedDocument(
            307,
            "telegram:-100500:307:attachment",
            "review.md",
            str(path),
            40442,
        ),
    )
    assert not hasattr(attached[0], "file_id")


def test_attach_recent_document_rechecks_age_and_allowlist(
        bot_env, monkeypatch):
    """The download seam rejects stale or non-text rows even if passed an ID."""
    bot = bot_env
    _seed_document(
        bot, -100500, 308, "STALE-ID", seconds_ago=1900, topic_id=4
    )
    _seed_document(
        bot,
        -100500,
        309,
        "BINARY-ID",
        file_name="payload.bin",
        topic_id=4,
    )
    monkeypatch.setattr(bot, "PHOTO_REFERENCE_MAX_AGE", 1800)
    monkeypatch.setattr(
        bot,
        "_download_by_file_id",
        lambda *args: pytest.fail("invalid document row reached download"),
    )

    assert bot._attach_recent_documents((308,), -100500, 4) == ()
    assert bot._attach_recent_documents((309,), -100500, 4) == ()


@pytest.fixture
def attach_env(bot_env, monkeypatch, tmp_path):
    bot = bot_env
    fetched = []

    def fake_download(file_id, media_type):
        p = tmp_path / f"clean-{file_id}.png"
        p.write_bytes(b"PNG")
        fetched.append(file_id)
        return str(p), "image"

    monkeypatch.setattr(bot, "_download_by_file_id", fake_download)
    return bot, fetched


def test_attach_returns_typed_path_without_file_id(attach_env):
    bot, fetched = attach_env
    _seed_photo(bot, -100500, 123, "FIDA")
    attached = bot._attach_recent_photos([123], -100500, None)
    assert len(attached) == 1
    assert attached[0].message_id == 123
    assert attached[0].source_ref == "telegram:-100500:123:photo"
    assert attached[0].path.endswith("clean-FIDA.png")
    assert not hasattr(attached[0], "file_id")
    assert fetched == ["FIDA"]


def test_attach_caps_newest_first_and_deduplicates(attach_env):
    bot, fetched = attach_env
    for message_id in (201, 202, 203, 204):
        _seed_photo(bot, -100500, message_id, f"F{message_id}")
    attached = bot._attach_recent_photos(
        [201, 202, 203, 204, 202], -100500, None
    )
    assert len(attached) == bot.MAX_ATTACHED_PHOTOS == 3
    assert [item.message_id for item in attached] == [204, 203, 202]
    assert fetched == ["F204", "F203", "F202"]


def test_attach_enforces_chat_and_integer_bounds(attach_env):
    bot, fetched = attach_env
    _seed_photo(bot, -100999, 301, "OTHERCHAT")
    _seed_photo(bot, -100500, 123, "FIDA")
    attached = bot._attach_recent_photos(
        [301, 99999999999999999999999999, True, 123], -100500, None
    )
    assert [item.message_id for item in attached] == [123]
    assert fetched == ["FIDA"]


def test_attach_enforces_topic_scope(attach_env):
    bot, fetched = attach_env
    _seed_photo(bot, -100500, 321, "TOPIC9", topic_id=9)
    assert bot._attach_recent_photos([321], -100500, 7) == ()
    assert fetched == []
    attached = bot._attach_recent_photos([321], -100500, 9)
    assert [item.message_id for item in attached] == [321]
    assert fetched == ["TOPIC9"]


def test_attach_failure_and_empty_selection_are_noop(attach_env, monkeypatch):
    bot, _ = attach_env
    _seed_photo(bot, -100500, 402, "BOOM")
    monkeypatch.setattr(
        bot,
        "_download_by_file_id",
        lambda *args: (_ for _ in ()).throw(RuntimeError("expired")),
    )
    assert bot._attach_recent_photos([402], -100500, None) == ()
    assert bot._attach_recent_photos([], -100500, None) == ()


def test_generate_response_attaches_and_cleans_up(bot_env, monkeypatch, tmp_path):
    bot = bot_env
    # This regression exercises the legacy prompt attachment and cleanup path,
    # not grounded evidence composition.
    monkeypatch.setattr(bot, "ENABLE_REPLY_GROUNDING", False)
    _seed_photo(bot, -100500, 123, "FIDA", topic_id=5)
    clean = tmp_path / "clean-FIDA.png"
    clean.write_bytes(b"PNG")
    monkeypatch.setattr(bot, "_download_by_file_id",
                        lambda file_id, media_type: (str(clean), "image"))
    # Neutralize the heavy context builders — not under test here.
    for fn in ("get_recent_activity", "get_own_actions", "get_notes",
               "get_relevant_knowledge"):
        monkeypatch.setattr(bot, fn, lambda *a, **k: "")
    monkeypatch.setattr(bot, "get_chat_history", lambda *a, **k: "")
    monkeypatch.setattr(bot, "_get_cached_positions", lambda *a, **k: "")
    captured = {}

    def fake_llm(prompt, **kw):
        captured["prompt"] = prompt
        captured["file_existed_during_call"] = clean.exists()
        return "Looked at the screenshot — Winthorpe's numbers check out."

    monkeypatch.setattr(bot, "llm_ask", fake_llm)
    # Production shape: a RAW captionless photo update (the driving incident
    # shape), marked by the same helper poll() Phase-1 runs — NOT a
    # hand-fabricated "text" field. Pre-fix, the marker lived in a poll-local
    # variable, so this exact shape had empty text, was skipped from
    # conv_context, and the attach never fired on real traffic.
    witnessed = _photo_msg(message_id=123, file_id="FIDA")
    witnessed["message_thread_id"] = 5
    witnessed["date"] = int(time.time()) - 60
    bot._apply_media_note(witnessed)
    assert witnessed["text"] == "[photo#123]"        # what the buffer now carries
    msg = {"message_id": 900, "chat": {"id": -100500}, "message_thread_id": 5,
           "from": {"id": 42, "username": "zero", "first_name": "Zero"},
           "text": "Benthic check the screenshot above and review his claims"}
    resp = bot.generate_response(msg, is_direct=True, recent_messages=[witnessed])
    assert isinstance(resp, str) and "Winthorpe" in resp
    assert f"view file '{clean}'" in captured["prompt"]
    assert captured["file_existed_during_call"] is True
    assert not clean.exists()               # unlinked after the LLM call


def test_generate_response_wires_attach():
    import inspect
    bot = _load_bot_module()
    assert "_generate_legacy_response(" in inspect.getsource(bot.generate_response)
    assert "_generate_grounded_response(" in inspect.getsource(bot.generate_response)
    assert "_attach_recent_photos(" in inspect.getsource(
        bot._generate_legacy_response
    )
    assert "_attach_recent_photos(" in inspect.getsource(
        bot._generate_grounded_response
    )


def test_prune_drops_old_and_overflow_rows(bot_env, monkeypatch):
    bot = bot_env
    # Pin retention so a PHOTO_RETENTION_DAYS shell override can't change
    # what "old" means for this test (the global is read at call time).
    monkeypatch.setattr(bot, "PHOTO_RETENTION_DAYS", 7)
    # Age path: one row beyond the 7-day retention window.
    _seed_photo(bot, -100500, 501, "OLD", hours_ago=24 * 10)
    # Row-cap path: three fresh rows with distinct seen_at timestamps, then a
    # cap of 2 — the cap SQL keeps the newest rows by seen_at DESC, so exactly
    # the two most recent fresh rows must survive. The module global resolves
    # at call time inside _prune_chat_history, so monkeypatching it works.
    _seed_photo(bot, -100500, 502, "FRESH1", hours_ago=1)
    _seed_photo(bot, -100500, 503, "FRESH2", hours_ago=2)
    _seed_photo(bot, -100500, 504, "FRESH3", hours_ago=3)
    monkeypatch.setattr(bot, "_MAX_SEEN_PHOTOS_ROWS", 2)
    monkeypatch.setattr(bot, "_prune_counter", 99)             # fire on next call
    bot._prune_chat_history()
    rows = _rows(bot)
    # 501 dropped by age; 504 (oldest fresh) dropped by the row cap.
    assert [r["message_id"] for r in rows] == [502, 503]


def test_prune_drops_old_and_overflow_document_rows(bot_env, monkeypatch):
    """Witnessed documents have an independent age and row-count bound."""
    bot = bot_env
    monkeypatch.setattr(bot, "PHOTO_RETENTION_DAYS", 7)
    _seed_document(
        bot, -100500, 601, "OLD", seconds_ago=10 * 24 * 3600
    )
    _seed_document(bot, -100500, 602, "NEWEST", seconds_ago=60)
    _seed_document(bot, -100500, 603, "MIDDLE", seconds_ago=120)
    _seed_document(bot, -100500, 604, "OLDEST", seconds_ago=180)
    monkeypatch.setattr(bot, "_MAX_SEEN_DOCUMENTS_ROWS", 2)
    monkeypatch.setattr(bot, "_prune_counter", 99)

    bot._prune_chat_history()

    assert [row["message_id"] for row in _document_rows(bot)] == [602, 603]

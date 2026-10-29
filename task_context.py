"""Metadata-only reply receipts for explicitly continued source tasks.

Message bodies have one existing home, chat_history. Receipts identify and
hash those witnessed rows; rehydration never trusts the quoted bot prose.
"""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import hashlib
import os
import re


RETENTION_SECONDS = 7 * 86400
MAX_REQUESTS = 8
MAX_SOURCES = 8
MAX_RECEIPT_ROWS = 4000
MAX_MESSAGE_CHARS = 2000
_HTTP = re.compile(r"https?://", re.I)


class TaskReceiptUnavailable(ValueError):
    """A receipt exists but cannot safely identify one witnessed task."""


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def history_text_complete(text, flag):
    """Legacy rows at the old storage limit may be truncated prefixes."""
    return isinstance(text, str) and (
        (flag == 1 and len(text) <= MAX_MESSAGE_CHARS)
        or (flag is None and len(text) < MAX_MESSAGE_CHARS)
    )


def _positive_id(value):
    return type(value) is int and 0 < value <= 0x7FFFFFFFFFFFFFFF


def scope(message):
    chat = message.get("chat") or {}
    return chat.get("id"), 0 if chat.get("is_forum") is False else int(message.get("message_thread_id") or 0)


def _event_time(value):
    try:
        if type(value) in (int, float):
            stamp = datetime.fromtimestamp(value, timezone.utc)
        elif isinstance(value, str):
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        else:
            return None
        if stamp.tzinfo is None:
            return None
        return stamp.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def has_explicit_source(message):
    text = str(message.get("text") or message.get("caption") or "")
    # Explicit source selection is deterministic. Natural-language task
    # continuity is classified only after an exact receipt is authenticated.
    return bool(_HTTP.search(text))


@dataclass(frozen=True)
class TaskMessage:
    message_id: int
    chat_id: int
    topic_id: int
    sender_id: int
    text: str
    event_time: str
    content_hash: str

    @classmethod
    def witnessed(cls, message):
        sender = message.get("from") or {}
        chat_id, topic_id = scope(message)
        timestamp = _event_time(message.get("event_time", message.get("date")))
        text = str(message.get("text") or message.get("caption") or "")
        if (not _positive_id(message.get("message_id"))
                or type(chat_id) is not int or not chat_id
                or not _positive_id(sender.get("id")) or sender.get("is_bot")
                or not text or len(text) > MAX_MESSAGE_CHARS or timestamp is None):
            return None
        try:
            content_hash = digest(text)
        except UnicodeError:
            return None
        return cls(message["message_id"], chat_id, topic_id, sender["id"], text,
                   timestamp.isoformat(), content_hash)


@dataclass(frozen=True)
class TaskContext:
    chat_id: int
    topic_id: int
    requester_id: int
    root_message_id: int
    requests: tuple[TaskMessage, ...]
    sources: tuple[TaskMessage, ...]
    reply_message_id: int | None = None

    def valid_for(self, current, *, now=None):
        current_time = _event_time(current.get("event_time", current.get("date")))
        now = now or datetime.now(timezone.utc)
        if (scope(current) != (self.chat_id, self.topic_id)
                or (current.get("from") or {}).get("id") != self.requester_id
                or not current_time or not self.sources or not self.requests
                or len(self.requests) > MAX_REQUESTS or len(self.sources) > MAX_SOURCES
                or self.requests[0].message_id != self.root_message_id):
            return False
        for item in (*self.requests, *self.sources):
            if not isinstance(item, TaskMessage):
                return False
            timestamp = _event_time(item.event_time)
            if (timestamp is None
                    or (item.chat_id, item.topic_id) != (self.chat_id, self.topic_id)
                    or not 0 <= (now - timestamp).total_seconds() <= RETENTION_SECONDS
                    or not 0 <= (current_time - timestamp).total_seconds() <= RETENTION_SECONDS
                    or item.message_id > current.get("message_id", 0)
                    or digest(item.text) != item.content_hash):
                return False
        return all(item.sender_id == self.requester_id for item in self.requests)

    def continue_with(self, message):
        current = TaskMessage.witnessed(message)
        if current is None or not self.valid_for(message):
            return None
        requests = self.requests
        if current.message_id not in {item.message_id for item in requests}:
            requests += (current,)
        result = replace(self, requests=requests)
        return result if result.valid_for(message) else None


def create_context(message, source_messages):
    current = TaskMessage.witnessed(message)
    sources = tuple(TaskMessage.witnessed(source) for source in source_messages)
    if current is None or not sources or any(item is None for item in sources):
        return None
    sources = tuple({item.message_id: item for item in sources}.values())
    context = TaskContext(current.chat_id, current.topic_id, current.sender_id,
                          current.message_id, (current,), sources)
    return context if context.valid_for(message) else None


def ensure_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS reply_task_messages (
        chat_id INTEGER NOT NULL,
        topic_id INTEGER NOT NULL,
        bot_message_id INTEGER NOT NULL,
        root_message_id INTEGER NOT NULL,
        requester_id INTEGER NOT NULL,
        message_id INTEGER NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('request', 'source')),
        sender_id INTEGER NOT NULL,
        content_hash TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(chat_id, topic_id, bot_message_id, root_message_id, message_id, role)
    )""")


def _history_matches(conn, item):
    row = conn.execute(
        "SELECT topic_id, sender_id, sender_is_bot, text, event_time, text_complete FROM chat_history "
        "WHERE chat_id = ? AND msg_id = ?", (item.chat_id, item.message_id),
    ).fetchone()
    return row is not None and history_text_complete(row[3], row[5]) and (
        int(row[0] or 0), row[1], bool(row[2]), digest(row[3] or ""),
        _event_time(row[4]),
    ) == (item.topic_id, item.sender_id, False, item.content_hash, _event_time(item.event_time))


def record(conn, context, sent_ids):
    if not isinstance(context, TaskContext) or not isinstance(sent_ids, (list, tuple)):
        return 0
    valid_ids = tuple(dict.fromkeys(value for value in sent_ids if _positive_id(value)))[:32]
    if not valid_ids or not all(_history_matches(conn, item) for item in (*context.requests, *context.sources)):
        return 0
    created_at = datetime.now(timezone.utc).isoformat()
    written = 0
    for sent_id in valid_ids:
        for role, messages in (("request", context.requests), ("source", context.sources)):
            for item in messages:
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO reply_task_messages "
                    "(chat_id, topic_id, bot_message_id, root_message_id, requester_id, "
                    "message_id, role, sender_id, content_hash, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (context.chat_id, context.topic_id, sent_id, context.root_message_id,
                     context.requester_id, item.message_id, role, item.sender_id,
                     item.content_hash, created_at),
                )
                written += max(cursor.rowcount, 0)
    return written


# The bot's own @handle (lowercase); receipts are only trusted from it.
_BOT_USERNAME = os.environ.get("BOT_USERNAME", "").lower()


def restore(conn, message):
    """Authenticate an exact sent-message receipt and reload each original row."""
    reply = message.get("reply_to_message") or {}
    reply_sender = reply.get("from") or {}
    reply_id = reply.get("message_id")
    if (not _positive_id(reply_id) or not reply_sender.get("is_bot")
            or not _BOT_USERNAME
            or str(reply_sender.get("username") or "").lower() != _BOT_USERNAME
            or has_explicit_source(message)):
        return None
    chat_id, topic_id = scope(message)
    rows = conn.execute(
        "SELECT r.root_message_id, r.requester_id, r.message_id, r.role, r.sender_id, "
        "r.content_hash, r.created_at, h.topic_id, h.sender_id, h.sender_is_bot, "
        "h.text, h.event_time, h.text_complete FROM reply_task_messages r "
        "LEFT JOIN chat_history h ON h.chat_id=r.chat_id AND h.msg_id=r.message_id "
        "WHERE r.chat_id=? AND r.topic_id=? AND r.bot_message_id=? "
        "ORDER BY r.message_id, r.role LIMIT ?",
        (chat_id, topic_id, reply_id, MAX_REQUESTS + MAX_SOURCES + 1),
    ).fetchall()
    if not rows:
        return None
    if len(rows) > MAX_REQUESTS + MAX_SOURCES:
        raise TaskReceiptUnavailable("task receipt exceeds bound")
    identities = {(row[0], row[1]) for row in rows}
    if len(identities) != 1:
        raise TaskReceiptUnavailable("conflicting task receipts")
    root_id, requester_id = next(iter(identities))
    if (message.get("from") or {}).get("id") != requester_id:
        raise TaskReceiptUnavailable("task requester mismatch")
    now = datetime.now(timezone.utc)
    requests, sources = [], []
    for root, requester, msg_id, role, sender_id, content_hash, created_at, row_topic, row_sender, is_bot, text, event_time, text_complete in rows:
        receipt_time = _event_time(created_at)
        if (receipt_time is None or not 0 <= (now - receipt_time).total_seconds() <= RETENTION_SECONDS
                or row_topic is None and text is None or int(row_topic or 0) != topic_id
                or row_sender != sender_id or is_bot or not isinstance(text, str)
                or not history_text_complete(text, text_complete)
                or digest(text) != content_hash or role not in {"request", "source"}):
            raise TaskReceiptUnavailable("task receipt integrity failed")
        item = TaskMessage(msg_id, chat_id, topic_id, sender_id, text, event_time, content_hash)
        (requests if role == "request" else sources).append(item)
    context = TaskContext(chat_id, topic_id, requester_id, root_id, tuple(requests), tuple(sources), reply_id)
    if not context.valid_for(message, now=now):
        raise TaskReceiptUnavailable("task context is out of scope")
    return context


def prune(conn):
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=RETENTION_SECONDS)).isoformat()
    conn.execute("DELETE FROM reply_task_messages WHERE created_at < ?", (cutoff,))
    conn.execute(
        "DELETE FROM reply_task_messages WHERE rowid NOT IN "
        "(SELECT rowid FROM reply_task_messages ORDER BY created_at DESC, rowid DESC LIMIT ?)",
        (MAX_RECEIPT_ROWS,),
    )

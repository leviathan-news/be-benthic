"""Bounded, process-local text history for private Telegram conversations."""

from collections import OrderedDict
from copy import deepcopy
import threading
import time


class PrivateConversationHistory:
    def __init__(self, *, max_chats=32, max_messages=20, max_text_bytes=8192,
                 ttl=1800, clock=time.monotonic):
        self._max_chats = max_chats
        self._max_messages = max_messages
        self._max_text_bytes = max_text_bytes
        self._ttl = ttl
        self._clock = clock
        self._chats = OrderedDict()
        self._lock = threading.Lock()

    def _prune(self, now):
        for key, rows in list(self._chats.items()):
            retained = [(at, row) for at, row in rows if now - at < self._ttl]
            if retained:
                self._chats[key] = retained
            else:
                del self._chats[key]

    def snapshot(self, chat_id, sender_id):
        with self._lock:
            self._prune(self._clock())
            return deepcopy([row for _, row in self._chats.get((chat_id, sender_id), ())])

    def append(self, chat_id, sender_id, message):
        chat = message.get("chat") or {}
        if chat.get("type") != "private" or chat.get("id") != chat_id:
            raise ValueError("private conversation scope mismatch")
        message_id = message.get("message_id")
        if type(message_id) is not int or message_id <= 0:
            raise ValueError("private conversation message ID is required")
        text = message.get("text") or message.get("caption") or ""
        if not text:
            return
        # Retain only bounded text and attribution. File IDs, reply trees,
        # ingress metadata and command capabilities never enter this cache.
        author = message.get("from") or {}
        row = {
            "message_id": message_id,
            "chat": {"id": chat_id, "type": "private", "is_forum": False},
            "from": {"id": author.get("id"),
                     "username": str(author.get("username") or "?")[:64],
                     "is_bot": bool(author.get("is_bot"))},
            "text": text.encode("utf-8")[:self._max_text_bytes].decode("utf-8", "ignore"),
        }
        for field in ("date", "event_time"):
            if field in message:
                row[field] = message[field]
        with self._lock:
            now = self._clock()
            self._prune(now)
            key = (chat_id, sender_id)
            rows = [(at, old) for at, old in self._chats.pop(key, ())
                    if old["message_id"] != message_id]
            self._chats[key] = (rows + [(now, row)])[-self._max_messages:]
            while len(self._chats) > self._max_chats:
                self._chats.popitem(last=False)

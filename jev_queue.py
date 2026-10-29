"""Durable evaluation and publication stages for Telegram push news."""

import json
import sqlite3
import time
from datetime import datetime, timezone


class NewsQueue:
    def __init__(self, path):
        self.conn = sqlite3.connect(str(path), timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("""CREATE TABLE IF NOT EXISTS jev_news_jobs (
            source_id INTEGER PRIMARY KEY,
            channel TEXT NOT NULL, msg_id INTEGER NOT NULL,
            text TEXT NOT NULL, msg_date TEXT,
            stage TEXT NOT NULL DEFAULT 'evaluate',
            decision TEXT, attempts INTEGER NOT NULL DEFAULT 0,
            retry_at REAL NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL,
            UNIQUE(channel, msg_id)
        )""")
        self.conn.execute("CREATE INDEX IF NOT EXISTS jev_news_ready "
                          "ON jev_news_jobs(stage, retry_at, source_id)")
        self.conn.commit()

    def close(self):
        self.conn.close()

    def ingest(self):
        # Keep the source row until its durable job reaches a terminal state.
        # Copy and enqueue happen in one SQLite transaction; a restart can
        # repeat this safely without launching duplicate evaluation jobs.
        with self.conn:
            self.conn.execute("""INSERT OR IGNORE INTO jev_news_jobs
                (source_id, channel, msg_id, text, msg_date, updated_at)
                SELECT id, channel, msg_id, COALESCE(text, ''), msg_date, ?
                FROM pending_source_messages WHERE consumed = 0
                AND NOT EXISTS (SELECT 1 FROM jev_news_jobs j
                                WHERE j.channel = pending_source_messages.channel
                                  AND j.msg_id = pending_source_messages.msg_id)
                ORDER BY id LIMIT 500""", (time.time(),))

    def ready(self, stage, limit):
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM jev_news_jobs WHERE stage = ? AND retry_at <= ? "
            "ORDER BY source_id LIMIT ?", (stage, time.time(), limit))]

    def move_to_recovery(self, row):
        with self.conn:
            self.conn.execute("UPDATE jev_news_jobs SET stage='recover', attempts=0, "
                              "retry_at=0, updated_at=? WHERE source_id=?",
                              (time.time(), row["source_id"]))

    def finish_unavailable(self, row):
        # Expiration is an operational outcome, not a negative editorial label.
        with self.conn:
            self.conn.execute("UPDATE jev_news_jobs SET stage='unavailable', updated_at=? "
                              "WHERE source_id=?", (time.time(), row["source_id"]))
            self.conn.execute("UPDATE pending_source_messages SET consumed=1 WHERE id=?",
                              (row["source_id"],))

    def finish_evaluation(self, row, decision):
        now = datetime.now(timezone.utc).isoformat()
        selected = decision is not None
        with self.conn:
            self.conn.execute("""INSERT OR IGNORE INTO evaluated_messages
                (channel, msg_id, text, url, is_newsworthy, reason, headline_hint, heat, evaluated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""", (
                row["channel"], row["msg_id"], row["text"][:2000],
                decision["url"] if selected else None, int(selected),
                decision["reason"] if selected else "jev_filtered",
                decision["headline_hint"] if selected else None,
                decision["heat"] if selected else None, now))
            self.conn.execute("UPDATE jev_news_jobs SET stage=?, decision=?, attempts=0, "
                              "retry_at=0, updated_at=? WHERE source_id=?", (
                "post" if selected else "done", json.dumps(decision) if selected else None,
                time.time(), row["source_id"]))
            if not selected:
                self.conn.execute("UPDATE pending_source_messages SET consumed=1 WHERE id=?",
                                  (row["source_id"],))

    def finish_post(self, row):
        with self.conn:
            self.conn.execute("UPDATE jev_news_jobs SET stage='done', updated_at=? "
                              "WHERE source_id=?", (time.time(), row["source_id"]))
            self.conn.execute("UPDATE pending_source_messages SET consumed=1 WHERE id=?",
                              (row["source_id"],))

    def defer(self, row):
        # Outages remain retryable without falling back to a metered LLM. The
        # cap also prevents a broken source from holding the queue head forever.
        attempts = row["attempts"] + 1
        with self.conn:
            self.conn.execute("UPDATE jev_news_jobs SET attempts=?, retry_at=?, "
                              "updated_at=? WHERE source_id=?", (
                attempts, time.time() + min(300, 5 * 2 ** min(attempts, 6)),
                time.time(), row["source_id"]))

    def prior_candidates(self, source_id):
        # Recovery can finish after a newer source row. Compare all accepted
        # pending publications, not just smaller IDs. Completed decisions can
        # have been rejected downstream; only actual published headlines belong
        # in the final publication guard, never those rejected source texts.
        return [json.loads(r[0])["text"][:800] for r in self.conn.execute(
            "SELECT decision FROM jev_news_jobs WHERE source_id != ? "
            "AND stage = 'post' AND decision IS NOT NULL AND updated_at > ? "
            "ORDER BY updated_at DESC, source_id DESC LIMIT 25", (source_id, time.time() - 86400))]

    def prune(self):
        with self.conn:
            self.conn.execute("DELETE FROM jev_news_jobs WHERE stage IN ('done','unavailable') AND updated_at < ?",
                              (time.time() - 7 * 86400,))

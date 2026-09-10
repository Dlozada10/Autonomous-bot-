"""SQLite-backed memory: what we've covered, made, and posted.

This is what keeps the channel from repeating itself, which is both an
audience problem and a YouTube-policy problem.
"""
from __future__ import annotations

import json
import sqlite3
import time
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS topics (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT UNIQUE NOT NULL,   -- normalised title, for exact-match dedupe
    title       TEXT NOT NULL,
    url         TEXT,
    source      TEXT,
    summary     TEXT,
    seen_at     REAL NOT NULL,
    used_at     REAL                    -- NULL until a video is made from it
);

CREATE TABLE IF NOT EXISTS videos (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    slug        TEXT UNIQUE NOT NULL,
    topic_id    INTEGER REFERENCES topics(id),
    format_id   TEXT NOT NULL,
    voice_id    TEXT,
    title       TEXT NOT NULL,
    script      TEXT NOT NULL,          -- JSON blob of the full script object
    score       REAL,
    duration    REAL,
    path        TEXT,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS uploads (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id    INTEGER REFERENCES videos(id),
    platform    TEXT NOT NULL,
    remote_id   TEXT,
    url         TEXT,
    status      TEXT NOT NULL,          -- ok | error
    detail      TEXT,
    created_at  REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_topics_seen ON topics(seen_at);
CREATE INDEX IF NOT EXISTS idx_videos_created ON videos(created_at);
CREATE INDEX IF NOT EXISTS idx_uploads_platform ON uploads(platform, created_at);
"""

_STOPWORDS = {
    "the", "a", "an", "is", "are", "to", "of", "for", "and", "or", "in", "on",
    "with", "your", "you", "how", "why", "what", "this", "that", "it", "its",
    "new", "now", "best", "top", "vs", "from", "at", "by", "be", "can",
}


def normalise(text: str) -> str:
    """Strip a title down to its meaningful tokens for comparison."""
    cleaned = "".join(ch.lower() if ch.isalnum() or ch.isspace() else " " for ch in text)
    tokens = [t for t in cleaned.split() if t and t not in _STOPWORDS]
    return " ".join(sorted(tokens))


class Store:
    def __init__(self, path: str | Path):
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # --- topics ------------------------------------------------------
    def add_topic(self, title: str, url: str, source: str, summary: str) -> int | None:
        """Insert a topic. Returns its id, or None if we already had it."""
        fp = normalise(title)
        if not fp:
            return None
        try:
            cur = self.conn.execute(
                "INSERT INTO topics (fingerprint, title, url, source, summary, seen_at)"
                " VALUES (?,?,?,?,?,?)",
                (fp, title, url, source, summary, time.time()),
            )
            self.conn.commit()
            return int(cur.lastrowid)
        except sqlite3.IntegrityError:
            return None

    def recent_fingerprints(self, days: int) -> list[str]:
        cutoff = time.time() - days * 86400
        rows = self.conn.execute(
            "SELECT fingerprint FROM topics WHERE used_at IS NOT NULL AND used_at > ?",
            (cutoff,),
        ).fetchall()
        return [r["fingerprint"] for r in rows]

    def is_too_similar(self, title: str, days: int, threshold: float) -> bool:
        """True if we've already published something this close."""
        fp = normalise(title)
        for prior in self.recent_fingerprints(days):
            if SequenceMatcher(None, fp, prior).ratio() >= threshold:
                return True
        return False

    def unused_topics(self, limit: int = 50) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM topics WHERE used_at IS NULL ORDER BY seen_at DESC LIMIT ?",
            (limit,),
        ).fetchall()

    def mark_topic_used(self, topic_id: int) -> None:
        self.conn.execute(
            "UPDATE topics SET used_at = ? WHERE id = ?", (time.time(), topic_id)
        )
        self.conn.commit()

    # --- rotation ----------------------------------------------------
    def recent_values(self, column: str, limit: int) -> list[str]:
        """Last N format_ids or voice_ids, newest first. Drives cooldowns."""
        if column not in {"format_id", "voice_id"}:
            raise ValueError(f"not a rotatable column: {column}")
        rows = self.conn.execute(
            f"SELECT {column} FROM videos ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [r[column] for r in rows if r[column]]

    # --- videos ------------------------------------------------------
    def add_video(self, **kw: Any) -> int:
        kw.setdefault("created_at", time.time())
        kw["script"] = json.dumps(kw.get("script", {}))
        cols = ", ".join(kw)
        marks = ", ".join("?" for _ in kw)
        cur = self.conn.execute(
            f"INSERT INTO videos ({cols}) VALUES ({marks})", tuple(kw.values())
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def pending_uploads(self, platform: str) -> list[sqlite3.Row]:
        """Rendered videos that this platform hasn't accepted yet."""
        return self.conn.execute(
            "SELECT v.* FROM videos v WHERE v.path IS NOT NULL AND NOT EXISTS ("
            "  SELECT 1 FROM uploads u WHERE u.video_id = v.id"
            "    AND u.platform = ? AND u.status = 'ok')"
            " ORDER BY v.created_at ASC",
            (platform,),
        ).fetchall()

    # --- uploads -----------------------------------------------------
    def add_upload(
        self, video_id: int, platform: str, status: str,
        remote_id: str = "", url: str = "", detail: str = "",
    ) -> None:
        self.conn.execute(
            "INSERT INTO uploads (video_id, platform, remote_id, url, status, detail,"
            " created_at) VALUES (?,?,?,?,?,?,?)",
            (video_id, platform, remote_id, url, status, detail, time.time()),
        )
        self.conn.commit()

    def uploads_today(self, platform: str) -> int:
        cutoff = time.time() - 86400
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM uploads WHERE platform = ? AND status = 'ok'"
            " AND created_at > ?",
            (platform, cutoff),
        ).fetchone()
        return int(row["n"])

    def close(self) -> None:
        self.conn.close()

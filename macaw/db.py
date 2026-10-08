"""SQLite storage. One file, WAL mode, plain SQL.

All timestamps are stored as ISO-8601 strings in UTC.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id                  INTEGER PRIMARY KEY,          -- Telegram user id
    chat_id             INTEGER NOT NULL,
    timezone            TEXT NOT NULL,
    quiet_start         TEXT NOT NULL DEFAULT '00:00',
    quiet_end           TEXT NOT NULL DEFAULT '08:00',
    cards_per_session   INTEGER NOT NULL DEFAULT 1,
    max_reminders       INTEGER NOT NULL DEFAULT 4,
    first_reminder_min  INTEGER NOT NULL DEFAULT 60,
    desired_retention   REAL NOT NULL DEFAULT 0.9,
    llm                 TEXT,                         -- 'claude' or 'free'; NULL = default for the user
    created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS decks (
    id               INTEGER PRIMARY KEY,
    user_id          INTEGER NOT NULL REFERENCES users(id),
    name             TEXT NOT NULL,
    deck_type        TEXT NOT NULL,
    fields           TEXT NOT NULL,                    -- JSON list of field names
    reverse          INTEGER NOT NULL DEFAULT 0,
    new_per_day      INTEGER NOT NULL DEFAULT 20,
    reviews_per_day  INTEGER NOT NULL DEFAULT 200,
    language         TEXT,
    created_at       TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS decks_user_name ON decks(user_id, name COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS notes (
    id          INTEGER PRIMARY KEY,
    deck_id     INTEGER NOT NULL REFERENCES decks(id) ON DELETE CASCADE,
    fields      TEXT NOT NULL,                         -- JSON object field -> value
    sort_key    TEXT NOT NULL,                         -- normalized first field, for duplicates
    guid        TEXT,                                  -- Anki's note id, kept for import/export
    anki        TEXT,                                  -- JSON: the note as Anki had it (note type, raw fields, tags)
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS notes_deck_key ON notes(deck_id, sort_key);

CREATE TABLE IF NOT EXISTS cards (
    id            INTEGER PRIMARY KEY,
    note_id       INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    ord           INTEGER NOT NULL DEFAULT 0,          -- 0 forward, 1 reverse
    state         INTEGER NOT NULL DEFAULT 0,          -- 0 new, 1 learning, 2 review, 3 relearning
    step          INTEGER,
    stability     REAL,
    difficulty    REAL,
    due           TEXT,
    last_review   TEXT,
    buried_until  TEXT,                                -- postponed: not shown before this time
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS cards_note ON cards(note_id);

CREATE TABLE IF NOT EXISTS review_log (
    id           INTEGER PRIMARY KEY,
    card_id      INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    rating       INTEGER NOT NULL,
    reviewed_at  TEXT NOT NULL,
    before       TEXT NOT NULL,                        -- JSON card scheduling state before
    after        TEXT NOT NULL,                        -- JSON card scheduling state after
    source       TEXT NOT NULL,                        -- 'claude' or 'user'
    reason       TEXT,
    message_id   INTEGER,                              -- Telegram message showing the grade
    missed       TEXT                                  -- what the answer left out ('' = complete, NULL = not recorded)
);
CREATE INDEX IF NOT EXISTS review_log_card ON review_log(card_id);
CREATE INDEX IF NOT EXISTS review_log_time ON review_log(reviewed_at);

CREATE TABLE IF NOT EXISTS proposals (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL,
    deck_id     INTEGER NOT NULL REFERENCES decks(id) ON DELETE CASCADE,
    fields      TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',       -- pending, added, skipped, replaced
    message_id  INTEGER,
    note_id     INTEGER,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL,
    role        TEXT NOT NULL,                         -- user, bot, note
    text        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_user ON messages(user_id, id);

CREATE TABLE IF NOT EXISTS conv_state (
    user_id              INTEGER PRIMARY KEY,
    active_card_id       INTEGER,
    asked_at             TEXT,
    next_ask_at          TEXT,
    session_count        INTEGER NOT NULL DEFAULT 0,
    burst                INTEGER NOT NULL DEFAULT 0,   -- extra cards the user asked for in a row
    last_user_at         TEXT,
    last_bot_at          TEXT,
    reminders_date       TEXT,
    reminders_today      INTEGER NOT NULL DEFAULT 0,
    reminders_streak     INTEGER NOT NULL DEFAULT 0,
    last_reminder_at     TEXT,
    editing_proposal_id  INTEGER,
    pending_input        TEXT,                         -- JSON, e.g. {"kind": "rename_deck", "deck_id": 3}
    llm_backoff_until    TEXT
);

-- Friends who joined through an invite link. They use the free model only.
CREATE TABLE IF NOT EXISTS guests (
    user_id     INTEGER PRIMARY KEY,
    name        TEXT,                                  -- Telegram name when they joined
    invited_by  INTEGER NOT NULL,
    added_at    TEXT NOT NULL
);

-- Anki note types of imported notes, so exports update the same notes in Anki.
CREATE TABLE IF NOT EXISTS anki_notetypes (
    user_id  INTEGER NOT NULL,
    id       INTEGER NOT NULL,
    data     TEXT NOT NULL,                            -- JSON: name, type, field names, templates, css
    PRIMARY KEY (user_id, id)
);

CREATE TABLE IF NOT EXISTS invites (
    code        TEXT PRIMARY KEY,
    created_by  INTEGER NOT NULL,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    used_by     INTEGER,
    used_at     TEXT
);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat() if dt else None


def parse(ts: str | None) -> datetime | None:
    return datetime.fromisoformat(ts) if ts else None


class Store:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        cols = {r["name"] for r in self.q("PRAGMA table_info(users)")}
        if "llm" not in cols:
            self.x("ALTER TABLE users ADD COLUMN llm TEXT")
        note_cols = {r["name"] for r in self.q("PRAGMA table_info(notes)")}
        if "guid" not in note_cols:
            self.x("ALTER TABLE notes ADD COLUMN guid TEXT")
        if "anki" not in note_cols:
            self.x("ALTER TABLE notes ADD COLUMN anki TEXT")
        review_cols = {r["name"] for r in self.q("PRAGMA table_info(review_log)")}
        if "message_id" not in review_cols:
            self.x("ALTER TABLE review_log ADD COLUMN message_id INTEGER")
        if "missed" not in review_cols:
            self.x("ALTER TABLE review_log ADD COLUMN missed TEXT")

    # ---------- generic helpers ----------

    def q(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, tuple(params)).fetchall()

    def q1(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, tuple(params)).fetchone()

    def x(self, sql: str, params: Iterable[Any] = ()) -> int:
        cur = self.conn.execute(sql, tuple(params))
        return cur.lastrowid or 0

    def backup(self, dest_dir: Path, keep: int = 14) -> Path | None:
        """Write today's copy of the database (once a day) and drop old ones."""
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"macaw-{utcnow():%Y-%m-%d}.sqlite3"
        if dest.exists():
            return None
        target = sqlite3.connect(str(dest))
        try:
            self.conn.backup(target)
        finally:
            target.close()
        for old in sorted(dest_dir.glob("macaw-*.sqlite3"))[:-keep]:
            old.unlink()
        return dest

    # ---------- users ----------

    def get_user(self, user_id: int) -> sqlite3.Row | None:
        return self.q1("SELECT * FROM users WHERE id=?", (user_id,))

    def ensure_user(self, user_id: int, chat_id: int, tz: str) -> sqlite3.Row:
        if not self.get_user(user_id):
            self.x(
                "INSERT INTO users(id, chat_id, timezone, created_at) VALUES (?,?,?,?)",
                (user_id, chat_id, tz, iso(utcnow())),
            )
            self.x("INSERT OR IGNORE INTO conv_state(user_id) VALUES (?)", (user_id,))
        return self.get_user(user_id)  # type: ignore[return-value]

    def all_users(self) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM users")

    def update_user(self, user_id: int, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        self.x(f"UPDATE users SET {cols} WHERE id=?", (*fields.values(), user_id))

    # ---------- guests and invites ----------

    def is_guest(self, user_id: int) -> bool:
        return self.q1("SELECT 1 FROM guests WHERE user_id=?", (user_id,)) is not None

    def guests(self) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM guests ORDER BY added_at")

    def remove_guest(self, user_id: int) -> bool:
        return self.conn.execute("DELETE FROM guests WHERE user_id=?", (user_id,)).rowcount > 0

    def create_invite(self, code: str, created_by: int, expires_at: datetime) -> None:
        self.x(
            "INSERT INTO invites(code, created_by, created_at, expires_at) VALUES (?,?,?,?)",
            (code, created_by, iso(utcnow()), iso(expires_at)),
        )

    def redeem_invite(self, code: str, user_id: int, name: str | None = None, now: datetime | None = None) -> int | None:
        """Use a one-time invite: the user becomes a guest. Returns who invited
        them, or None if the code is unknown, already used or expired."""
        now = now or utcnow()
        claimed = self.conn.execute(
            "UPDATE invites SET used_by=?, used_at=? WHERE code=? AND used_by IS NULL AND expires_at>?",
            (user_id, iso(now), code, iso(now)),
        ).rowcount
        if not claimed:
            return None
        inviter = self.q1("SELECT created_by FROM invites WHERE code=?", (code,))["created_by"]
        self.x(
            "INSERT OR IGNORE INTO guests(user_id, name, invited_by, added_at) VALUES (?,?,?,?)",
            (user_id, name, inviter, iso(now)),
        )
        return inviter

    # ---------- conversation state ----------

    def state(self, user_id: int) -> sqlite3.Row:
        row = self.q1("SELECT * FROM conv_state WHERE user_id=?", (user_id,))
        if row is None:
            self.x("INSERT INTO conv_state(user_id) VALUES (?)", (user_id,))
            row = self.q1("SELECT * FROM conv_state WHERE user_id=?", (user_id,))
        return row  # type: ignore[return-value]

    def update_state(self, user_id: int, **fields: Any) -> None:
        self.state(user_id)
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        self.x(f"UPDATE conv_state SET {cols} WHERE user_id=?", (*fields.values(), user_id))

    def pending_input(self, user_id: int) -> dict | None:
        raw = self.state(user_id)["pending_input"]
        return json.loads(raw) if raw else None

    def set_pending_input(self, user_id: int, value: dict | None) -> None:
        self.update_state(user_id, pending_input=json.dumps(value) if value else None)

    # ---------- messages ----------

    def log_message(self, user_id: int, role: str, text: str) -> None:
        self.x(
            "INSERT INTO messages(user_id, role, text, created_at) VALUES (?,?,?,?)",
            (user_id, role, text, iso(utcnow())),
        )

    def recent_messages(self, user_id: int, limit: int = 24) -> list[sqlite3.Row]:
        rows = self.q(
            "SELECT * FROM messages WHERE user_id=? ORDER BY id DESC LIMIT ?", (user_id, limit)
        )
        return list(reversed(rows))

    def prune_messages(self, keep: int = 500) -> None:
        """Keep each user's latest messages, so one chatty user can't trim another's history."""
        self.x(
            "DELETE FROM messages WHERE id IN (SELECT id FROM "
            "(SELECT id, ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY id DESC) AS n FROM messages) "
            "WHERE n > ?)",
            (keep,),
        )

    # ---------- decks ----------

    def decks(self, user_id: int) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM decks WHERE user_id=? ORDER BY name COLLATE NOCASE", (user_id,))

    def deck(self, deck_id: int) -> sqlite3.Row | None:
        return self.q1("SELECT * FROM decks WHERE id=?", (deck_id,))

    def deck_by_name(self, user_id: int, name: str) -> sqlite3.Row | None:
        return self.q1(
            "SELECT * FROM decks WHERE user_id=? AND name=? COLLATE NOCASE", (user_id, name.strip())
        )

    def create_deck(
        self,
        user_id: int,
        name: str,
        deck_type: str,
        fields: list[str],
        reverse: bool = False,
        language: str | None = None,
    ) -> int:
        return self.x(
            "INSERT INTO decks(user_id, name, deck_type, fields, reverse, language, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (user_id, name.strip(), deck_type, json.dumps(fields), int(reverse), language, iso(utcnow())),
        )

    def update_deck(self, deck_id: int, **fields: Any) -> None:
        if "fields" in fields and not isinstance(fields["fields"], str):
            fields["fields"] = json.dumps(fields["fields"])
        cols = ", ".join(f"{k}=?" for k in fields)
        self.x(f"UPDATE decks SET {cols} WHERE id=?", (*fields.values(), deck_id))

    def delete_deck(self, deck_id: int) -> None:
        self.x("DELETE FROM decks WHERE id=?", (deck_id,))

    # ---------- notes and cards ----------

    def add_note(self, deck_id: int, fields: dict[str, str], sort_key: str, reverse: bool) -> int:
        now = iso(utcnow())
        note_id = self.x(
            "INSERT INTO notes(deck_id, fields, sort_key, created_at) VALUES (?,?,?,?)",
            (deck_id, json.dumps(fields, ensure_ascii=False), sort_key, now),
        )
        self.x("INSERT INTO cards(note_id, ord, created_at) VALUES (?,0,?)", (note_id, now))
        if reverse:
            self.x("INSERT INTO cards(note_id, ord, created_at) VALUES (?,1,?)", (note_id, now))
        return note_id

    def add_reverse_cards(self, deck_id: int) -> None:
        """Give every note in the deck its answer -> prompt card, if missing."""
        self.x(
            "INSERT INTO cards(note_id, ord, created_at) "
            "SELECT n.id, 1, ? FROM notes n WHERE n.deck_id=? "
            "AND NOT EXISTS (SELECT 1 FROM cards c WHERE c.note_id=n.id AND c.ord=1)",
            (iso(utcnow()), deck_id),
        )

    def note(self, note_id: int) -> sqlite3.Row | None:
        return self.q1("SELECT * FROM notes WHERE id=?", (note_id,))

    def update_note(self, note_id: int, fields: dict[str, str], sort_key: str) -> None:
        self.x(
            "UPDATE notes SET fields=?, sort_key=? WHERE id=?",
            (json.dumps(fields, ensure_ascii=False), sort_key, note_id),
        )

    def delete_note(self, note_id: int) -> None:
        self.x("DELETE FROM notes WHERE id=?", (note_id,))

    def notes_by_key(self, deck_id: int, sort_key: str) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM notes WHERE deck_id=? AND sort_key=?", (deck_id, sort_key))

    def notes_in_deck(self, deck_id: int, offset: int = 0, limit: int = 1000) -> list[sqlite3.Row]:
        return self.q(
            "SELECT * FROM notes WHERE deck_id=? ORDER BY sort_key LIMIT ? OFFSET ?",
            (deck_id, limit, offset),
        )

    def count_notes(self, deck_id: int) -> int:
        return self.q1("SELECT COUNT(*) AS n FROM notes WHERE deck_id=?", (deck_id,))["n"]

    def search_notes(self, user_id: int, text: str, deck_id: int | None = None, limit: int = 20) -> list[sqlite3.Row]:
        like = f"%{text.lower()}%"
        sql = (
            "SELECT n.*, d.name AS deck_name FROM notes n JOIN decks d ON d.id=n.deck_id "
            "WHERE d.user_id=? AND (lower(n.fields) LIKE ? OR n.sort_key LIKE ?)"
        )
        params: list[Any] = [user_id, like, like]
        if deck_id is not None:
            sql += " AND d.id=?"
            params.append(deck_id)
        sql += " ORDER BY n.sort_key LIMIT ?"
        params.append(limit)
        return self.q(sql, params)

    def user_notes(self, user_id: int) -> list[sqlite3.Row]:
        """Every note of the user with its deck's name and field list."""
        return self.q(
            "SELECT n.*, d.name AS deck_name, d.fields AS deck_fields FROM notes n "
            "JOIN decks d ON d.id=n.deck_id WHERE d.user_id=?",
            (user_id,),
        )

    def card(self, card_id: int) -> sqlite3.Row | None:
        return self.q1(
            "SELECT c.*, n.fields AS note_fields, n.deck_id AS deck_id, d.user_id AS user_id "
            "FROM cards c JOIN notes n ON n.id=c.note_id JOIN decks d ON d.id=n.deck_id "
            "WHERE c.id=?",
            (card_id,),
        )

    def cards_for_note(self, note_id: int) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM cards WHERE note_id=? ORDER BY ord", (note_id,))

    def set_card_schedule(self, card_id: int, sched: dict[str, Any]) -> None:
        self.x(
            "UPDATE cards SET state=?, step=?, stability=?, difficulty=?, due=?, last_review=? WHERE id=?",
            (
                sched["state"],
                sched["step"],
                sched["stability"],
                sched["difficulty"],
                sched["due"],
                sched["last_review"],
                card_id,
            ),
        )

    def bury(self, card_id: int, until: datetime) -> None:
        self.x("UPDATE cards SET buried_until=? WHERE id=?", (iso(until), card_id))

    def user_cards(self, user_id: int) -> list[sqlite3.Row]:
        """Every card of the user with its deck settings attached."""
        return self.q(
            "SELECT c.*, n.fields AS note_fields, n.deck_id AS deck_id, d.name AS deck_name, "
            "d.new_per_day, d.reviews_per_day "
            "FROM cards c JOIN notes n ON n.id=c.note_id JOIN decks d ON d.id=n.deck_id "
            "WHERE d.user_id=?",
            (user_id,),
        )

    # ---------- review log ----------

    def add_review(
        self,
        card_id: int,
        rating: int,
        reviewed_at: datetime,
        before: dict,
        after: dict,
        source: str,
        reason: str | None,
        missed: str | None = None,
    ) -> int:
        return self.x(
            "INSERT INTO review_log(card_id, rating, reviewed_at, before, after, source, reason, missed) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (card_id, rating, iso(reviewed_at), json.dumps(before), json.dumps(after), source, reason, missed),
        )

    def review(self, log_id: int) -> sqlite3.Row | None:
        return self.q1("SELECT * FROM review_log WHERE id=?", (log_id,))

    def update_review(
        self, log_id: int, rating: int, after: dict, source: str, reason: str | None = None
    ) -> None:
        self.x(
            "UPDATE review_log SET rating=?, after=?, source=?, reason=COALESCE(?, reason) WHERE id=?",
            (rating, json.dumps(after), source, reason, log_id),
        )

    def set_review_missed(self, log_id: int, missed: str) -> None:
        self.x("UPDATE review_log SET missed=? WHERE id=?", (missed, log_id))

    def answer_history(self, card_id: int, limit: int = 4) -> list[sqlite3.Row]:
        """The card's latest reviews where the bot noted what the answer missed, oldest first."""
        rows = self.q(
            "SELECT * FROM review_log WHERE card_id=? AND missed IS NOT NULL ORDER BY id DESC LIMIT ?",
            (card_id, limit),
        )
        return rows[::-1]

    def set_review_message(self, log_id: int, message_id: int) -> None:
        self.x("UPDATE review_log SET message_id=? WHERE id=?", (message_id, log_id))

    def last_graded(self, user_id: int, since: datetime) -> sqlite3.Row | None:
        """The user's most recent review since a time, if it is still its card's latest."""
        return self.q1(
            "SELECT r.* FROM review_log r JOIN cards c ON c.id=r.card_id JOIN notes n ON n.id=c.note_id "
            "JOIN decks d ON d.id=n.deck_id WHERE d.user_id=? AND r.reviewed_at>=? "
            "AND r.id=(SELECT MAX(id) FROM review_log WHERE card_id=r.card_id) "
            "ORDER BY r.id DESC LIMIT 1",
            (user_id, iso(since)),
        )

    def last_review_id(self, card_id: int) -> int | None:
        row = self.q1("SELECT id FROM review_log WHERE card_id=? ORDER BY id DESC LIMIT 1", (card_id,))
        return row["id"] if row else None

    def reviews_since(self, user_id: int, since: datetime) -> list[sqlite3.Row]:
        """Review log rows of this user since a time, with each card's deck and whether it was new."""
        return self.q(
            "SELECT r.*, n.deck_id AS deck_id, json_extract(r.before, '$.state') AS before_state "
            "FROM review_log r JOIN cards c ON c.id=r.card_id JOIN notes n ON n.id=c.note_id "
            "JOIN decks d ON d.id=n.deck_id WHERE d.user_id=? AND r.reviewed_at>=?",
            (user_id, iso(since)),
        )

    # ---------- proposals ----------

    def create_proposal(self, user_id: int, deck_id: int, fields: dict[str, str]) -> int:
        return self.x(
            "INSERT INTO proposals(user_id, deck_id, fields, created_at) VALUES (?,?,?,?)",
            (user_id, deck_id, json.dumps(fields, ensure_ascii=False), iso(utcnow())),
        )

    def proposal(self, proposal_id: int) -> sqlite3.Row | None:
        return self.q1("SELECT * FROM proposals WHERE id=?", (proposal_id,))

    def card_owner(self, card_id: int) -> sqlite3.Row:
        return self.q1(
            "SELECT u.* FROM cards c JOIN notes n ON n.id=c.note_id JOIN decks d ON d.id=n.deck_id "
            "JOIN users u ON u.id=d.user_id WHERE c.id=?",
            (card_id,),
        )

    def pending_proposals(self, user_id: int) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM proposals WHERE user_id=? AND status='pending' ORDER BY id", (user_id,))

    def update_proposal(self, proposal_id: int, **fields: Any) -> None:
        if "fields" in fields and not isinstance(fields["fields"], str):
            fields["fields"] = json.dumps(fields["fields"], ensure_ascii=False)
        cols = ", ".join(f"{k}=?" for k in fields)
        self.x(f"UPDATE proposals SET {cols} WHERE id=?", (*fields.values(), proposal_id))

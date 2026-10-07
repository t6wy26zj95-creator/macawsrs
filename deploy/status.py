"""Read-only snapshot of the bot's state, for debugging.

Run on the server:  sudo /opt/macaw/venv/bin/python /opt/macaw/app/deploy/status.py
It changes nothing.
"""

import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

DB = sys.argv[1] if len(sys.argv) > 1 else "/opt/macaw/data/macaw.sqlite3"
db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
db.row_factory = sqlite3.Row
STATES = {0: "new", 1: "learning", 2: "review", 3: "relearning"}

for user in db.execute("SELECT * FROM users"):
    tz = ZoneInfo(user["timezone"])

    def t(s):
        return datetime.fromisoformat(s).astimezone(tz).strftime("%d %b %H:%M") if s else "-"

    print(f"now {datetime.now(timezone.utc).astimezone(tz):%d %b %H:%M} ({user['timezone']})")
    st = db.execute("SELECT * FROM conv_state WHERE user_id=?", (user["id"],)).fetchone()
    if st:
        print(f"open question: card {st['active_card_id'] or '-'} asked {t(st['asked_at'])}")
        print(f"next card at: {t(st['next_ask_at'])}  last you: {t(st['last_user_at'])}  last bot: {t(st['last_bot_at'])}")
        print(f"reminders today: {st['reminders_today']}  claude paused until: {t(st['llm_backoff_until'])}")
    print("cards:")
    for c in db.execute(
        "SELECT c.*, n.sort_key FROM cards c JOIN notes n ON n.id=c.note_id "
        "JOIN decks d ON d.id=n.deck_id WHERE d.user_id=? ORDER BY c.id", (user["id"],)
    ):
        extra = f" postponed until {t(c['buried_until'])}" if c["buried_until"] else ""
        print(f"  #{c['id']} {c['sort_key'][:25]}: {STATES.get(c['state'])}, due {t(c['due'])}{extra}")
    print("last messages:")
    rows = db.execute(
        "SELECT * FROM messages WHERE user_id=? ORDER BY id DESC LIMIT 20", (user["id"],)
    ).fetchall()
    for m in reversed(rows):
        text = " ".join(m["text"].split())
        print(f"  {t(m['created_at'])} {m['role']}: {text[:60]}")

print("problems in the log (last 2 hours):")
try:
    out = subprocess.run(
        ["journalctl", "-u", "macaw", "--since", "2 hours ago", "--no-pager", "-p", "warning", "-n", "15", "-o", "cat"],
        capture_output=True, text=True,
    ).stdout.strip()
except OSError as e:
    out = f"  (couldn't read the log: {e})"
print(out or "  none")

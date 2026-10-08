"""Anki packages (.apkg / .colpkg): read them into decks, write decks back out.

Reading understands every format current Anki versions write:
  collection.anki21b  zstd-compressed SQLite, schema 18 (Anki 2.1.50+, the default)
  collection.anki21   SQLite, schema 11 ("support older Anki versions")
  collection.anki2    SQLite, schema 11 (very old Anki)
Writing produces the schema-11 `collection.anki2` package, which every Anki,
AnkiMobile and AnkiDroid version imports.

Media (audio, images) is not supported: references to it are dropped from the
text. Scheduling is kept: FSRS memory state when Anki has it, otherwise it is
rebuilt from the review history, and due dates stay what Anki had.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import shutil
import sqlite3
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from fsrs import Card, Rating, Scheduler, State

from . import srs
from .db import Store, iso, parse
from .templates import deck_fields, note_fields, sort_key

MAX_UNPACKED = 512 * 1024 * 1024  # refuse collections bigger than this once unpacked
SEP = "\x1f"


class ApkgError(ValueError):
    """The file can't be read as an Anki package; the message is for the user."""


# ====================================================================== reading


@dataclass
class ImportedCard:
    ord: int  # 0 forward, 1 reverse (the bot's convention)
    sched: dict[str, Any]  # state, step, stability, difficulty, due, last_review
    reviews: list[dict[str, Any]] = field(default_factory=list)  # Anki revlog rows
    anki_ord: int = 0  # which of the note type's card templates it was in Anki


@dataclass
class ImportedNote:
    guid: str
    values: dict[str, str]  # field name -> plain text
    cards: list[ImportedCard]
    fields: list[str]  # this note's own field order (prompt, answer, extras)
    ntid: int = 0  # Anki note type
    raw: list[str] = field(default_factory=list)  # Anki's field contents (HTML, media references)
    tags: str = ""


@dataclass
class ImportedDeck:
    name: str
    notes: list[ImportedNote] = field(default_factory=list)

    @property
    def fields(self) -> list[str]:
        """The field list of the note type most notes use, without fields that are always empty."""
        counts: dict[tuple[str, ...], int] = {}
        for n in self.notes:
            counts[tuple(n.fields)] = counts.get(tuple(n.fields), 0) + 1
        main = list(max(counts, key=counts.get)) if counts else ["Front", "Back"]
        used = [f for i, f in enumerate(main) if i < 2 or any(n.values.get(f) for n in self.notes)]
        return used

    @property
    def card_count(self) -> int:
        return sum(len(n.cards) for n in self.notes)


@dataclass
class Package:
    decks: list[ImportedDeck]
    suspended: int = 0  # cards left out because they are suspended in Anki
    other_cards: int = 0  # cards of extra card types the bot has no room for
    media: int = 0  # notes that referenced audio or images
    notetypes: dict[int, dict[str, Any]] = field(default_factory=dict)  # used note types, see _NoteType.model

    @property
    def card_count(self) -> int:
        return sum(d.card_count for d in self.decks)

    @property
    def studied_count(self) -> int:
        return sum(1 for d in self.decks for n in d.notes for c in n.cards if c.sched["state"] != srs.NEW)


def read_package(path: Path | str, user: Mapping[str, Any], fallback_name: str = "Imported") -> Package:
    """Read an .apkg/.colpkg file. Due dates are placed on the user's study days."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = _unpack(Path(path), Path(tmp))
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            return _read_collection(conn, user, fallback_name)
        except sqlite3.DatabaseError as e:
            raise ApkgError("That file doesn't look like an Anki deck I can read.") from e
        finally:
            conn.close()


def _unpack(path: Path, tmp: Path) -> Path:
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as e:
        raise ApkgError("That file isn't an Anki package (.apkg or .colpkg).") from e
    with zf:
        names = set(zf.namelist())
        out = tmp / "collection.sqlite"
        for name in ("collection.anki21b", "collection.anki21", "collection.anki2"):
            if name not in names:
                continue
            info = zf.getinfo(name)
            if name != "collection.anki21b" and info.file_size > MAX_UNPACKED:
                raise ApkgError("That deck is too big for me to import.")
            with zf.open(name) as src, open(out, "wb") as dst:
                if name == "collection.anki21b":
                    _unzstd(src, dst)
                else:
                    shutil.copyfileobj(src, dst)
            return out
    raise ApkgError("That file isn't an Anki package (.apkg or .colpkg).")


def _unzstd(src, dst) -> None:
    import zstandard

    total = 0
    reader = zstandard.ZstdDecompressor().stream_reader(src)
    while True:
        chunk = reader.read(1 << 20)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_UNPACKED:
            raise ApkgError("That deck is too big for me to import.")
        dst.write(chunk)


# ---------- note types ----------

_REF = re.compile(r"\{\{([^{}]+)\}\}")


def _refs(fmt: str, names: list[str]) -> list[str]:
    """Field names a card template shows, in order (conditionals and FrontSide skipped)."""
    out: list[str] = []
    for raw in _REF.findall(fmt or ""):
        raw = raw.strip()
        if raw[:1] in "#^/!":
            continue
        name = raw.split(":")[-1].strip()
        if name in names and name not in out:
            out.append(name)
    return out


@dataclass
class _NoteType:
    names: list[str]  # Anki's field order
    order: list[str]  # the bot's order: prompt, answer, extras
    cloze: bool
    reverse_ord: int | None  # Anki template ord that is the answer -> prompt card
    model: dict[str, Any] = field(default_factory=dict)  # what export needs to write it back


def _note_type(ntid: int, name: str, names: list[str], templates: list[tuple[int, str, str, str]],
               cloze: bool, css: str) -> _NoteType:
    model = {
        "id": ntid, "name": name, "type": 1 if cloze else 0, "flds": names, "css": css,
        "tmpls": [{"ord": o, "name": n, "qfmt": q, "afmt": a} for o, n, q, a in sorted(templates)],
    }
    nt = _note_type_order(names, [(o, q, a) for o, _, q, a in templates], cloze)
    nt.model = model
    return nt


def _note_type_order(names: list[str], templates: list[tuple[int, str, str]], cloze: bool) -> _NoteType:
    if cloze:
        return _NoteType(names, ["Text", "Answer"] + [n for n in names if n != "Text"], True, None)
    templates = sorted(templates)
    if not templates:
        return _NoteType(names, list(names), False, None)
    _, q0, a0 = templates[0]
    front = _refs(q0, names)
    back = [n for n in _refs(a0, names) if n not in front]
    prompt = front[0] if front else names[0]
    rest = [n for n in names if n != prompt]
    answer = back[0] if back else (rest[0] if rest else prompt)
    order = [prompt] + ([answer] if answer != prompt else []) + [n for n in names if n not in (prompt, answer)]
    reverse_ord = None
    for ord_, q, a in templates[1:]:
        fq = _refs(q, names)
        if fq and fq[0] == answer and prompt in _refs(a, names):
            reverse_ord = ord_
            break
    return _NoteType(names, order, False, reverse_ord)


def _proto_fields(blob: bytes) -> dict[int, list[Any]]:
    """Minimal protobuf reader: field number -> values (varints as int, others as bytes)."""
    out: dict[int, list[Any]] = {}
    i = 0

    def varint() -> int:
        nonlocal i
        shift = result = 0
        while True:
            b = blob[i]
            i += 1
            result |= (b & 0x7F) << shift
            if not b & 0x80:
                return result
            shift += 7

    while i < len(blob):
        key = varint()
        num, wire = key >> 3, key & 7
        if wire == 0:
            val: Any = varint()
        elif wire == 2:
            n = varint()
            val = blob[i : i + n]
            i += n
        elif wire == 1:
            val, i = blob[i : i + 8], i + 8
        elif wire == 5:
            val, i = blob[i : i + 4], i + 4
        else:
            break
        out.setdefault(num, []).append(val)
    return out


def _note_types(conn: sqlite3.Connection) -> dict[int, _NoteType]:
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    out: dict[int, _NoteType] = {}
    if "notetypes" in tables:  # schema 18
        for nt in conn.execute("SELECT id, name, config FROM notetypes"):
            cfg = _proto_fields(nt["config"] or b"")
            cloze = cfg.get(1, [0])[0] == 1
            css = cfg.get(3, [b""])[0].decode("utf-8", "replace")
            names = [r["name"] for r in conn.execute("SELECT name FROM fields WHERE ntid=? ORDER BY ord", (nt["id"],))]
            tmpls = []
            for t in conn.execute("SELECT ord, name, config FROM templates WHERE ntid=?", (nt["id"],)):
                tc = _proto_fields(t["config"] or b"")
                q = tc.get(1, [b""])[0].decode("utf-8", "replace")
                a = tc.get(2, [b""])[0].decode("utf-8", "replace")
                tmpls.append((t["ord"], t["name"], q, a))
            out[nt["id"]] = _note_type(nt["id"], nt["name"], names, tmpls, cloze, css)
    else:  # schema 11
        models = json.loads(conn.execute("SELECT models FROM col").fetchone()[0] or "{}")
        for m in models.values():
            names = [f["name"] for f in sorted(m["flds"], key=lambda f: f["ord"])]
            tmpls = [(t["ord"], t.get("name", ""), t.get("qfmt", ""), t.get("afmt", "")) for t in m.get("tmpls", [])]
            out[int(m["id"])] = _note_type(int(m["id"]), m["name"], names, tmpls, m.get("type") == 1, m.get("css", ""))
    return out


def _deck_names(conn: sqlite3.Connection) -> dict[int, str]:
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "decks" in tables:
        return {r["id"]: r["name"].replace(SEP, "::") for r in conn.execute("SELECT id, name FROM decks")}
    decks = json.loads(conn.execute("SELECT decks FROM col").fetchone()[0] or "{}")
    return {int(d["id"]): d["name"] for d in decks.values()}


# ---------- text ----------

_SOUND = re.compile(r"\[sound:[^\]]*\]")
_IMG = re.compile(r"<img\b[^>]*>", re.I)
_BREAK = re.compile(r"<br\s*/?>|</(div|p|li|tr|h\d)>", re.I)
_TAG = re.compile(r"<[^>]+>")
_CLOZE = re.compile(r"\{\{c\d+::(.*?)(?:::(.*?))?\}\}", re.S)


def html_to_text(value: str) -> str:
    value = _SOUND.sub("", value)
    value = _IMG.sub("", value)
    value = _BREAK.sub("\n", value)
    value = _TAG.sub("", value)
    value = html.unescape(value).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in value.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def has_media(value: str) -> bool:
    return bool(_SOUND.search(value) or _IMG.search(value))


def cloze_sides(text: str) -> tuple[str, str]:
    """(question with gaps, full sentence) for a cloze note."""
    question = _CLOZE.sub(lambda m: f"[{m.group(2)}]" if m.group(2) else "[...]", text)
    answer = _CLOZE.sub(lambda m: m.group(1), text)
    return question, answer


# ---------- scheduling ----------


def _day_to_due(user: Mapping[str, Any], crt: int, day: int) -> datetime:
    """Anki review due (days since collection creation) -> start of that study day for the user."""
    tz = srs.tz_of(user)
    local = datetime.fromtimestamp(crt + day * 86400, timezone.utc).astimezone(tz)
    h, m = (int(x) for x in user["quiet_end"].split(":"))
    start = local.replace(hour=h % 24, minute=m, second=0, microsecond=0)
    return start.astimezone(timezone.utc)


def _replay(scheduler: Scheduler, reviews: list[dict[str, Any]]) -> tuple[float | None, float | None]:
    """Stability and difficulty from an Anki review history, for cards without FSRS data."""
    card = Card(state=State.Learning, step=0)
    seen = False
    for r in reviews:
        if r["ease"] not in (1, 2, 3, 4) or r["type"] not in (0, 1, 2, 3):
            continue
        when = datetime.fromtimestamp(r["id"] / 1000, timezone.utc)
        if card.last_review and when < card.last_review:
            continue
        card, _ = scheduler.review_card(card, Rating(r["ease"]), review_datetime=when)
        seen = True
    return (card.stability, card.difficulty) if seen else (None, None)


def _card_sched(
    row: sqlite3.Row, crt: int, user: Mapping[str, Any], reviews: list[dict[str, Any]], scheduler: Scheduler
) -> dict[str, Any]:
    ctype = row["type"]
    if ctype == 0:
        return {"state": srs.NEW, "step": None, "stability": None, "difficulty": None, "due": None, "last_review": None}
    due_raw = row["odue"] if row["odid"] else row["due"]
    if ctype in (1, 3) and due_raw > 1_000_000_000:  # learning step, due as a timestamp
        due = datetime.fromtimestamp(due_raw, timezone.utc)
    else:  # due as a day number
        due = _day_to_due(user, crt, due_raw)
    try:
        data = json.loads(row["data"] or "{}")
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    stability, difficulty = data.get("s"), data.get("d")
    if stability is None or difficulty is None:
        stability, difficulty = _replay(scheduler, reviews)
    if (stability is None or difficulty is None) and ctype in (2, 3):
        stability = float(max(row["ivl"], 1))
        factor = row["factor"] or 2500
        difficulty = min(10.0, max(1.0, 5 + (2500 - factor) / 200))
    if data.get("lrt"):
        last = datetime.fromtimestamp(int(data["lrt"]), timezone.utc)
    elif reviews:
        last = datetime.fromtimestamp(reviews[-1]["id"] / 1000, timezone.utc)
    else:
        last = due - timedelta(days=max(row["ivl"], 1)) if ctype == 2 else due
    return {
        "state": {1: 1, 2: 2, 3: 3}[ctype],
        "step": None if ctype == 2 else 0,
        "stability": stability,
        "difficulty": difficulty,
        "due": iso(due),
        "last_review": iso(last),
    }


# ---------- the collection ----------


def _read_collection(conn: sqlite3.Connection, user: Mapping[str, Any], fallback_name: str) -> Package:
    crt = conn.execute("SELECT crt FROM col").fetchone()[0]
    types = _note_types(conn)
    deck_names = _deck_names(conn)
    scheduler = srs.scheduler_for(user)

    revlog: dict[int, list[dict[str, Any]]] = {}
    for r in conn.execute("SELECT id, cid, ease, ivl, lastIvl, factor, time, type FROM revlog ORDER BY id"):
        revlog.setdefault(r["cid"], []).append(dict(r))

    cards_by_note: dict[int, list[sqlite3.Row]] = {}
    for c in conn.execute("SELECT * FROM cards ORDER BY nid, ord"):
        cards_by_note.setdefault(c["nid"], []).append(c)

    pkg = Package(decks=[])
    decks: dict[int, ImportedDeck] = {}
    order: dict[int, list[tuple[tuple, ImportedNote]]] = {}
    for n in conn.execute("SELECT id, guid, mid, flds, tags FROM notes ORDER BY id"):
        nt = types.get(n["mid"])
        cards = cards_by_note.get(n["id"], [])
        if nt is None or not cards:
            continue
        raw = n["flds"].split(SEP)
        if any(has_media(v) for v in raw):
            pkg.media += 1
        values = {name: html_to_text(raw[i]) if i < len(raw) else "" for i, name in enumerate(nt.names)}
        if nt.cloze:
            q, a = cloze_sides(values.get("Text", ""))
            values["Text"], values["Answer"] = q, a

        kept: list[ImportedCard] = []
        deck_id = None
        for c in cards:
            if c["queue"] == -1:
                pkg.suspended += 1
                continue
            if nt.cloze:
                ord_ = 0 if not kept else None
            elif c["ord"] == 0:
                ord_ = 0
            elif c["ord"] == nt.reverse_ord:
                ord_ = 1
            else:
                ord_ = None
            if ord_ is None or any(k.ord == ord_ for k in kept):
                pkg.other_cards += 1
                continue
            reviews = revlog.get(c["id"], [])
            kept.append(ImportedCard(ord_, _card_sched(c, crt, user, reviews, scheduler), reviews, c["ord"]))
            if deck_id is None or ord_ == 0:
                deck_id = c["odid"] or c["did"]
        if not kept:
            continue
        if deck_id not in decks:
            decks[deck_id] = ImportedDeck(deck_names.get(deck_id) or "Default")
            order[deck_id] = []
        note = ImportedNote(n["guid"], values, sorted(kept, key=lambda k: k.ord), nt.order,
                            n["mid"], raw[: len(nt.names)], (n["tags"] or "").strip())
        pkg.notetypes[n["mid"]] = nt.model
        # New cards keep Anki's new-card order; studied ones go first.
        new_pos = min((c["due"] for c in cards if c["type"] == 0 and c["queue"] != -1), default=-1)
        order[deck_id].append(((new_pos, n["id"]), note))

    for deck_id, deck in decks.items():
        deck.notes = [note for _, note in sorted(order[deck_id], key=lambda x: x[0])]
        if deck.name == "Default":
            deck.name = fallback_name
        pkg.decks.append(deck)
    pkg.decks.sort(key=lambda d: d.name.lower())
    if not pkg.decks:
        raise ApkgError("I couldn't find any cards in that file.")
    return pkg


# ====================================================================== importing


def field_map(src_fields: list[str], dst_fields: list[str]) -> dict[str, str]:
    """Which deck field each imported field goes to: same name first, then
    prompt -> prompt and answer -> answer. Fields without a place are left out."""
    lower = {f.lower(): f for f in dst_fields}
    out: dict[str, str] = {}
    left: list[tuple[int, str]] = []
    for i, f in enumerate(src_fields):
        dst = lower.get(f.lower())
        if dst and dst not in out.values():
            out[f] = dst
        else:
            left.append((i, f))
    for i, f in left:
        if i < 2 and i < len(dst_fields) and dst_fields[i] not in out.values():
            out[f] = dst_fields[i]
    return out


def map_values(src_fields: list[str], values: Mapping[str, str], dst_fields: list[str]) -> tuple[dict[str, str], list[str]]:
    """Fit a note's values into a deck's fields (see field_map). Returns the values
    and the fields the deck lacks for values that had no place."""
    out = {f: "" for f in dst_fields}
    mapping = field_map(src_fields, dst_fields)
    missing: list[str] = []
    for f in src_fields:
        if f in mapping:
            out[mapping[f]] = values.get(f, "")
        elif values.get(f):
            out[f] = values[f]
            missing.append(f)
    return out, missing


@dataclass
class Plan:
    """What an import into one bot deck will do."""

    deck_name: str
    target_id: int | None  # None: create a new deck
    replace: bool
    fields: list[str]
    notes: list[tuple[ImportedNote, dict[str, str]]]
    duplicates: int = 0
    notetypes: dict[int, dict[str, Any]] = field(default_factory=dict)


def plan_import(store: Store, user_id: int, pkg: Package, target: int | None, replace: bool) -> list[Plan]:
    """Decide where every note goes. target None makes one new deck per Anki deck."""
    plans: list[Plan] = []
    if target is None:
        taken = {d["name"].lower() for d in store.decks(user_id)}
        for deck in pkg.decks:
            name = _free_name(deck.name, taken)
            taken.add(name.lower())
            fields = deck.fields
            notes = [(n, _fit(n, fields)) for n in deck.notes]
            plans.append(Plan(name, None, False, fields, notes, notetypes=pkg.notetypes))
        return plans

    dst = store.deck(target)
    if replace:
        fields = max(pkg.decks, key=lambda d: len(d.notes)).fields
    else:
        fields = deck_fields(dst)
    plan = Plan(dst["name"], target, replace, list(fields), [], notetypes=pkg.notetypes)
    seen_keys: set[str] = set()
    seen_guids: set[str] = set()
    if not replace:
        for n in store.notes_in_deck(target, 0, 1_000_000):
            seen_keys.add(n["sort_key"])
            if n["guid"]:
                seen_guids.add(n["guid"])
    for deck in pkg.decks:
        for n in deck.notes:
            vals = _fit(n, plan.fields)
            key = sort_key(plan.fields, vals)
            if not replace and (n.guid in seen_guids or (key and key in seen_keys)):
                plan.duplicates += 1
                continue
            seen_keys.add(key)
            seen_guids.add(n.guid)
            plan.notes.append((n, vals))
    return [plan]


def _fit(note: ImportedNote, fields: list[str]) -> dict[str, str]:
    """Note values in the deck's fields; fields the deck lacks are added to the list."""
    vals, missing = map_values(note.fields, note.values, fields)
    for f in missing:
        if f not in fields:
            fields.append(f)
    return {f: vals.get(f, "") for f in fields}


def _free_name(name: str, taken: set[str]) -> str:
    name = name.strip()[:60] or "Imported"
    if name.lower() not in taken:
        return name
    for i in range(2, 100):
        cand = f"{name} ({i})"
        if cand.lower() not in taken:
            return cand
    return f"{name} ({int(time.time())})"


def backlog(plans: list[Plan], user: Mapping[str, Any], now: datetime) -> int:
    """Studied cards in the plans that would be due today (overdue included)."""
    end = srs.day_start(user, now) + timedelta(days=1)
    return sum(
        1
        for p in plans
        for n, _ in p.notes
        for c in n.cards
        if c.sched["state"] != srs.NEW and parse(c.sched["due"]) < end
    )


def spread(plans: list[Plan], user: Mapping[str, Any], now: datetime, per_day: int) -> int:
    """Move overdue cards onto the coming study days, at most per_day a day.
    Cards most likely still remembered come first, so the fewest are lost to the wait;
    FSRS accounts for the longer gap when they are graded. Returns the number of days used."""
    if per_day <= 0:
        return 0
    today = srs.day_start(user, now)
    end = today + timedelta(days=1)
    scheduler = srs.scheduler_for(user)
    due = [c for p in plans for n, _ in p.notes for c in n.cards
           if c.sched["state"] != srs.NEW and parse(c.sched["due"]) < end]

    def recall(c: ImportedCard) -> float:
        s = c.sched
        if s["stability"] is None or s["last_review"] is None:
            return 0.0
        card = Card(state=State(s["state"]), step=s["step"], stability=s["stability"],
                    difficulty=s["difficulty"], due=parse(s["due"]), last_review=parse(s["last_review"]))
        return scheduler.get_card_retrievability(card, current_datetime=now)

    due.sort(key=recall, reverse=True)
    for i, c in enumerate(due):
        day = i // per_day
        if day:
            c.sched["due"] = iso(today + timedelta(days=day))
            if c.sched["state"] in (1, 3):  # a learning step pushed to another day starts it again
                c.sched["step"] = 0
    return (len(due) + per_day - 1) // per_day


@dataclass
class Result:
    deck_ids: list[int]
    deck_names: list[str]
    notes: int = 0
    cards: int = 0
    studied: int = 0
    duplicates: int = 0
    replaced: int = 0


def apply(store: Store, user_id: int, plans: list[Plan]) -> Result:
    """Write the plans to the database in one transaction."""
    res = Result([], [])
    now = iso(datetime.now(timezone.utc))
    store.x("BEGIN")
    try:
        used = {n.ntid for p in plans for n, _ in p.notes}
        for ntid, model in pkg_notetypes(plans).items():
            if ntid in used:
                store.x("INSERT OR REPLACE INTO anki_notetypes(user_id, id, data) VALUES (?,?,?)",
                        (user_id, ntid, json.dumps(model, ensure_ascii=False)))
        for p in plans:
            reverse = any(c.ord == 1 for n, _ in p.notes for c in n.cards)
            if p.target_id is None:
                deck_id = store.create_deck(user_id, p.deck_name, "basic", p.fields, reverse)
            else:
                deck_id = p.target_id
                if p.replace:
                    res.replaced += store.count_notes(deck_id)
                    store.x("DELETE FROM notes WHERE deck_id=?", (deck_id,))
                    store.update_deck(deck_id, fields=p.fields, reverse=int(reverse))
                else:
                    store.update_deck(deck_id, fields=p.fields)
                    reverse = bool(store.deck(deck_id)["reverse"])
            for note, vals in p.notes:
                anki = {
                    "nt": note.ntid, "raw": note.raw, "tags": note.tags,
                    "map": field_map(note.fields, p.fields),
                    "ords": {str(c.ord): c.anki_ord for c in note.cards},
                }
                note_id = store.x(
                    "INSERT INTO notes(deck_id, fields, sort_key, guid, anki, created_at) VALUES (?,?,?,?,?,?)",
                    (deck_id, json.dumps(vals, ensure_ascii=False), sort_key(p.fields, vals), note.guid,
                     json.dumps(anki, ensure_ascii=False) if note.ntid else None, now),
                )
                cards = list(note.cards)
                if reverse and p.target_id is not None and not p.replace and not any(c.ord == 1 for c in cards):
                    cards.append(ImportedCard(1, {"state": srs.NEW, "step": None, "stability": None,
                                                  "difficulty": None, "due": None, "last_review": None}))
                for c in cards:
                    s = c.sched
                    card_id = store.x(
                        "INSERT INTO cards(note_id, ord, state, step, stability, difficulty, due, last_review, created_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        (note_id, c.ord, s["state"], s["step"], s["stability"], s["difficulty"], s["due"],
                         s["last_review"], now),
                    )
                    _copy_reviews(store, card_id, c.reviews)
                    res.cards += 1
                    res.studied += s["state"] != srs.NEW
                res.notes += 1
            res.duplicates += p.duplicates
            res.deck_ids.append(deck_id)
            res.deck_names.append(p.deck_name)
        store.x("COMMIT")
    except Exception:
        store.x("ROLLBACK")
        raise
    return res


def pkg_notetypes(plans: list[Plan]) -> dict[int, dict[str, Any]]:
    return {k: v for p in plans for k, v in p.notetypes.items()}


def _copy_reviews(store: Store, card_id: int, reviews: list[dict[str, Any]]) -> None:
    state = srs.NEW
    for r in reviews:
        if r["ease"] not in (1, 2, 3, 4):
            continue
        after = 2 if r["ivl"] > 0 else (3 if r["type"] == 2 else 1)
        when = datetime.fromtimestamp(r["id"] / 1000, timezone.utc)
        anki = {k: r[k] for k in ("ivl", "lastIvl", "factor", "time", "type")}
        store.add_review(card_id, r["ease"], when, {"state": state}, {"state": after, "anki": anki}, "anki", None)
        state = after


# ====================================================================== exporting


def _stable_id(*parts: Any) -> int:
    h = hashlib.sha1(json.dumps(parts).encode()).digest()
    return 1_400_000_000_000 + int.from_bytes(h[:5], "big") % 100_000_000_000


def _checksum(text: str) -> int:
    return int(hashlib.sha1(text.encode()).hexdigest()[:8], 16)


def _to_html(text: str) -> str:
    return html.escape(text or "", quote=False).replace("\n", "<br>")


_SCHEMA_11 = """
CREATE TABLE col (id integer PRIMARY KEY, crt integer NOT NULL, mod integer NOT NULL, scm integer NOT NULL,
  ver integer NOT NULL, dty integer NOT NULL, usn integer NOT NULL, ls integer NOT NULL, conf text NOT NULL,
  models text NOT NULL, decks text NOT NULL, dconf text NOT NULL, tags text NOT NULL);
CREATE TABLE notes (id integer PRIMARY KEY, guid text NOT NULL, mid integer NOT NULL, mod integer NOT NULL,
  usn integer NOT NULL, tags text NOT NULL, flds text NOT NULL, sfld integer NOT NULL, csum integer NOT NULL,
  flags integer NOT NULL, data text NOT NULL);
CREATE TABLE cards (id integer PRIMARY KEY, nid integer NOT NULL, did integer NOT NULL, ord integer NOT NULL,
  mod integer NOT NULL, usn integer NOT NULL, type integer NOT NULL, queue integer NOT NULL, due integer NOT NULL,
  ivl integer NOT NULL, factor integer NOT NULL, reps integer NOT NULL, lapses integer NOT NULL,
  left integer NOT NULL, odue integer NOT NULL, odid integer NOT NULL, flags integer NOT NULL, data text NOT NULL);
CREATE TABLE revlog (id integer PRIMARY KEY, cid integer NOT NULL, usn integer NOT NULL, ease integer NOT NULL,
  ivl integer NOT NULL, lastIvl integer NOT NULL, factor integer NOT NULL, time integer NOT NULL,
  type integer NOT NULL);
CREATE TABLE graves (usn integer NOT NULL, oid integer NOT NULL, type integer NOT NULL);
CREATE INDEX ix_notes_usn ON notes (usn);
CREATE INDEX ix_cards_usn ON cards (usn);
CREATE INDEX ix_revlog_usn ON revlog (usn);
CREATE INDEX ix_cards_nid ON cards (nid);
CREATE INDEX ix_cards_sched ON cards (did, queue, due);
CREATE INDEX ix_revlog_cid ON revlog (cid);
CREATE INDEX ix_notes_csum ON notes (csum);
"""


def _model(model_id: int, name: str, fields: list[str], reverse: bool, deck_id: int) -> dict[str, Any]:
    prompt, answer = fields[0], fields[1] if len(fields) > 1 else fields[0]
    extras = "".join(f"{{{{#{f}}}}}<br><br><i>{html.escape(f)}:</i> {{{{{f}}}}}{{{{/{f}}}}}" for f in fields[2:])
    tmpls = [
        {"name": "Card 1", "ord": 0, "qfmt": f"{{{{{prompt}}}}}",
         "afmt": f"{{{{FrontSide}}}}\n\n<hr id=answer>\n\n{{{{{answer}}}}}{extras}"},
    ]
    if reverse:
        tmpls.append({"name": "Card 2", "ord": 1, "qfmt": f"{{{{{answer}}}}}",
                      "afmt": f"{{{{FrontSide}}}}\n\n<hr id=answer>\n\n{{{{{prompt}}}}}{extras}"})
    for t in tmpls:
        t.update({"bqfmt": "", "bafmt": "", "did": None, "bfont": "", "bsize": 0})
    return {
        "id": model_id, "name": name, "type": 0, "mod": int(time.time()), "usn": -1, "sortf": 0,
        "did": deck_id, "tmpls": tmpls,
        "flds": [{"name": f, "ord": i, "sticky": False, "rtl": False, "font": "Arial", "size": 20,
                  "media": []} for i, f in enumerate(fields)],
        "css": ".card {\n font-family: arial;\n font-size: 20px;\n text-align: center;\n color: black;\n"
               " background-color: white;\n}\n",
        "latexPre": "", "latexPost": "", "latexsvg": False,
        "req": [[0, "any", [0]]] + ([[1, "any", [1]]] if reverse else []),
        "tags": [], "vers": [],
    }


def _deck_json(deck_id: int, name: str) -> dict[str, Any]:
    return {
        "id": deck_id, "name": name, "mod": int(time.time()), "usn": -1, "desc": "", "dyn": 0, "conf": 1,
        "collapsed": False, "browserCollapsed": False, "extendNew": 0, "extendRev": 0,
        "newToday": [0, 0], "revToday": [0, 0], "lrnToday": [0, 0], "timeToday": [0, 0],
    }


_DCONF = {
    "1": {
        "id": 1, "name": "Default", "mod": 0, "usn": 0, "maxTaken": 60, "autoplay": True, "timer": 0,
        "replayq": True, "dyn": False,
        "new": {"bury": False, "delays": [1.0, 10.0], "initialFactor": 2500, "ints": [1, 4, 0], "order": 1,
                "perDay": 20},
        "rev": {"bury": False, "ease4": 1.3, "ivlFct": 1.0, "maxIvl": 36500, "perDay": 200, "hardFactor": 1.2},
        "lapse": {"delays": [10.0], "leechAction": 1, "leechFails": 8, "minInt": 1, "mult": 0.0},
    }
}


def _ivl(delta: timedelta) -> int:
    """Anki's revlog interval: days when positive, seconds when negative."""
    secs = delta.total_seconds()
    return max(1, round(secs / 86400)) if secs >= 86400 else -max(1, int(secs))


def export_decks(store: Store, user: Mapping[str, Any], deck_ids: list[int], out: Path,
                 now: datetime | None = None) -> int:
    """Write the decks as an .apkg at `out`, with scheduling and review history.
    Returns the number of cards written."""
    now = now or datetime.now(timezone.utc)
    today = srs.day_start(user, now)
    crt = int(today.timestamp())
    tmp = Path(tempfile.mkdtemp())
    try:
        db_path = tmp / "collection.anki2"
        conn = sqlite3.connect(str(db_path))
        conn.executescript(_SCHEMA_11)
        models: dict[str, Any] = {}
        decks: dict[str, Any] = {"1": _deck_json(1, "Default")}
        used_ids: set[int] = set()
        n_cards = 0
        mod = int(now.timestamp())
        for deck_id in deck_ids:
            d = store.deck(deck_id)
            fields = deck_fields(d)
            reverse = bool(d["reverse"]) or bool(store.q1(
                "SELECT 1 FROM cards c JOIN notes n ON n.id=c.note_id WHERE n.deck_id=? AND c.ord=1", (deck_id,)))
            anki_deck = _stable_id("deck", deck_id)
            model_id = _stable_id("model", deck_id, fields, reverse)
            decks[str(anki_deck)] = _deck_json(anki_deck, d["name"])
            for n in store.notes_in_deck(deck_id, 0, 1_000_000):
                vals = note_fields(n["fields"])
                origin = _origin(store, user["id"], n)
                if origin:
                    # Imported from Anki: write it back as the same note type, so Anki updates
                    # the note it already has. Fields unchanged here keep Anki's formatting and media.
                    model, info = origin
                    mid = model["id"]
                    models.setdefault(str(mid), _model_from(model, anki_deck))
                    flds = _anki_fields(model, info, vals)
                    ords = {int(k): v for k, v in info.get("ords", {}).items()}
                    tags = f" {info['tags']} " if info.get("tags") else ""
                else:
                    mid = model_id
                    models.setdefault(str(mid), _model(model_id, d["name"], fields, reverse, anki_deck))
                    flds = [_to_html(vals.get(f, "")) for f in fields]
                    ords = {0: 0, 1: 1} if reverse else {0: 0}
                    tags = ""
                nid = _unique(int(parse(n["created_at"]).timestamp() * 1000), used_ids)
                guid = n["guid"] or f"macaw-{n['id']}"
                first = html_to_text(flds[0]) if flds else ""
                conn.execute(
                    "INSERT INTO notes VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (nid, guid, mid, mod, -1, tags, SEP.join(flds), first, _checksum(first), 0, ""),
                )
                for pos, c in enumerate(store.cards_for_note(n["id"])):
                    if c["ord"] not in ords:
                        continue
                    cid = _unique(nid + 1 + pos, used_ids)
                    _write_card(conn, c, cid, nid, anki_deck, ords[c["ord"]], user, today, crt, mod, n["id"])
                    _write_reviews(conn, store, c["id"], cid, used_ids)
                    n_cards += 1
        conf = {"nextPos": 1, "schedVer": 2, "sched2021": True, "curDeck": 1, "activeDecks": [1],
                "curModel": next(iter(models), None), "collapseTime": 1200, "creationOffset": 0}
        conn.execute(
            "INSERT INTO col VALUES (1,?,?,?,11,0,0,0,?,?,?,?,?)",
            (crt, mod * 1000, mod * 1000, json.dumps(conf), json.dumps(models), json.dumps(decks),
             json.dumps(_DCONF), "{}"),
        )
        conn.commit()
        conn.close()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(db_path, "collection.anki2")
            zf.writestr("media", "{}")
        return n_cards
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _unique(i: int, used: set[int]) -> int:
    while i in used:
        i += 1
    used.add(i)
    return i


def _origin(store: Store, user_id: int, note) -> tuple[dict[str, Any], dict[str, Any]] | None:
    if not note["anki"]:
        return None
    info = json.loads(note["anki"])
    row = store.q1("SELECT data FROM anki_notetypes WHERE user_id=? AND id=?", (user_id, info.get("nt")))
    return (json.loads(row["data"]), info) if row else None


def _anki_fields(model: dict[str, Any], info: dict[str, Any], vals: Mapping[str, str]) -> list[str]:
    raw = list(info.get("raw") or [])
    mapping = info.get("map") or {}
    out = []
    for i, name in enumerate(model["flds"]):
        original = raw[i] if i < len(raw) else ""
        dst = mapping.get(name)
        if model["type"] == 1 and name == "Text" or dst is None or dst not in vals:
            out.append(original)  # a cloze text can't be rebuilt from the plain question
        elif vals[dst] == html_to_text(original):
            out.append(original)
        else:
            out.append(_to_html(vals[dst]))
    return out


def _model_from(m: dict[str, Any], deck_id: int) -> dict[str, Any]:
    """Schema-11 JSON for a note type that came from Anki."""
    tmpls = [{"name": t["name"], "ord": t["ord"], "qfmt": t["qfmt"], "afmt": t["afmt"], "bqfmt": "", "bafmt": "",
              "did": None, "bfont": "", "bsize": 0} for t in m["tmpls"]]
    return {
        "id": m["id"], "name": m["name"], "type": m["type"], "mod": int(time.time()), "usn": -1, "sortf": 0,
        "did": deck_id, "tmpls": tmpls,
        "flds": [{"name": f, "ord": i, "sticky": False, "rtl": False, "font": "Arial", "size": 20, "media": []}
                 for i, f in enumerate(m["flds"])],
        "css": m.get("css", ""), "latexPre": "", "latexPost": "", "latexsvg": False,
        "req": [[t["ord"], "any", [0]] for t in m["tmpls"]], "tags": [], "vers": [],
    }


def _write_card(conn, c, cid, nid, did, ord_, user, today, crt, mod, pos) -> None:
    state = c["state"]
    due_dt = parse(c["due"])
    buried = parse(c["buried_until"])
    if due_dt and buried and buried > due_dt:
        due_dt = buried
    last = parse(c["last_review"])
    data: dict[str, Any] = {}
    if c["stability"] is not None and c["difficulty"] is not None:
        data = {"s": round(c["stability"], 4), "d": round(c["difficulty"], 3)}
        if last:
            data["lrt"] = int(last.timestamp())
    if state == srs.NEW or due_dt is None:
        ctype, queue, due, ivl = 0, 0, pos, 0
    elif state == 2:
        ctype, queue = 2, 2
        due = (srs.day_start(user, due_dt) - today).days
        ivl = max(1, round((due_dt - last).total_seconds() / 86400)) if last else 1
    else:
        ctype, queue = state, 1
        due, ivl = int(due_dt.timestamp()), 0
    conn.execute(
        "INSERT INTO cards VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (cid, nid, did, ord_, mod, -1, ctype, queue, due, ivl, 2500 if ctype == 2 else 0, 0, 0,
         1 if ctype in (1, 3) else 0, 0, 0, 0, json.dumps(data) if data else ""),
    )


def _write_reviews(conn, store: Store, card_id: int, cid: int, used: set[int]) -> None:
    for r in store.q("SELECT * FROM review_log WHERE card_id=? ORDER BY id", (card_id,)):
        before, after = json.loads(r["before"]), json.loads(r["after"])
        when = parse(r["reviewed_at"])
        if "anki" in after:
            a = after["anki"]
            ivl, last_ivl, factor, took, rtype = a["ivl"], a["lastIvl"], a["factor"], a["time"], a["type"]
        else:
            due = parse(after.get("due"))
            ivl = _ivl(due - when) if due else 0
            prev_due, prev_last = parse(before.get("due")), parse(before.get("last_review"))
            last_ivl = _ivl(prev_due - prev_last) if prev_due and prev_last else 0
            factor, took = 2500, 0
            rtype = {0: 0, 1: 0, 2: 1, 3: 2}.get(before.get("state"), 1)
        rid = _unique(int(when.timestamp() * 1000), used)
        conn.execute("INSERT INTO revlog VALUES (?,?,?,?,?,?,?,?,?)",
                     (rid, cid, -1, r["rating"], ivl, last_ivl, factor, took, rtype))

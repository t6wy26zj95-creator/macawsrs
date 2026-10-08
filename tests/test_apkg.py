"""Anki import/export. The fixtures were made with the real Anki library (anki 26.9):
a "Spanish" deck with Spanish::Vocab (Basic and reversed, a custom note type whose
prompt is its second field) and Spanish::Grammar (Basic, Cloze, one suspended card),
some reviews, one card overdue by 300 days. Exported three ways: the current
format (collection.anki21b), "support older Anki versions" (collection.anki21)
with the SM-2 scheduler, and a full collection backup (.colpkg)."""

from __future__ import annotations

import json
import sqlite3
import zipfile
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from macaw import apkg, srs
from macaw.bot.transfer import Transfer
from macaw.db import Store, iso, parse
from macaw.templates import deck_fields, note_fields

from .conftest import UID, at

FIX = Path(__file__).parent / "fixtures"
FILES = ["fsrs-modern.apkg", "sm2-legacy.apkg", "fsrs-full.colpkg"]
NOW = at("2026-10-08 18:00")


def _notes(pkg, deck):
    d = next(d for d in pkg.decks if d.name == deck)
    return {n.values[n.fields[0]]: n for n in d.notes}


@pytest.mark.parametrize("name", FILES)
def test_reads_every_anki_format(name, user):
    pkg = apkg.read_package(FIX / name, user)
    assert [d.name for d in pkg.decks] == ["Spanish::Grammar", "Spanish::Vocab"]
    assert pkg.suspended == 1
    assert pkg.other_cards == 1  # the second cloze gap; one question per note
    assert pkg.media == 2
    vocab = _notes(pkg, "Spanish::Vocab")
    # HTML and sound tags become plain text
    assert vocab["la casa"].values["Back"] == "the house"
    assert vocab["el perro"].values["Back"] == "the dog\n(animal)"
    assert vocab["el árbol"].values["Front"] == "el árbol"
    # the custom note type shows Word on the front, so Word is the prompt
    eat = vocab["comer"]
    assert eat.fields[:3] == ["Word", "Meaning", "Example"]
    assert eat.values["Meaning"] == "to eat"
    # both directions of a reversed note, with Anki's scheduling
    perro = vocab["el perro"]
    assert [c.ord for c in perro.cards] == [0, 1]
    forward = perro.cards[0].sched
    assert forward["state"] == 2
    # due 300 days before the collection was made, at the start of the user's study day
    # (08:00 in Berlin)
    assert parse(forward["due"]) == at("2025-12-12 07:00")
    assert forward["stability"] and forward["difficulty"]
    grammar = _notes(pkg, "Spanish::Grammar")
    cloze = grammar["Yo [...] de España y [verb] cansado."]
    assert cloze.values["Answer"] == "Yo soy de España y estoy cansado."
    assert cloze.cards[0].sched["state"] == srs.NEW
    assert "suspended one" not in grammar


def test_not_an_anki_file(tmp_path, user):
    bad = tmp_path / "x.apkg"
    bad.write_text("hello")
    with pytest.raises(apkg.ApkgError):
        apkg.read_package(bad, user)
    empty = tmp_path / "y.apkg"
    with zipfile.ZipFile(empty, "w") as zf:
        zf.writestr("media", "{}")
    with pytest.raises(apkg.ApkgError):
        apkg.read_package(empty, user)


def test_import_as_new_decks_keeps_progress(store, user):
    pkg = apkg.read_package(FIX / "fsrs-modern.apkg", user)
    res = apkg.apply(store, UID, apkg.plan_import(store, UID, pkg, None, False))
    assert res.deck_names == ["Spanish::Grammar", "Spanish::Vocab"]
    vocab = store.deck_by_name(UID, "Spanish::Vocab")
    assert deck_fields(vocab) == ["Front", "Back", "Example"]  # the custom note's extra field is kept
    assert vocab["reverse"] == 1
    eat = store.search_notes(UID, "comer")[0]
    assert note_fields(eat["fields"]) == {"Front": "comer", "Back": "to eat", "Example": "Quiero comer."}
    assert eat["guid"]
    # the overdue card is due now, the review history came along
    queue = srs.due_queue(store, user, NOW)
    assert any("el perro" in c["note_fields"] for c in queue)
    n_logs = store.q1("SELECT COUNT(*) AS n FROM review_log WHERE source='anki'")["n"]
    assert n_logs == 12
    # importing the same file again makes new decks with free names
    res2 = apkg.apply(store, UID, apkg.plan_import(store, UID, pkg, None, False))
    assert res2.deck_names == ["Spanish::Grammar (2)", "Spanish::Vocab (2)"]


def test_merge_on_top_skips_duplicates_and_maps_fields(store, user):
    deck_id = store.create_deck(UID, "Spanish", "vocabulary", ["word", "meaning", "example", "notes"], reverse=True)
    store.add_note(deck_id, {"word": "la casa", "meaning": "house"}, "la casa", reverse=True)
    pkg = apkg.read_package(FIX / "fsrs-modern.apkg", user)
    plans = apkg.plan_import(store, UID, pkg, deck_id, replace=False)
    assert plans[0].duplicates == 1
    res = apkg.apply(store, UID, plans)
    assert res.notes == 6
    assert store.count_notes(deck_id) == 7
    # fields match by name, else prompt to prompt and answer to answer; anything left gets its own field
    assert deck_fields(store.deck(deck_id)) == ["word", "meaning", "example", "notes", "Back Extra"]
    eat = store.search_notes(UID, "comer")[0]
    assert note_fields(eat["fields"])["example"] == "Quiero comer."
    cloze = store.search_notes(UID, "cansado")[0]
    # the deck is reversed, so notes without a reverse card get a new one
    assert [c["ord"] for c in store.cards_for_note(cloze["id"])] == [0, 1]
    # adding the same file again adds nothing
    again = apkg.plan_import(store, UID, pkg, deck_id, replace=False)
    assert again[0].notes == [] and again[0].duplicates == 7


def test_replace_swaps_the_deck_contents(store, user):
    deck_id = store.create_deck(UID, "Spanish", "vocabulary", ["word", "meaning", "example", "notes"])
    store.add_note(deck_id, {"word": "old", "meaning": "gone"}, "old", reverse=False)
    pkg = apkg.read_package(FIX / "sm2-legacy.apkg", user)
    res = apkg.apply(store, UID, apkg.plan_import(store, UID, pkg, deck_id, replace=True))
    assert res.replaced == 1
    assert store.count_notes(deck_id) == 7
    assert not store.search_notes(UID, "old")
    d = store.deck(deck_id)
    assert d["name"] == "Spanish" and d["reverse"] == 1
    assert deck_fields(d)[:2] == ["Front", "Back"]


def _backlog_plan(n: int, now) -> list[apkg.Plan]:
    notes = []
    for i in range(n):
        last = now - timedelta(days=400)
        sched = {"state": 2, "step": None, "stability": 1.0 + i, "difficulty": 5.0,
                 "due": iso(now - timedelta(days=300)), "last_review": iso(last)}
        card = apkg.ImportedCard(0, sched)
        notes.append((apkg.ImportedNote(f"g{i}", {"Front": f"w{i}", "Back": "x"}, [card], ["Front", "Back"]),
                      {"Front": f"w{i}", "Back": "x"}))
    return [apkg.Plan("Old", None, False, ["Front", "Back"], notes)]


def test_overdue_pile_is_spread_over_days(store, user):
    plans = _backlog_plan(95, NOW)
    assert apkg.backlog(plans, user, NOW) == 95
    assert apkg.spread(plans, user, NOW, 25) == 4
    apkg.apply(store, UID, plans)
    due = srs.due_queue(store, user, NOW)
    assert len(due) == 25
    # the most stable cards (most likely remembered) come first
    assert {json.loads(c["note_fields"])["Front"] for c in due} == {f"w{i}" for i in range(70, 95)}
    tomorrow = srs.due_queue(store, user, NOW + timedelta(days=1))
    assert len(tomorrow) == 50  # nobody reviewed today's, so they pile onto tomorrow's
    upcoming = srs.upcoming(store, user, NOW)
    assert [n for _, n in upcoming] == [25, 25, 20]


def test_keep_all_due_does_not_move_cards(user):
    plans = _backlog_plan(30, NOW)
    assert apkg.spread(plans, user, NOW, 0) == 0
    assert apkg.backlog(plans, user, NOW) == 30


def test_export_round_trip(store, user, tmp_path):
    pkg = apkg.read_package(FIX / "fsrs-modern.apkg", user)
    apkg.apply(store, UID, apkg.plan_import(store, UID, pkg, None, False))
    vocab = store.deck_by_name(UID, "Spanish::Vocab")
    store.add_note(vocab["id"], {"Front": "<b>x</b> & y", "Back": "two\nlines"}, "x y", reverse=True)
    out = tmp_path / "out.apkg"
    n = apkg.export_decks(store, user, [vocab["id"]], out)
    assert n == 11  # 5 notes x 2 directions, minus comer's missing reverse card, plus the new note's 2
    with zipfile.ZipFile(out) as zf:
        assert set(zf.namelist()) == {"collection.anki2", "media"}

    back = apkg.read_package(out, user)
    assert [d.name for d in back.decks] == ["Spanish::Vocab"]
    notes = _notes(back, "Spanish::Vocab")
    assert notes["<b>x</b> & y"].values["Back"] == "two\nlines"
    original = _notes(pkg, "Spanish::Vocab")
    for word in ("el perro", "el gato", "el árbol", "la casa"):
        for a, b in zip(original[word].cards, notes[word].cards):
            assert a.ord == b.ord and a.sched["state"] == b.sched["state"]
            assert abs(a.sched["stability"] - b.sched["stability"]) < 0.01
            if a.sched["state"] == 2:
                assert a.sched["due"] == b.sched["due"]
    assert notes["el perro"].guid == original["el perro"].guid
    assert len(notes["el perro"].cards[1].reviews) == 2

    # Notes from Anki go back as their own note type, so Anki updates them in place;
    # untouched fields keep Anki's formatting and media, edited ones carry the edit.
    casa = store.search_notes(UID, "la casa")[0]
    store.update_note(casa["id"], {"Front": "la casa", "Back": "the home", "Example": ""}, "la casa")
    apkg.export_decks(store, user, [vocab["id"]], out)
    with zipfile.ZipFile(out) as zf:
        zf.extract("collection.anki2", tmp_path)
    conn = sqlite3.connect(tmp_path / "collection.anki2")
    flds = dict(conn.execute("SELECT substr(flds, 1, instr(flds, char(31)) - 1), flds FROM notes").fetchall())
    mids = {r[0] for r in conn.execute("SELECT mid FROM notes")}
    assert flds["<b>la casa</b>"] == "<b>la casa</b>\x1fthe home"
    assert flds["el árbol [sound:arbol.mp3]"] == "el árbol [sound:arbol.mp3]\x1fthe tree"
    assert flds["to eat"] == "to eat\x1fcomer\x1fQuiero comer.\x1f[sound:comer.mp3]"
    assert original["el perro"].ntid in mids and original["comer"].ntid in mids


def test_deck_stats(store, user):
    deck_id = store.create_deck(UID, "D", "basic", ["front", "back"])
    for i, (state, ivl) in enumerate([(0, 0), (1, 0), (2, 3), (2, 30), (2, 21)]):
        nid = store.add_note(deck_id, {"front": f"q{i}", "back": "a"}, f"q{i}", reverse=False)
        card = store.cards_for_note(nid)[0]
        if state:
            last = NOW - timedelta(days=1)
            store.set_card_schedule(card["id"], {"state": state, "step": 0, "stability": 3.0, "difficulty": 5.0,
                                                 "due": iso(last + timedelta(days=ivl)), "last_review": iso(last)})
    s = srs.deck_stats(store, user, NOW)[deck_id]
    assert (s["new"], s["learning"], s["young"], s["mature"]) == (1, 1, 1, 2)


def test_old_database_gets_guid_column(tmp_path):
    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, deck_id INTEGER NOT NULL, fields TEXT NOT NULL, "
                 "sort_key TEXT NOT NULL, created_at TEXT NOT NULL)")
    conn.commit()
    conn.close()
    s = Store(path)
    assert "guid" in {r["name"] for r in s.q("PRAGMA table_info(notes)")}


# ---------- the chat flow ----------


class FakeBot:
    def __init__(self, source: Path):
        self.source = source
        self.docs = []

    async def download(self, doc, destination):
        Path(destination).write_bytes(self.source.read_bytes())

    async def send_document(self, chat_id, document, caption=None):
        self.docs.append((document.filename, document.data, caption))


class Msg:
    def __init__(self, name="Spanish.apkg", size=1000):
        self.from_user = SimpleNamespace(id=UID)
        self.chat = SimpleNamespace(id=UID)
        self.document = SimpleNamespace(file_name=name, file_size=size)
        self.answers = []

    async def answer(self, text, reply_markup=None, **kw):
        self.answers.append((text, reply_markup))

    async def edit_text(self, text, reply_markup=None, **kw):
        self.answers.append((text, reply_markup))


class Cb:
    def __init__(self, data, message):
        self.data = data
        self.from_user = SimpleNamespace(id=UID)
        self.message = message

    async def answer(self, *a, **kw):
        pass


def _buttons(markup):
    return {b.text: b.callback_data for row in markup.inline_keyboard for b in row}


@pytest.mark.asyncio
async def test_chat_flow_merge_replace(store, tmp_path):
    deck_id = store.create_deck(UID, "Spanish", "vocabulary", ["word", "meaning", "example", "notes"])
    store.add_note(deck_id, {"word": "old", "meaning": "gone"}, "old", reverse=False)
    t = Transfer(store, FakeBot(FIX / "fsrs-modern.apkg"), tmp_path)
    m = Msg()
    await t.on_document(m)
    text, kb = m.answers[-1]
    assert "11 cards" in text and "9 already studied" in text
    assert set(_buttons(kb)) == {"Create 2 new decks", "Merge into one of my decks", "Cancel"}

    await t.on_callback(Cb("imp:merge", m))
    await t.on_callback(Cb(_buttons(m.answers[-1][1])["Spanish"], m))
    await t.on_callback(Cb(_buttons(m.answers[-1][1])["Replace"], m))
    assert "can't be undone" in m.answers[-1][0]
    await t.on_callback(Cb(_buttons(m.answers[-1][1])["Yes, replace"], m))
    done = m.answers[-1][0]
    assert done.startswith("Done. 11 cards imported into Spanish.")
    assert "Left out 1 suspended card." in done
    assert store.count_notes(deck_id) == 7
    assert not (tmp_path / "imports" / f"{UID}.apkg").exists()
    # pressing an old button after the import finds no file
    await t.on_callback(Cb("imp:c:n:a", m))
    assert "send it again" in m.answers[-1][0]


@pytest.mark.asyncio
async def test_chat_flow_new_deck_and_wrong_file(store, tmp_path):
    t = Transfer(store, FakeBot(FIX / "sm2-legacy.apkg"), tmp_path)
    m = Msg(name="notes.pdf")
    await t.on_document(m)
    assert "apkg" in m.answers[-1][0]
    m = Msg(size=30 * 1024 * 1024)
    await t.on_document(m)
    assert "20 MB" in m.answers[-1][0]
    m = Msg()
    await t.on_document(m)
    assert "Merge into one of my decks" not in _buttons(m.answers[-1][1])  # no decks yet
    await t.on_callback(Cb("imp:m:n:a", m))
    assert m.answers[-1][0].startswith("Done.")
    assert [d["name"] for d in store.decks(UID)] == ["Spanish::Grammar", "Spanish::Vocab"]
    assert any("Anki import" in r["text"] for r in store.recent_messages(UID))


@pytest.mark.asyncio
async def test_chat_flow_asks_to_spread_a_big_backlog(store, user, tmp_path, monkeypatch):
    t = Transfer(store, FakeBot(FIX / "fsrs-modern.apkg"), tmp_path)
    monkeypatch.setattr("macaw.bot.transfer.BACKLOG_ASK", 3)
    m = Msg()
    await t.on_document(m)
    await t.on_callback(Cb("imp:m:n:a", m))
    text, kb = m.answers[-1]
    assert "spread them" in text
    buttons = _buttons(kb)
    assert "Keep them all due today" in buttons
    await t.on_callback(Cb(buttons["Keep them all due today"], m))
    assert m.answers[-1][0].startswith("Done.")


@pytest.mark.asyncio
async def test_export_sends_a_file(store, user, tmp_path):
    bot = FakeBot(FIX / "fsrs-modern.apkg")
    t = Transfer(store, bot, tmp_path)
    assert t.export_menu(UID)[1] is None
    deck_id = store.create_deck(UID, "Words / mine", "vocabulary", ["word", "meaning", "example", "notes"])
    store.add_note(deck_id, {"word": "hola", "meaning": "hello"}, "hola", reverse=False)
    assert await t.send_export(UID, UID, str(deck_id)) is None
    filename, data, caption = bot.docs[-1]
    assert filename == "Words _ mine.apkg"
    assert caption.startswith("1 card ")
    out = tmp_path / "e.apkg"
    out.write_bytes(data)
    assert _notes(apkg.read_package(out, user), "Words / mine")["hola"].values["meaning"] == "hello"

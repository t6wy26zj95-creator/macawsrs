"""Anki import and export in the chat.

Importing: the user sends an .apkg/.colpkg file. It is kept on disk (one per
user) while they choose with buttons: new deck, or merge into one of their
decks (replace it entirely, or add on top and skip duplicates). If many
studied cards are overdue, they can spread them over the coming days.
Each button carries the choices made so far, so nothing else is stored.
Callback data starts with "imp:"; exports use "exp:".
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from aiogram.types import InlineKeyboardButton as Btn
from aiogram.types import InlineKeyboardMarkup as Kb

from .. import apkg
from ..db import Store

log = logging.getLogger(__name__)

EXTENSIONS = (".apkg", ".colpkg")
MAX_DOWNLOAD = 20 * 1024 * 1024  # the most the Telegram bot API lets a bot download
BACKLOG_ASK = 20  # ask about spreading overdue cards above this many
SPREAD_CHOICES = (10, 25, 50)


def _kb(rows: list[list[tuple[str, str]]]) -> Kb:
    return Kb(inline_keyboard=[[Btn(text=t, callback_data=d) for t, d in row] for row in rows])


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


class Transfer:
    def __init__(self, store: Store, bot: Bot, data_dir: Path):
        self.store = store
        self.bot = bot
        self.dir = data_dir / "imports"

    def _path(self, user_id: int) -> Path:
        return self.dir / f"{user_id}.apkg"

    def _name_path(self, user_id: int) -> Path:
        return self.dir / f"{user_id}.name"

    def _forget(self, user_id: int) -> None:
        for p in (self._path(user_id), self._name_path(user_id)):
            with contextlib.suppress(FileNotFoundError):
                p.unlink()

    async def _read(self, user_id: int) -> apkg.Package | None:
        path = self._path(user_id)
        if not path.exists():
            return None
        try:
            name = self._name_path(user_id).read_text().strip() or "Imported"
        except FileNotFoundError:
            name = "Imported"
        user = dict(self.store.get_user(user_id))
        return await asyncio.to_thread(apkg.read_package, path, user, name)

    # ================= receiving the file =================

    async def on_document(self, m: Message) -> None:
        uid = m.from_user.id
        doc = m.document
        name = doc.file_name or ""
        if not name.lower().endswith(EXTENSIONS):
            await m.answer("I can import Anki decks (.apkg files). For anything else, just write to me.")
            return
        if doc.file_size and doc.file_size > MAX_DOWNLOAD:
            await m.answer(
                "That file is over 20 MB, the most Telegram lets me download. In Anki, export the deck "
                "again with \"Include media\" turned off. I can't use audio or images anyway, so nothing is lost."
            )
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        await self.bot.download(doc, destination=self._path(uid))
        self._name_path(uid).write_text(re.sub(r"\.(apkg|colpkg)$", "", name, flags=re.I).strip())
        try:
            pkg = await self._read(uid)
        except apkg.ApkgError as e:
            self._forget(uid)
            await m.answer(str(e))
            return
        except Exception:
            log.exception("reading Anki package failed")
            self._forget(uid)
            await m.answer("Something went wrong reading that file. Is it an Anki deck export?")
            return
        text, kb = self._start_view(uid, pkg)
        await m.answer(text, reply_markup=kb, parse_mode=ParseMode.HTML)

    def _summary(self, pkg: apkg.Package) -> str:
        names = ", ".join(f"<b>{html.escape(d.name)}</b>" for d in pkg.decks)
        studied = pkg.studied_count
        text = f"Anki deck{'s' if len(pkg.decks) > 1 else ''} {names}: {_plural(pkg.card_count, 'card')}"
        if studied:
            text += f", {studied} already studied (their Anki progress comes along)"
        return text + "."

    def _start_view(self, uid: int, pkg: apkg.Package) -> tuple[str, Kb]:
        new_label = "Create a new deck" if len(pkg.decks) == 1 else f"Create {len(pkg.decks)} new decks"
        rows = [[(new_label, "imp:m:n:a")]]
        if self.store.decks(uid):
            rows.append([("Merge into one of my decks", "imp:merge")])
        rows.append([("Cancel", "imp:x")])
        return self._summary(pkg) + "\n\nWhat should I do with it?", _kb(rows)

    # ================= the buttons =================

    async def on_callback(self, c: CallbackQuery) -> None:
        uid = c.from_user.id
        parts = c.data.split(":")
        step = parts[1]
        if step == "x":
            self._forget(uid)
            await self._edit(c, "Import cancelled.", None)
            return
        try:
            pkg = await self._read(uid)
        except apkg.ApkgError as e:
            self._forget(uid)
            await self._edit(c, str(e), None)
            return
        if pkg is None:
            await self._edit(c, "I don't have that file anymore. Please send it again.", None)
            return

        if step == "home":
            await self._edit(c, *self._start_view(uid, pkg))
        elif step == "merge":
            rows = [[(d["name"][:60], f"imp:t:{d['id']}")] for d in self.store.decks(uid)]
            rows.append([("« Back", "imp:home")])
            await self._edit(c, "Which deck should it go into?", _kb(rows))
        elif step == "t":
            deck = self._own_deck(uid, parts[2])
            if deck is None:
                await self._edit(c, *self._start_view(uid, pkg))
                return
            n = self.store.count_notes(deck["id"])
            name = html.escape(deck["name"])
            text = (
                f"<b>{name}</b> has {_plural(n, 'card')}.\n\n"
                f"<b>Replace</b>: {name} ends up with only the imported cards; its current cards and their "
                "progress are deleted.\n"
                f"<b>Add on top</b>: the imported cards join the ones already there. Cards {name} already "
                "has are skipped."
            )
            await self._edit(c, text, _kb([
                [("Replace", f"imp:m:{deck['id']}:r"), ("Add on top", f"imp:m:{deck['id']}:a")],
                [("« Back", "imp:merge")],
            ]))
        elif step == "m":
            target, mode = parts[2], parts[3]
            if mode == "r":
                deck = self._own_deck(uid, target)
                if deck is None:
                    await self._edit(c, *self._start_view(uid, pkg))
                    return
                n = self.store.count_notes(deck["id"])
                text = (
                    f"Delete the {_plural(n, 'card')} in <b>{html.escape(deck['name'])}</b> and their progress, "
                    "and put the imported ones in their place? This can't be undone."
                )
                await self._edit(c, text, _kb([
                    [("Yes, replace", f"imp:c:{target}:r"), ("No", f"imp:t:{target}")],
                ]))
            else:
                await self._check(c, uid, pkg, target, mode)
        elif step == "c":
            await self._check(c, uid, pkg, parts[2], parts[3])
        elif step == "s":
            await self._run(c, uid, pkg, parts[2], parts[3], int(parts[4]))
        else:
            await c.answer()

    def _own_deck(self, uid: int, raw: str):
        if not raw.isdigit():
            return None
        d = self.store.deck(int(raw))
        return d if d and d["user_id"] == uid else None

    def _plans(self, uid: int, pkg: apkg.Package, target: str, mode: str) -> list[apkg.Plan] | None:
        if target == "n":
            return apkg.plan_import(self.store, uid, pkg, None, False)
        deck = self._own_deck(uid, target)
        if deck is None:
            return None
        return apkg.plan_import(self.store, uid, pkg, deck["id"], mode == "r")

    async def _check(self, c: CallbackQuery, uid: int, pkg: apkg.Package, target: str, mode: str) -> None:
        """Ask about spreading a backlog of overdue cards, or import straight away."""
        plans = self._plans(uid, pkg, target, mode)
        if plans is None:
            await self._edit(c, *self._start_view(uid, pkg))
            return
        user = dict(self.store.get_user(uid))
        n = apkg.backlog(plans, user, datetime.now(timezone.utc))
        if n <= BACKLOG_ASK:
            await self._run(c, uid, pkg, target, mode, 0)
            return
        rows = []
        for per_day in SPREAD_CHOICES:
            days = -(-n // per_day)
            if days > 1:
                rows.append([(f"{per_day} a day ({days} days)", f"imp:s:{target}:{mode}:{per_day}")])
        rows.append([("Keep them all due today", f"imp:s:{target}:{mode}:0")])
        text = (
            f"{_plural(n, 'card')} from this deck {'is' if n == 1 else 'are'} due for review, many of them "
            "overdue. Instead of all of them landing today, I can spread them over the next days.\n\n"
            "The cards you most likely still remember come first. When you answer a card late, the "
            "scheduler takes the longer gap into account, so the extra wait does no harm."
        )
        await self._edit(c, text, _kb(rows))

    async def _run(self, c: CallbackQuery, uid: int, pkg: apkg.Package, target: str, mode: str, per_day: int) -> None:
        plans = self._plans(uid, pkg, target, mode)
        if plans is None:
            await self._edit(c, *self._start_view(uid, pkg))
            return
        await self._edit(c, "Importing...", None)
        user = dict(self.store.get_user(uid))
        now = datetime.now(timezone.utc)
        overdue = apkg.backlog(plans, user, now) if per_day else 0
        days = apkg.spread(plans, user, now, per_day)
        res = apkg.apply(self.store, uid, plans)  # on the loop: the database connection is shared
        self._forget(uid)
        # A replaced deck may have held the card the user was being asked.
        st = self.store.state(uid)
        if st["active_card_id"] and self.store.card(st["active_card_id"]) is None:
            self.store.update_state(uid, active_card_id=None, asked_at=None)

        where = ", ".join(res.deck_names)
        lines = [f"Done. {_plural(res.cards, 'card')} imported into {where}."]
        if res.studied:
            lines.append(f"{res.studied} keep their Anki progress; {res.cards - res.studied} are new.")
        if res.replaced:
            lines.append(f"The {_plural(res.replaced, 'card')} that were there before are gone.")
        if res.duplicates:
            lines.append(f"Skipped {_plural(res.duplicates, 'card')} the deck already had.")
        if days > 1:
            lines.append(f"The {overdue} due cards are spread over the next {days} days.")
        if pkg.suspended:
            lines.append(f"Left out {pkg.suspended} suspended {'card' if pkg.suspended == 1 else 'cards'}.")
        if pkg.other_cards:
            lines.append(
                f"Left out {pkg.other_cards} extra card {'type' if pkg.other_cards == 1 else 'types'} "
                "I can't ask (I keep one card per note, plus a reverse card)."
            )
        if pkg.media:
            lines.append("Audio and images aren't supported, so they were left out.")
        text = "\n".join(lines)
        self.store.log_message(uid, "note", "Anki import: " + text)
        await self._edit(c, text, None)

    async def _edit(self, c: CallbackQuery, text: str, kb: Kb | None) -> None:
        with contextlib.suppress(TelegramBadRequest):
            await c.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        with contextlib.suppress(TelegramBadRequest):
            await c.answer()

    # ================= export =================

    def export_menu(self, uid: int) -> tuple[str, Kb | None]:
        decks = self.store.decks(uid)
        if not decks:
            return "You have no decks to export yet.", None
        rows = [[(d["name"][:60], f"exp:{d['id']}")] for d in decks]
        if len(decks) > 1:
            rows.append([("All decks", "exp:all")])
        return "Which deck should I export for Anki?", _kb(rows)

    async def send_export(self, chat_id: int, uid: int, which: str) -> str | None:
        """Send the deck(s) as an .apkg. Returns an error for the user, or None."""
        decks = self.store.decks(uid)
        if which == "all":
            chosen = decks
        else:
            chosen = [d for d in decks if str(d["id"]) == which]
        if not chosen:
            return "That deck is gone."
        user = dict(self.store.get_user(uid))
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "export.apkg"
            n = apkg.export_decks(self.store, user, [d["id"] for d in chosen], out)
            data = out.read_bytes()
        stem = chosen[0]["name"] if len(chosen) == 1 else "All decks"
        filename = re.sub(r"[^\w\- ]+", "_", stem).strip() + ".apkg"
        await self.bot.send_document(
            chat_id,
            BufferedInputFile(data, filename=filename),
            caption=f"{_plural(n, 'card')} with their review history. Open the file with Anki, "
            "AnkiMobile or AnkiDroid to import it.\n\nCards Anki already has get your edits, but Anki keeps "
            "its own progress for them. To take over the progress from here, delete the deck in Anki "
            "before importing.",
        )
        return None

    async def on_export_callback(self, c: CallbackQuery) -> None:
        which = c.data.split(":", 1)[1]
        await c.answer("Preparing the file...")
        err = await self.send_export(c.message.chat.id, c.from_user.id, which)
        if err:
            await self.bot.send_message(c.message.chat.id, err)

"""Telegram handlers and the background timer."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatAction, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message

from .. import pacing, srs
from ..agent import Action, Agent, ConfirmDelete, Preview, RatingNote, Text
from ..config import Config
from ..db import Store, iso, parse
from ..llm import LLMError
from ..prompts import GREETING
from . import menus, render

log = logging.getLogger(__name__)

TICK_SECONDS = 30
LLM_BACKOFF_MIN = 15
TG_LIMIT = 4000


class App:
    def __init__(self, config: Config, store: Store, agent: Agent, bot: Bot):
        self.config = config
        self.store = store
        self.agent = agent
        self.bot = bot
        self.locks: dict[int, asyncio.Lock] = {}
        self.router = Router()
        self._register()

    def lock(self, user_id: int) -> asyncio.Lock:
        return self.locks.setdefault(user_id, asyncio.Lock())

    # ================= sending =================

    async def send_actions(self, chat_id: int, actions: list[Action]) -> None:
        for a in actions:
            if isinstance(a, Text):
                for chunk in _chunks(a.text):
                    await self.bot.send_message(chat_id, chunk, parse_mode=None)
            elif isinstance(a, Preview):
                for old in a.replaces:
                    await self.remove_preview(chat_id, old)
                text, kb = render.preview(self.store, a.proposal_id)
                msg = await self.bot.send_message(chat_id, text, reply_markup=kb, parse_mode=ParseMode.HTML)
                self.store.update_proposal(a.proposal_id, message_id=msg.message_id)
            elif isinstance(a, RatingNote):
                text, kb = render.rating_note(self.store, a.log_id)
                await self.bot.send_message(chat_id, text, reply_markup=kb, parse_mode=ParseMode.HTML)
            elif isinstance(a, ConfirmDelete):
                text, kb = render.confirm_delete(self.store, a.note_id)
                await self.bot.send_message(chat_id, text, reply_markup=kb, parse_mode=ParseMode.HTML)

    async def remove_preview(self, chat_id: int, message_id: int, note: str = "Replaced by a newer version.") -> None:
        """Delete a card preview that is done with. Telegram only lets bots delete
        messages for 48 hours; older ones shrink to a single line instead."""
        try:
            await self.bot.delete_message(chat_id, message_id)
        except TelegramBadRequest:
            with contextlib.suppress(TelegramBadRequest):
                await self.bot.edit_message_text(text=note, chat_id=chat_id, message_id=message_id)

    @contextlib.asynccontextmanager
    async def typing(self, chat_id: int):
        async def loop():
            while True:
                with contextlib.suppress(Exception):
                    await self.bot.send_chat_action(chat_id, ChatAction.TYPING)
                await asyncio.sleep(4)

        task = asyncio.create_task(loop())
        try:
            yield
        finally:
            task.cancel()

    async def run_llm(self, user_id: int, chat_id: int, coro_factory, notify: bool) -> None:
        """Run an agent call with typing indicator and friendly errors."""
        try:
            async with self.typing(chat_id):
                actions = await coro_factory()
        except LLMError as e:
            log.warning("LLM unavailable: %s", e)
            self.store.update_state(
                user_id, llm_backoff_until=iso(_now() + timedelta(minutes=LLM_BACKOFF_MIN))
            )
            if notify:
                await self.bot.send_message(
                    chat_id,
                    "I can't reach Claude right now (maybe the Pro usage limit was hit). "
                    "Try again in a little while; your cards are safe.",
                )
            return
        await self.send_actions(chat_id, actions)

    # ================= handlers =================

    def _allowed(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self.config.owner_ids

    def _register(self) -> None:
        r = self.router

        @r.message(CommandStart())
        async def start(m: Message):
            if not self._allowed(m.from_user and m.from_user.id):
                await m.answer("Sorry, this is a private bot.")
                return
            self.store.ensure_user(m.from_user.id, m.chat.id, self.config.default_timezone)
            self.store.update_user(m.from_user.id, chat_id=m.chat.id)
            self.store.log_message(m.from_user.id, "bot", GREETING)
            await m.answer(GREETING, parse_mode=None)

        @r.message(Command("decks"))
        async def decks(m: Message):
            if not self._allowed(m.from_user and m.from_user.id):
                return
            self.store.ensure_user(m.from_user.id, m.chat.id, self.config.default_timezone)
            text, kb = menus.deck_list(self.store, m.from_user.id)
            await m.answer(text, reply_markup=kb, parse_mode=ParseMode.HTML)

        @r.message(Command("review"))
        async def review(m: Message):
            if not self._allowed(m.from_user and m.from_user.id):
                return
            await self._chat(m, "Let's review a card now.")

        @r.message(Command("settings"))
        async def settings(m: Message):
            if not self._allowed(m.from_user and m.from_user.id):
                return
            u = self.store.ensure_user(m.from_user.id, m.chat.id, self.config.default_timezone)
            await m.answer(
                f"Timezone: {u['timezone']}\n"
                f"Quiet hours: {u['quiet_start']}–{u['quiet_end']}\n"
                f"Cards per session: {u['cards_per_session']}\n"
                f"Reminders: up to {u['max_reminders']} a day, first after {u['first_reminder_min']} min\n"
                f"Desired retention: {u['desired_retention']}\n\n"
                "To change any of these, just tell me, e.g. \"I'm in Berlin\" or \"ask me 3 cards at a time\".",
                parse_mode=None,
            )

        @r.message(F.text)
        async def text(m: Message):
            if not self._allowed(m.from_user and m.from_user.id):
                await m.answer("Sorry, this is a private bot.")
                return
            uid = m.from_user.id
            self.store.ensure_user(uid, m.chat.id, self.config.default_timezone)
            pending = self.store.pending_input(uid)
            if pending and pending.get("kind") == "rename_deck":
                await self._finish_rename(m, pending)
                return
            await self._chat(m, m.text)

        @r.message()
        async def other(m: Message):
            if self._allowed(m.from_user and m.from_user.id):
                await m.answer("I can only read text for now.")

        @r.callback_query(F.data.startswith("p:"))
        async def proposal_cb(c: CallbackQuery):
            if not self._allowed(c.from_user.id):
                return
            _, action, pid = c.data.split(":")
            pid = int(pid)
            uid = c.from_user.id
            if action == "add":
                note = self.agent.add_proposal(pid, uid)
            elif action == "skip":
                note = self.agent.skip_proposal(pid, uid)
            else:
                self.agent.start_edit(pid, uid)
                await c.answer()
                await c.message.answer("What should I change?")
                self.store.log_message(uid, "bot", f"(preview #{pid}) What should I change?")
                return
            await c.answer(note)
            await self.remove_preview(c.message.chat.id, c.message.message_id, note)

        @r.callback_query(F.data.startswith("r:"))
        async def rating_cb(c: CallbackQuery):
            if not self._allowed(c.from_user.id):
                return
            _, log_id, rating = c.data.split(":")
            user = self.store.get_user(c.from_user.id)
            try:
                srs.regrade(self.store, user, int(log_id), int(rating))
            except ValueError as e:
                await c.answer(str(e), show_alert=True)
                return
            self.store.log_message(
                user["id"], "note", f"user changed the grade to {srs.RATING_NAMES[int(rating)]}"
            )
            await c.answer("Updated")
            text, kb = render.rating_note(self.store, int(log_id))
            with contextlib.suppress(TelegramBadRequest):
                await c.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)

        @r.callback_query(F.data.startswith("x:"))
        async def delete_cb(c: CallbackQuery):
            if not self._allowed(c.from_user.id):
                return
            _, action, note_id = c.data.split(":")
            note = self.store.note(int(note_id))
            if note is None:
                await c.answer("Already gone.")
                with contextlib.suppress(TelegramBadRequest):
                    await c.message.edit_reply_markup(reply_markup=None)
                return
            if action == "yes":
                self._delete_note(c.from_user.id, note["id"])
                await c.message.edit_text("Deleted.")
            else:
                await c.message.edit_text("Kept it.")
            await c.answer()

        @r.callback_query(F.data.startswith("m:"))
        async def menu_cb(c: CallbackQuery):
            if not self._allowed(c.from_user.id):
                return
            await self._menu(c)

    async def _chat(self, m: Message, text: str) -> None:
        uid = m.from_user.id
        async with self.lock(uid):
            await self.run_llm(uid, m.chat.id, lambda: self.agent.on_user_message(uid, text), notify=True)

    def _delete_note(self, user_id: int, note_id: int) -> None:
        st = self.store.state(user_id)
        card_ids = [c["id"] for c in self.store.cards_for_note(note_id)]
        if st["active_card_id"] in card_ids:
            self.store.update_state(user_id, active_card_id=None, asked_at=None)
        self.store.delete_note(note_id)
        self.store.log_message(user_id, "note", f"note #{note_id} deleted")

    async def _finish_rename(self, m: Message, pending: dict[str, Any]) -> None:
        uid = m.from_user.id
        self.store.set_pending_input(uid, None)
        deck = self.store.deck(pending["deck_id"])
        name = (m.text or "").strip()
        if deck is None or deck["user_id"] != uid:
            return
        if not name or len(name) > 60:
            await m.answer("That name doesn't work; keep it under 60 characters.")
            return
        other = self.store.deck_by_name(uid, name)
        if other and other["id"] != deck["id"]:
            await m.answer("You already have a deck with that name.")
            return
        self.store.update_deck(deck["id"], name=name)
        with contextlib.suppress(TelegramBadRequest):
            await m.delete()
        text, kb = menus.deck_view(self.store, deck["id"])
        with contextlib.suppress(TelegramBadRequest):
            await self.bot.edit_message_text(
                text=text, chat_id=m.chat.id, message_id=pending["message_id"],
                reply_markup=kb, parse_mode=ParseMode.HTML,
            )

    async def _menu(self, c: CallbackQuery) -> None:
        uid = c.from_user.id
        parts = c.data.split(":")
        action = parts[1]
        ids = [int(p) for p in parts[2:] if p.lstrip("-").isdigit()]

        def owned_deck(deck_id: int):
            d = self.store.deck(deck_id)
            return d if d and d["user_id"] == uid else None

        def owned_note(note_id: int):
            n = self.store.note(note_id)
            return n if n and owned_deck(n["deck_id"]) else None

        view = None
        if action == "close":
            with contextlib.suppress(TelegramBadRequest):
                await c.message.delete()
            await c.answer()
            return
        if action == "list":
            view = menus.deck_list(self.store, uid)
        elif action in ("deck", "cards", "lim", "limset", "tmpl", "rev", "ren", "del", "delok"):
            d = owned_deck(ids[0]) if ids else None
            if d is None:
                view = menus.deck_list(self.store, uid)
            elif action == "deck":
                self.store.set_pending_input(uid, None)
                view = menus.deck_view(self.store, d["id"])
            elif action == "cards":
                view = menus.card_list(self.store, d["id"], ids[1] if len(ids) > 1 else 0)
            elif action == "lim":
                view = menus.limits(self.store, d["id"])
            elif action == "limset":
                col = "new_per_day" if parts[3] == "n" else "reviews_per_day"
                self.store.update_deck(d["id"], **{col: max(0, d[col] + int(parts[4]))})
                view = menus.limits(self.store, d["id"])
            elif action == "tmpl":
                view = menus.template(self.store, d["id"])
            elif action == "rev":
                if not d["reverse"]:
                    self.store.add_reverse_cards(d["id"])
                self.store.update_deck(d["id"], reverse=0 if d["reverse"] else 1)
                view = menus.template(self.store, d["id"])
            elif action == "ren":
                self.store.set_pending_input(
                    uid, {"kind": "rename_deck", "deck_id": d["id"], "message_id": c.message.message_id}
                )
                view = menus.rename_prompt(self.store, d["id"])
            elif action == "del":
                view = menus.delete_confirm(self.store, d["id"])
            elif action == "delok":
                st = self.store.state(uid)
                if st["active_card_id"]:
                    row = self.store.card(st["active_card_id"])
                    if row and row["deck_id"] == d["id"]:
                        self.store.update_state(uid, active_card_id=None, asked_at=None)
                self.store.delete_deck(d["id"])
                self.store.log_message(uid, "note", f"deck {d['name']!r} deleted")
                await c.answer("Deleted")
                view = menus.deck_list(self.store, uid)
        elif action in ("note", "ndel", "ndelok"):
            n = owned_note(ids[0]) if ids else None
            page = ids[1] if len(ids) > 1 else 0
            if n is None:
                view = menus.deck_list(self.store, uid)
            elif action == "note":
                view = menus.note_view(self.store, n["id"], page)
            elif action == "ndel":
                view = menus.note_delete_confirm(self.store, n["id"], page)
            else:
                deck_id = n["deck_id"]
                self._delete_note(uid, n["id"])
                view = menus.card_list(self.store, deck_id, page)
        if view is None:
            view = menus.deck_list(self.store, uid)
        text, kb = view
        with contextlib.suppress(TelegramBadRequest):
            await c.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        with contextlib.suppress(TelegramBadRequest):
            await c.answer()

    # ================= timer =================

    async def ticker(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("tick failed")
            await asyncio.sleep(TICK_SECONDS)

    async def tick(self, now: datetime | None = None) -> None:
        now = now or _now()
        if self.config.db_path and str(self.config.db_path) != ":memory:":
            if self.store.backup(self.config.db_path.parent / "backups"):
                log.info("database backup written")
        for user in self.store.all_users():
            if user["id"] not in self.config.owner_ids:
                continue
            lock = self.lock(user["id"])
            if lock.locked():
                continue
            async with lock:
                await self._tick_user(dict(user), now)

    async def _tick_user(self, user: dict[str, Any], now: datetime) -> None:
        uid = user["id"]
        st = self.store.state(uid)
        backoff = parse(st["llm_backoff_until"])
        if backoff and now < backoff:
            return
        # New study day: reset the daily reminder counter.
        today = srs.day_start(user, now).date().isoformat()
        if st["reminders_date"] != today:
            self.store.update_state(uid, reminders_date=today, reminders_today=0)
            st = self.store.state(uid)
        quiet = srs.is_quiet(user, now)
        last_user = parse(st["last_user_at"])
        active = st["active_card_id"] is not None and self.store.card(st["active_card_id"]) is not None
        if st["active_card_id"] and not active:
            self.store.update_state(uid, active_card_id=None, asked_at=None)

        if pacing.should_remind(
            now,
            active_card=active,
            asked_at=parse(st["asked_at"]),
            last_user_at=last_user,
            quiet=quiet,
            reminders_today=st["reminders_today"],
            max_reminders=user["max_reminders"],
            streak=st["reminders_streak"],
            first_gap_min=user["first_reminder_min"],
        ):
            await self.run_llm(uid, user["chat_id"], lambda: self.agent.remind(uid, now), notify=False)
            return

        has_due = bool(srs.due_queue(self.store, user, now))
        if pacing.should_ask(
            now,
            has_due=has_due,
            active_card=active,
            next_ask_at=parse(st["next_ask_at"]),
            quiet=quiet,
            last_user_at=last_user,
            last_bot_at=parse(st["last_bot_at"]),
        ):
            await self.run_llm(uid, user["chat_id"], lambda: self.agent.ask_next(uid, now), notify=False)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _chunks(text: str) -> list[str]:
    out = []
    while len(text) > TG_LIMIT:
        cut = text.rfind("\n", 0, TG_LIMIT)
        if cut < TG_LIMIT // 2:
            cut = TG_LIMIT
        out.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text:
        out.append(text)
    return out


def build_dispatcher(app: App) -> Dispatcher:
    dp = Dispatcher()
    dp.include_router(app.router)
    return dp

"""Web server for the reader Mini App.

It listens on localhost only. A web server with HTTPS (Caddy, see
deploy/reader-https.sh) passes requests from the internet to it.

Who is asking: Telegram gives the Mini App signed "init data" about the user.
The app trades it once for a session token (POST /api/session); every other
request carries that token. Only people allowed to use the bot get one, and
everyone sees only their own books.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import shutil
import time
from pathlib import Path
from typing import Any

from aiogram.utils.web_app import safe_parse_webapp_init_data
from aiohttp import web

from .. import books
from ..db import Store, iso, utcnow
from ..llm import LLMError
from . import pictures

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"
TOKEN_DAYS = 30
WORD_MAX, SENTENCE_MAX, CONTEXT_MAX = 200, 600, 1500
QUESTION_MAX, HISTORY_MAX = 500, 6
PREF_KEYS = {"theme", "size", "font", "spacing", "margins", "justify", "bold", "scroll", "sort", "view"}


class Library:
    """Books on disk plus their rows in the database."""

    def __init__(self, store: Store, data_dir: Path):
        self.store = store
        self.dir = data_dir / "books"

    def folder(self, book_id: int) -> Path:
        return self.dir / str(book_id)

    def add(self, user_id: int, data: bytes, filename: str) -> dict:
        """Reads and stores a book. Raises books.BookError if it can't be read."""
        if len(data) > books.MAX_SIZE:
            raise books.BookError("That file is over 100 MB, too big for the reader.")
        book = books.read_book(data, filename)
        book_id = self.store.add_book(user_id, book.title.strip()[:300], book.author.strip()[:300], None,
                                      sum(c.chars for c in book.chapters))
        try:
            cover = books.save_book(book, self.folder(book_id), data, filename)
        except Exception:
            self.store.delete_book(book_id)
            with contextlib.suppress(FileNotFoundError):
                shutil.rmtree(self.folder(book_id))
            raise
        if cover:
            self.store.update_book(book_id, cover=cover)
        return book_row(self.store.book(user_id, book_id))

    def delete(self, user_id: int, book_id: int) -> bool:
        if not self.store.book(user_id, book_id):
            return False
        self.store.delete_book(book_id)
        with contextlib.suppress(FileNotFoundError):
            shutil.rmtree(self.folder(book_id))
        return True


def book_row(row) -> dict:
    return {
        "id": row["id"],
        "title": row["title"],
        "author": row["author"],
        "cover": bool(row["cover"]),
        "percent": row["percent"],
        "finished": bool(row["finished"]),
        "position": json.loads(row["position"]) if row["position"] else None,
        "bookmarks": json.loads(row["bookmarks"] or "[]"),
        "added_at": row["added_at"],
        "opened_at": row["opened_at"],
    }


# ---------------- session tokens ----------------

def _key(bot_token: str) -> bytes:
    return hashlib.sha256(b"macaw-reader:" + bot_token.encode()).digest()


def make_token(bot_token: str, user_id: int, now: float | None = None, days: int = TOKEN_DAYS) -> str:
    exp = int((now or time.time()) + days * 86400)
    body = f"{user_id}.{exp}"
    sig = hmac.new(_key(bot_token), body.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{body}.{sig}"


def read_token(bot_token: str, token: str, now: float | None = None) -> int | None:
    try:
        uid, exp, sig = token.split(".")
        body = f"{uid}.{exp}"
        good = hmac.new(_key(bot_token), body.encode(), hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(sig, good) or int(exp) < (now or time.time()):
            return None
        return int(uid)
    except (ValueError, AttributeError):
        return None


# ---------------- the web app ----------------

class ReaderServer:
    def __init__(self, bot_token: str, allowed, library: Library, store: Store, explain=None, add_word=None, ask=None,
                 find_pictures=pictures.find, charlotte=None, owners: frozenset[int] = frozenset()):
        self.bot_token = bot_token
        self.allowed = allowed  # callable: user id -> bool
        self.library = library
        self.store = store
        # Tap a word in the book: explain(uid, word, sentence, context, book) -> {text, picture} (async), and
        # add_word(uid, word, sentence, meaning, book), which hands it to the bot to make a card.
        self.explain = explain
        self.add_word = add_word
        self.ask = ask  # ask(uid, word, sentence, context, book, explanation, history, question) -> text
        self.find_pictures = find_pictures
        self.charlotte = charlotte  # the Witcher 3 companion (macaw.charlotte.Charlotte); owners only
        self.owners = owners
        self._explained: dict[tuple, dict] = {}
        self._pictures: dict[str, list] = {}

    def build(self) -> web.Application:
        app = web.Application(client_max_size=books.MAX_SIZE + 1024 * 1024, middlewares=[self._headers])
        r = app.router
        r.add_get("/", self.index)
        r.add_static("/static/", STATIC, append_version=False)
        r.add_post("/api/session", self.session)
        r.add_get("/api/books", self.list_books)
        r.add_post("/api/books", self.upload)
        r.add_get("/api/books/{id:\\d+}", self.get_book)
        r.add_patch("/api/books/{id:\\d+}", self.patch_book)
        r.add_post("/api/books/{id:\\d+}/progress", self.beacon)  # sendBeacon when the app closes
        r.add_delete("/api/books/{id:\\d+}", self.delete_book)
        r.add_get("/api/books/{id:\\d+}/cover", self.cover)
        r.add_get("/api/books/{id:\\d+}/res/{name}", self.resource)
        r.add_get("/api/prefs", self.get_prefs)
        r.add_put("/api/prefs", self.put_prefs)
        r.add_post("/api/books/{id:\\d+}/explain", self.explain_word)
        r.add_post("/api/books/{id:\\d+}/add-word", self.add_to_deck)
        r.add_post("/api/books/{id:\\d+}/ask", self.ask_about)
        r.add_get("/api/pictures", self.pictures)
        r.add_get("/api/charlotte/status", self.charlotte_status)
        r.add_post("/api/charlotte/event", self.charlotte_event)
        return app

    @web.middleware
    async def _headers(self, request: web.Request, handler):
        resp = await handler(request)
        if request.path.startswith("/api/"):
            resp.headers.setdefault("Cache-Control", "no-store")
        elif request.path.startswith("/static/") or request.path == "/":
            resp.headers["Cache-Control"] = "no-cache"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        return resp

    def _user(self, request: web.Request) -> int:
        token = request.query.get("t") or request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        uid = read_token(self.bot_token, token) if token else None
        if uid is None or not self.allowed(uid):
            raise web.HTTPUnauthorized(text="Open the reader from the bot in Telegram.")
        return uid

    def _book(self, request: web.Request, uid: int):
        row = self.store.book(uid, int(request.match_info["id"]))
        if not row:
            raise web.HTTPNotFound(text="No such book.")
        return row

    async def index(self, request: web.Request) -> web.StreamResponse:
        return web.FileResponse(STATIC / "index.html")

    async def session(self, request: web.Request) -> web.Response:
        body = await request.json()
        try:
            data = safe_parse_webapp_init_data(self.bot_token, body.get("initData", ""))
        except ValueError:
            raise web.HTTPUnauthorized(text="Open the reader from the bot in Telegram.")
        if not data.user or not self.allowed(data.user.id):
            raise web.HTTPForbidden(text="Sorry, this is a private bot.")
        return web.json_response({"token": make_token(self.bot_token, data.user.id), "user": data.user.id})

    async def list_books(self, request: web.Request) -> web.Response:
        uid = self._user(request)
        return web.json_response([book_row(r) for r in self.store.books(uid)])

    async def upload(self, request: web.Request) -> web.Response:
        uid = self._user(request)
        reader = await request.multipart()
        part = await reader.next()
        while part is not None and part.name != "file":
            part = await reader.next()
        if part is None or not part.filename:
            raise web.HTTPBadRequest(text="No file.")
        data = await part.read(decode=False)
        try:
            row = await asyncio.to_thread(self.library.add, uid, bytes(data), part.filename)
        except books.BookError as e:
            return web.json_response({"error": str(e)}, status=422)
        except Exception:
            log.exception("adding a book failed")
            return web.json_response({"error": "Something went wrong reading that book."}, status=500)
        return web.json_response(row)

    async def get_book(self, request: web.Request) -> web.Response:
        uid = self._user(request)
        row = self._book(request, uid)
        path = self.library.folder(row["id"]) / "book.json"
        content = await asyncio.to_thread(path.read_text)
        self.store.update_book(row["id"], opened_at=iso(utcnow()))
        meta = json.dumps(book_row(row), ensure_ascii=False)
        # The book text is already JSON on disk; wrap it without parsing it again.
        return web.Response(text=f'{{"meta":{meta},"book":{content}}}', content_type="application/json")

    def _apply(self, row, body: dict[str, Any]) -> None:
        fields: dict[str, Any] = {}
        if isinstance(body.get("position"), dict):
            pos = body["position"]
            fields["position"] = {"c": int(pos.get("c", 0)), "p": min(1.0, max(0.0, float(pos.get("p", 0))))}
        if "percent" in body:
            fields["percent"] = min(100.0, max(0.0, float(body["percent"])))
        if "finished" in body:
            fields["finished"] = 1 if body["finished"] else 0
        if isinstance(body.get("bookmarks"), list):
            fields["bookmarks"] = [
                {"c": int(b.get("c", 0)), "p": float(b.get("p", 0)), "text": str(b.get("text", ""))[:200],
                 "at": str(b.get("at", ""))[:40]}
                for b in body["bookmarks"][:500] if isinstance(b, dict)
            ]
        if isinstance(body.get("title"), str) and body["title"].strip():
            fields["title"] = body["title"].strip()[:300]
        if isinstance(body.get("author"), str):
            fields["author"] = body["author"].strip()[:300]
        self.store.update_book(row["id"], **fields)

    async def patch_book(self, request: web.Request) -> web.Response:
        uid = self._user(request)
        row = self._book(request, uid)
        self._apply(row, await request.json())
        return web.json_response(book_row(self.store.book(uid, row["id"])))

    async def beacon(self, request: web.Request) -> web.Response:
        uid = self._user(request)
        row = self._book(request, uid)
        with contextlib.suppress(ValueError):
            self._apply(row, json.loads(await request.text()))
        return web.Response(status=204)

    async def delete_book(self, request: web.Request) -> web.Response:
        uid = self._user(request)
        if not self.library.delete(uid, int(request.match_info["id"])):
            raise web.HTTPNotFound(text="No such book.")
        return web.Response(status=204)

    async def cover(self, request: web.Request) -> web.StreamResponse:
        uid = self._user(request)
        row = self._book(request, uid)
        if not row["cover"]:
            raise web.HTTPNotFound()
        resp = web.FileResponse(self.library.folder(row["id"]) / row["cover"])
        resp.headers["Cache-Control"] = "private, max-age=86400"
        return resp

    async def resource(self, request: web.Request) -> web.StreamResponse:
        uid = self._user(request)
        row = self._book(request, uid)
        name = request.match_info["name"]
        if "/" in name or "\\" in name or name.startswith("."):
            raise web.HTTPNotFound()
        path = self.library.folder(row["id"]) / "res" / name
        if not path.is_file():
            raise web.HTTPNotFound()
        resp = web.FileResponse(path)
        if path.suffix.lower() == ".svg":
            # A picture, never a page that could run scripts.
            resp.headers["Content-Security-Policy"] = "script-src 'none'"
        resp.headers["Cache-Control"] = "private, max-age=86400"
        return resp

    async def get_prefs(self, request: web.Request) -> web.Response:
        uid = self._user(request)
        return web.json_response(self.store.reader_prefs(uid))

    async def put_prefs(self, request: web.Request) -> web.Response:
        uid = self._user(request)
        body = await request.json()
        prefs = {k: v for k, v in body.items() if k in PREF_KEYS and isinstance(v, (str, int, float, bool))}
        self.store.set_reader_prefs(uid, prefs)
        return web.json_response(prefs)

    async def _word(self, request: web.Request) -> tuple[int, Any, dict[str, str]]:
        uid = self._user(request)
        row = self._book(request, uid)
        body = await request.json()
        word = " ".join(str(body.get("word") or "").split())[:WORD_MAX]
        sentence = " ".join(str(body.get("sentence") or "").split())[:SENTENCE_MAX]
        if not word:
            raise web.HTTPBadRequest(text="No word.")
        return uid, row, {"word": word, "sentence": sentence or word,
                          "context": " ".join(str(body.get("context") or "").split())[:CONTEXT_MAX],
                          "meaning": str(body.get("meaning") or "").strip()[:1500]}

    async def _llm(self, call) -> web.Response:
        try:
            return web.json_response(await call())
        except LLMError as e:
            log.warning("reader AI call failed: %s", e)
            return web.json_response({"error": "Couldn't get an answer right now. Try again in a bit."}, status=503)
        except Exception:
            log.exception("reader AI call failed")
            return web.json_response({"error": "Something went wrong. Try again."}, status=500)

    async def explain_word(self, request: web.Request) -> web.Response:
        uid, row, w = await self._word(request)
        if not self.explain:
            return web.json_response({"error": "Explanations aren't switched on."}, status=503)
        key = (uid, row["id"], w["word"].lower(), w["sentence"])

        async def call():
            if key not in self._explained:
                out = await self.explain(uid, w["word"], w["sentence"], w["context"], row["title"])
                if len(self._explained) >= 300:
                    self._explained.pop(next(iter(self._explained)))
                self._explained[key] = out
            return self._explained[key]

        return await self._llm(call)

    async def ask_about(self, request: web.Request) -> web.Response:
        uid, row, w = await self._word(request)
        body = await request.json()
        question = str(body.get("question") or "").strip()[:QUESTION_MAX]
        if not question:
            raise web.HTTPBadRequest(text="No question.")
        if not self.ask:
            return web.json_response({"error": "Questions aren't switched on."}, status=503)
        history = [
            (str(h.get("q", ""))[:QUESTION_MAX], str(h.get("a", ""))[:2000])
            for h in (body.get("history") or [])[-HISTORY_MAX:] if isinstance(h, dict)
        ]
        explanation = str(body.get("explanation") or "")[:2000]

        async def call():
            text = await self.ask(uid, w["word"], w["sentence"], w["context"], row["title"],
                                  explanation, history, question)
            return {"text": text}

        return await self._llm(call)

    async def pictures(self, request: web.Request) -> web.Response:
        self._user(request)
        q = " ".join(request.query.get("q", "").split())[:60]
        if not q:
            return web.json_response([])
        key = q.lower()
        found = self._pictures.get(key)
        if found is None:
            try:
                found = await self.find_pictures(q)
            except pictures.Unreachable as e:
                log.warning("photos for %r: Wikimedia Commons unreachable (%s)", q, e)
                return web.json_response({"error": "Couldn't reach Wikimedia for photos."}, status=502)
            log.info("photos for %r: %d found", q, len(found))
            if len(self._pictures) >= 300:
                self._pictures.pop(next(iter(self._pictures)))
            self._pictures[key] = found
        resp = web.json_response(found)
        resp.headers["Cache-Control"] = "private, max-age=86400"
        return resp

    async def charlotte_status(self, request: web.Request) -> web.Response:
        uid = _charlotte_user(self, request)
        return web.json_response(self.charlotte.status(uid))

    async def charlotte_event(self, request: web.Request) -> web.Response:
        uid = _charlotte_user(self, request)
        body = await request.json()
        kind = str(body.get("event") or "")
        said = str(body.get("said") or "")
        game = body.get("game") if isinstance(body.get("game"), dict) else {}
        if len(json.dumps(game)) > CHARLOTTE_GAME_MAX:
            raise web.HTTPBadRequest(text="Game info too long.")
        try:
            result = await self.charlotte.event(uid, kind, said, game)
        except ValueError as e:
            raise web.HTTPBadRequest(text=str(e))
        except LLMError as e:
            log.warning("charlotte turn failed: %s", e)
            # Out of Claude for now: she excuses herself instead of a weaker model taking over.
            return web.json_response({"say": "Forgive me, Geralt, my head is spinning. Another time.",
                                      "action": "leave", "error": "unavailable"})
        return web.json_response(result.as_dict())

    async def add_to_deck(self, request: web.Request) -> web.Response:
        uid, row, w = await self._word(request)
        if not self.add_word:
            return web.json_response({"error": "Adding words isn't switched on."}, status=503)
        self.add_word(uid, w["word"], w["sentence"], w["meaning"], row["title"])
        return web.json_response({"ok": True})


CHARLOTTE_GAME_MAX = 2000


def _charlotte_user(server: "ReaderServer", request: web.Request) -> int:
    uid = server._user(request)
    if server.charlotte is None or uid not in server.owners:
        raise web.HTTPForbidden(text="Charlotte is only for the bot's owner.")
    return uid


async def start(server: ReaderServer, host: str, port: int) -> web.AppRunner | None:
    """Starts the web server; on failure logs it and returns None so the bot keeps working."""
    runner = web.AppRunner(server.build(), access_log=None)
    await runner.setup()
    try:
        await web.TCPSite(runner, host, port).start()
    except OSError as e:
        log.warning("reader web server could not listen on %s:%s (%s); the reader is off", host, port, e)
        await runner.cleanup()
        return None
    log.info("reader web server on http://%s:%s", host, port)
    return runner

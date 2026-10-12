from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest
from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer

from macaw import books
from macaw.reader import server as reader
from tests.bookfiles import PNG, make_epub, make_fb2

BOT_TOKEN = "123:abc"


def test_epub_chapters_toc_cover_and_cleaning():
    extra = '<p onclick="steal()">Hi <a href="ch1.xhtml#top">back</a> <a href="https://example.com">web</a>' \
            '<img src="../images/pic.png" alt="pic"/></p><iframe src="x"></iframe>'
    book = books.read_book(make_epub(extras={1: extra}), "river.epub")
    assert book.title == "The Quiet River"
    assert book.author == "Anna Field"
    assert len(book.chapters) == 3
    assert [t["title"] for t in book.toc] == ["Chapter 1", "Chapter 2", "Chapter 3"]
    assert book.toc[1] == {"title": "Chapter 2", "c": 1, "a": "c1-top", "level": 0}
    assert book.cover == (".png", PNG)
    html = book.chapters[1].html
    assert "<script" not in html and "alert" not in html and "color: red" not in html
    assert "onclick" not in html and "iframe" not in html
    assert 'id="c1-top"' in html
    assert '<a data-a="c0-top" data-c="0">back</a>' in html
    assert 'data-ext="https://example.com"' in html
    assert '<img src="res/1.png" alt="pic">' in html
    assert book.resources == {"1.png": PNG}
    assert book.chapters[0].title == "Chapter 1"


def test_epub_with_ncx_only():
    book = books.read_book(make_epub(nav=False, cover=None), "x.epub")
    assert [t["c"] for t in book.toc] == [0, 1, 2]
    assert book.cover is None


def test_fb2():
    book = books.read_book(make_fb2(), "reka.fb2")
    assert book.title == "Тихая река"
    assert book.author == "Анна Полева"
    assert book.cover == (".png", PNG)
    titles = [t["title"] for t in book.toc]
    assert titles == ["Глава первая", "Глава вторая", "Notes"]
    first = book.chapters[book.toc[0]["c"]].html
    assert "<em>Ивы</em>" in first and 'src="res/' in first
    notes_c = book.toc[2]["c"]
    assert f'data-c="{notes_c}" data-a="c{notes_c}-n1"' in first


def test_txt_splits_chapters_and_reads_cp1251():
    text = "Глава 1\n\nПервый абзац.\n\nВторой.\n\nГлава 2\n\nТретий."
    book = books.read_book(text.encode("cp1251"), "my_book.txt")
    assert book.title == "my book"
    assert [t["title"] for t in book.toc] == ["Глава 1", "Глава 2"]
    assert "<p>Первый абзац.</p>" in book.chapters[0].html


def test_bad_files():
    with pytest.raises(books.BookError):
        books.read_book(b"not a zip", "x.epub")
    with pytest.raises(books.BookError):
        books.read_book(b"hello", "x.pdf")


def init_data(user_id: int, token: str = BOT_TOKEN) -> str:
    fields = {"auth_date": str(int(time.time())), "user": json.dumps({"id": user_id, "first_name": "N"})}
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def test_tokens():
    t = reader.make_token(BOT_TOKEN, 42)
    assert reader.read_token(BOT_TOKEN, t) == 42
    assert reader.read_token("other", t) is None
    assert reader.read_token(BOT_TOKEN, t.replace("42.", "43.", 1)) is None
    assert reader.read_token(BOT_TOKEN, t, now=time.time() + 40 * 86400) is None


@pytest.fixture
async def client(store, tmp_path):
    lib = reader.Library(store, tmp_path)
    srv = reader.ReaderServer(BOT_TOKEN, lambda uid: uid in (42, 7), lib, store)
    c = TestClient(TestServer(srv.build()))
    await c.start_server()
    yield c
    await c.close()


async def session(client, uid=42) -> dict:
    r = await client.post("/api/session", json={"initData": init_data(uid)})
    assert r.status == 200
    return {"Authorization": "Bearer " + (await r.json())["token"]}


async def test_server_flow(client):
    r = await client.post("/api/session", json={"initData": init_data(99)})
    assert r.status == 403
    r = await client.post("/api/session", json={"initData": init_data(42, "wrong:token")})
    assert r.status == 401
    assert (await client.get("/api/books")).status == 401

    h = await session(client)
    form = FormData()
    form.add_field("file", make_epub(extras={0: '<img src="../images/pic.png"/>'}), filename="river.epub")
    r = await client.post("/api/books", data=form, headers=h)
    assert r.status == 200, await r.text()
    b = await r.json()
    assert b["title"] == "The Quiet River" and b["cover"] and b["opened_at"] is None

    r = await client.get(f"/api/books/{b['id']}", headers=h)
    data = await r.json()
    assert len(data["book"]["chapters"]) == 3 and data["meta"]["id"] == b["id"]

    token = h["Authorization"].split()[1]
    assert (await client.get(f"/api/books/{b['id']}/cover?t={token}")).status == 200
    assert (await client.get(f"/api/books/{b['id']}/res/1.png?t={token}")).status == 200
    assert (await client.get(f"/api/books/{b['id']}/res/..%2Fbook.json?t={token}")).status == 404
    assert (await client.get(f"/api/books/{b['id']}/cover")).status == 401

    r = await client.patch(f"/api/books/{b['id']}", headers=h, json={
        "position": {"c": 1, "p": 0.5}, "percent": 40, "bookmarks": [{"c": 1, "p": 0.5, "text": "x"}]})
    row = await r.json()
    assert row["position"] == {"c": 1, "p": 0.5} and row["percent"] == 40 and row["bookmarks"][0]["text"] == "x"
    r = await client.post(f"/api/books/{b['id']}/progress?t={token}", data=json.dumps({"percent": 100, "finished": True}))
    assert r.status == 204

    # Someone else can't see it.
    h7 = await session(client, 7)
    assert await (await client.get("/api/books", headers=h7)).json() == []
    assert (await client.get(f"/api/books/{b['id']}", headers=h7)).status == 404
    assert (await client.delete(f"/api/books/{b['id']}", headers=h7)).status == 404

    books_list = await (await client.get("/api/books", headers=h)).json()
    assert books_list[0]["finished"] is True and books_list[0]["opened_at"]

    r = await client.put("/api/prefs", headers=h, json={"theme": "calm", "size": 23, "evil": "x"})
    assert await r.json() == {"theme": "calm", "size": 23}
    assert await (await client.get("/api/prefs", headers=h)).json() == {"theme": "calm", "size": 23}

    form = FormData()
    form.add_field("file", b"junk", filename="bad.epub")
    r = await client.post("/api/books", data=form, headers=h)
    assert r.status == 422 and "damaged" in (await r.json())["error"]

    assert (await client.delete(f"/api/books/{b['id']}", headers=h)).status == 204
    assert await (await client.get("/api/books", headers=h)).json() == []
    assert (await client.get("/")).status == 200


async def test_tunnel_url():
    from aiohttp import web

    from macaw.reader.tunnel import tunnel_url

    app = web.Application()
    app.router.add_get("/quicktunnel", lambda r: web.json_response({"hostname": "abc-def.trycloudflare.com"}))
    srv = TestServer(app)
    await srv.start_server()
    try:
        assert await tunnel_url(f"127.0.0.1:{srv.port}") == "https://abc-def.trycloudflare.com/"
    finally:
        await srv.close()
    assert await tunnel_url(f"127.0.0.1:{srv.port}") is None  # nothing listening any more


async def test_tap_a_word_explain_and_add(store, tmp_path):
    lib = reader.Library(store, tmp_path)
    calls = {"explain": [], "add": [], "ask": [], "pics": []}

    async def explain(uid, word, sentence, context, book):
        calls["explain"].append((uid, word, sentence, context, book))
        if word == "broken":
            from macaw.llm import LLMError
            raise LLMError("limit hit")
        return {"text": "deep (adjective)\nNot shallow.", "picture": "deep pool"}

    async def ask(uid, word, sentence, context, book, explanation, history, question):
        calls["ask"].append((word, explanation, history, question))
        return "Yes."

    async def find_pictures(q):
        calls["pics"].append(q)
        return [{"src": "https://upload.wikimedia.org/x.jpg"}] if q != "nothing" else []

    srv = reader.ReaderServer(BOT_TOKEN, lambda uid: uid in (42, 7), lib, store, explain=explain,
                              add_word=lambda *a: calls["add"].append(a), ask=ask, find_pictures=find_pictures)
    client = TestClient(TestServer(srv.build()))
    await client.start_server()
    try:
        h = await session(client)
        b = lib.add(42, make_epub(), "river.epub")
        word = {"word": " deep ", "sentence": "They swam in the\n deep pool.", "context": "It was hot. They swam in the deep pool."}
        r = await client.post(f"/api/books/{b['id']}/explain", headers=h, json=word)
        assert await r.json() == {"text": "deep (adjective)\nNot shallow.", "picture": "deep pool"}
        assert calls["explain"] == [(42, "deep", "They swam in the deep pool.", "It was hot. They swam in the deep pool.",
                                     "The Quiet River")]
        await client.post(f"/api/books/{b['id']}/explain", headers=h, json=word)
        assert len(calls["explain"]) == 1  # the same word in the same sentence is asked once

        r = await client.post(f"/api/books/{b['id']}/explain", headers=h, json={"word": "broken", "sentence": "x"})
        assert r.status == 503 and "Try again" in (await r.json())["error"]
        assert (await client.post(f"/api/books/{b['id']}/explain", headers=h, json={"word": " "})).status == 400

        r = await client.post(f"/api/books/{b['id']}/add-word", headers=h,
                              json={"word": "deep", "sentence": "They swam in the deep pool.", "meaning": "Not shallow."})
        assert await r.json() == {"ok": True}
        assert calls["add"] == [(42, "deep", "They swam in the deep pool.", "Not shallow.", "The Quiet River")]

        r = await client.post(f"/api/books/{b['id']}/ask", headers=h, json={
            **word, "explanation": "Not shallow.", "history": [{"q": "Q1", "a": "A1"}], "question": " Can I say deep river? "})
        assert await r.json() == {"text": "Yes."}
        assert calls["ask"] == [("deep", "Not shallow.", [("Q1", "A1")], "Can I say deep river?")]
        assert (await client.post(f"/api/books/{b['id']}/ask", headers=h, json={**word, "question": ""})).status == 400

        r = await client.get("/api/pictures?q=deep%20pool", headers=h)
        assert await r.json() == [{"src": "https://upload.wikimedia.org/x.jpg"}]
        await client.get("/api/pictures?q=Deep  pool", headers=h)
        await client.get("/api/pictures?q=nothing", headers=h)
        await client.get("/api/pictures?q=nothing", headers=h)
        assert calls["pics"] == ["deep pool", "nothing", "nothing"]  # found photos are kept, misses asked again
        assert (await client.get("/api/pictures?q=x")).status == 401

        # Only the owner of the book, and only with a session.
        h7 = await session(client, 7)
        assert (await client.post(f"/api/books/{b['id']}/add-word", headers=h7, json={"word": "deep"})).status == 404
        assert (await client.post(f"/api/books/{b['id']}/explain", json={"word": "deep"})).status == 401
    finally:
        await client.close()


async def test_explain_word_and_add_book_word(store):
    import asyncio

    from macaw.agent import Agent
    from macaw.bot.app import App
    from macaw.config import Config
    from tests.conftest import UID, FakeLLM

    llm = FakeLLM()
    seen = {}

    async def reply(prompt, tools):
        seen["prompt"] = prompt
        return "deep (adjective) 🙂\nNot shallow.\nPicture: deep pool."

    llm.script.append(reply)
    agent = Agent(store, llm)
    text = await agent.explain_word(UID, "deep", "They swam in the deep pool.", "It was hot. They swam in the deep pool.", "River")
    assert text == {"text": "deep (adjective)\nNot shallow.", "picture": "deep pool"}
    assert "Word tapped: deep" in seen["prompt"] and "Book: River" in seen["prompt"] and "It was hot." in seen["prompt"]

    app = App(Config("t", frozenset({UID}), None, "UTC", "fake", None, "INFO"), store, agent, None)
    ran = []

    async def run_llm(uid, chat_id, factory, notify):
        ran.append((uid, chat_id, notify))
        await factory()

    async def on_user_message(uid, text, now=None):
        ran.append(text)
        return []

    app.run_llm = run_llm
    agent.on_user_message = on_user_message
    app.add_book_word(UID, "deep", "They swam in the deep pool.", "Not shallow.", "River")
    await asyncio.gather(*app._background)
    assert ran[0] == (UID, UID, True)
    assert '"deep"' in ran[1] and "River" in ran[1] and "They swam in the deep pool." in ran[1] and "Not shallow." in ran[1]


async def test_explain_without_picture_and_follow_up(store):
    from macaw.agent import Agent
    from tests.conftest import UID, FakeLLM

    llm = FakeLLM()
    prompts = []

    def step(reply):
        async def run(prompt, tools):
            prompts.append(prompt)
            return reply
        return run

    llm.script += [step("however (adverb)\nIt shows a contrast.\n**Picture:** none"), step("It is formal. ")]
    agent = Agent(store, llm)
    assert await agent.explain_word(UID, "however", "However, he went.") == {
        "text": "however (adverb)\nIt shows a contrast.", "picture": ""}
    answer = await agent.ask_about_word(UID, "at the end of the day", "At the end of the day, he went.", "", "River",
                                        "It means finally.", [("Is it common?", "Yes.")], "Is it formal?")
    assert answer == "It is formal."
    assert "Words selected: at the end of the day" in prompts[1] and "It means finally." in prompts[1]
    assert "They asked: Is it common?\nYou answered: Yes." in prompts[1] and prompts[1].endswith("Is it formal?")


async def test_pictures_from_commons():
    from aiohttp import web

    from macaw.reader import pictures

    seen = {}

    async def api(request):
        seen.update(request.query)
        seen["ua"] = request.headers.get("User-Agent", "")
        info = lambda name, w, h: {"thumburl": f"https://upload.wikimedia.org/{name}.jpg", "thumbwidth": 360,
                                   "thumbheight": 240, "width": w, "height": h,
                                   "descriptionurl": f"https://commons.wikimedia.org/wiki/File:{name}.jpg"}
        return web.json_response({"query": {"pages": [
            {"index": 3, "title": "File:Third.jpg", "imageinfo": [info("Third", 1200, 800)]},
            {"index": 1, "title": "File:First pool.jpg", "imageinfo": [info("First", 4000, 3000)]},
            {"index": 2, "title": "File:Strip.jpg", "imageinfo": [info("Strip", 9000, 600)]},  # a panorama
            {"index": 4, "title": "File:Tiny.jpg", "imageinfo": [info("Tiny", 120, 90)]},
        ]}})

    app = web.Application()
    app.router.add_get("/w/api.php", api)
    srv = TestServer(app)
    await srv.start_server()
    try:
        found = await pictures.find("deep pool", api=f"http://127.0.0.1:{srv.port}/w/api.php")
    finally:
        await srv.close()
    assert [p["title"] for p in found] == ["First pool", "Third"]
    assert found[0]["src"] == "https://upload.wikimedia.org/First.jpg" and found[0]["page"].endswith("First.jpg")
    assert seen["gsrsearch"] == "deep pool filemime:image/jpeg" and "macaw" in seen["ua"]
    assert await pictures.find("x", api=f"http://127.0.0.1:{srv.port}/w/api.php") == []  # offline: no photos

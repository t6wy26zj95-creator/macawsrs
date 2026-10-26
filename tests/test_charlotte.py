from __future__ import annotations

import pytest

from macaw.agent import Agent
from macaw.charlotte import Charlotte, clean_speech
from macaw.llm import claude_options

from .conftest import UID, FakeLLM, at
from .test_agent import NOW, _make_deck_with_card


@pytest.fixture
def setup(store):
    llm = FakeLLM()
    agent = Agent(store, llm)
    her = FakeLLM()
    return agent, llm, Charlotte(store, agent, her), her


async def test_first_meeting_then_journal(setup, store):
    agent, llm, charlotte, her = setup
    assert charlotte.status(UID, NOW) == {"due": 0, "met": False, "in_meeting": False}

    async def hello(prompt, t):
        assert "FIRST meeting" in prompt
        assert "YOUR JOURNAL: empty" in prompt
        assert "region: Velen" in prompt and "quest: Family Matters" in prompt
        assert claude_options.get().model == "sonnet"
        return '"Geralt of Rivia? *smiles* Forgive me."'

    her.script.append(hello)
    r = await charlotte.event(UID, "first_meeting", game={"region": "Velen", "quest": "Family Matters", "junk": "x"}, now=NOW)
    assert r.say == "Geralt of Rivia? Forgive me."
    assert r.action == "stay"

    async def bye(prompt, t):
        assert "Geralt: I have to go" in prompt
        assert "Charlotte: Geralt of Rivia? Forgive me." in prompt
        await t["write_journal"]({"entry": "Met Geralt in Velen. He agreed to the contract."})
        await t["set_action"]({"action": "leave"})
        return "Until next time."

    her.script.append(bye)
    r = await charlotte.event(UID, "speech", said="I have to go", now=NOW)
    assert r.action == "leave" and r.journal_saved
    assert charlotte.status(UID, NOW)["met"] is True

    async def again(prompt, t):
        assert "FIRST meeting" not in prompt
        assert "He agreed to the contract." in prompt
        return "We meet again."

    her.script.append(again)
    r = await charlotte.event(UID, "first_meeting", now=NOW)  # only happens once
    assert r.say == "We meet again."


async def test_quiz_grades_with_the_bot_scheduler(setup, store):
    agent, llm, charlotte, her = setup
    await _make_deck_with_card(agent, llm)
    assert charlotte.status(UID, NOW)["due"] == 1

    async def ask(prompt, t):
        assert "CARDS DUE NOW: 1" in prompt
        out = await t["next_card"]({})
        assert "Ask about: ubiquitous" in out
        return "What does ubiquitous mean?"

    her.script.append(ask)
    await charlotte.event(UID, "arrive", now=NOW)
    card_id = charlotte.meetings[UID].active

    async def grade(prompt, t):
        assert "OPEN QUESTION" in prompt and "present everywhere" in prompt
        with pytest.raises(ValueError):
            await t["grade_card"]({"card_id": card_id + 99, "rating": "Good"})
        out = await t["grade_card"]({"card_id": card_id, "rating": "Good"})
        assert out.startswith("Graded Good. 0 more due")
        return "Well remembered."

    her.script.append(grade)
    r = await charlotte.event(UID, "speech", said="it means everywhere", now=NOW)
    assert r.graded == [{"card_id": card_id, "rating": "Good"}]
    log = store.q("SELECT source FROM review_log WHERE card_id=?", (card_id,))
    assert [row["source"] for row in log] == ["charlotte"]
    assert charlotte.status(UID, NOW)["due"] == 0


async def test_bad_input(setup):
    agent, llm, charlotte, her = setup
    with pytest.raises(ValueError):
        await charlotte.event(UID, "dance", now=NOW)
    assert (await charlotte.event(UID, "speech", said="   ", now=NOW)).say == ""


def test_clean_speech():
    assert clean_speech("*laughs* Oh, **Geralt**.") == "Oh, Geralt."


async def test_server_owner_only(store, tmp_path):
    import aiohttp
    from aiohttp.test_utils import TestClient, TestServer

    from macaw.reader import server as reader

    store.ensure_user(7, 7, "UTC")
    agent = Agent(store, FakeLLM())

    async def fake_transcribe(audio, name):
        return "" if audio == b"silence" else "hello Charlotte"

    her = FakeLLM()
    srv = reader.ReaderServer("tok", lambda uid: uid in (UID, 7), reader.Library(store, tmp_path), store,
                              charlotte=Charlotte(store, agent, her), owners=frozenset({UID}),
                              transcribe=fake_transcribe)
    c = TestClient(TestServer(srv.build()))
    await c.start_server()
    try:
        key = {"Authorization": "Bearer " + reader.make_token("tok", UID, days=365)}
        guest = {"Authorization": "Bearer " + reader.make_token("tok", 7)}
        assert (await c.get("/api/charlotte/status")).status == 401
        assert (await c.get("/api/charlotte/status", headers=guest)).status == 403
        r = await c.get("/api/charlotte/status", headers=key)
        assert (await r.json())["due"] == 0

        async def hi(prompt, t):
            return "Hello, witcher."

        her.script.append(hi)
        r = await c.post("/api/charlotte/event", headers=key, json={"event": "arrive", "game": {"region": "Skellige"}})
        assert (await r.json())["say"] == "Hello, witcher."
        r = await c.post("/api/charlotte/event", headers=key, json={"event": "nope"})
        assert r.status == 400

        async def reply(prompt, t):
            assert "Geralt: hello Charlotte" in prompt and "region: Skellige" in prompt
            return "Hello again."

        her.script.append(reply)
        form = aiohttp.FormData()
        form.add_field("audio", b"RIFFxxxx", filename="a.wav")
        form.add_field("game", '{"region": "Skellige"}')
        r = await c.post("/api/charlotte/hear", headers=key, data=form)
        data = await r.json()
        assert set(data.pop("timing")) == {"hearing", "thinking"}
        assert data == {"say": "Hello again.", "action": "stay", "journal_saved": False,
                        "graded": [], "heard": "hello Charlotte"}
        # warming up her brain: the fake model has no prepare(), so it's a no-op that still answers ok
        assert (await (await c.post("/api/charlotte/ready", headers=key)).json()) == {"ok": True}
        assert (await c.post("/api/charlotte/ready", headers=guest)).status == 403
        form = aiohttp.FormData()
        form.add_field("audio", b"silence", filename="a.wav")
        r = await c.post("/api/charlotte/hear", headers=key, data=form)
        assert (await r.json())["heard"] == ""
    finally:
        await c.close()


class StreamingLLM(FakeLLM):
    """Like FakeLLM, but writes its reply in pieces through on_text, the way Claude streams."""

    def __init__(self, pieces, tool_first=None):
        super().__init__()
        self.pieces = pieces
        self.tool_first = tool_first

    async def run(self, system, prompt, tools, must_use_tool=False, on_text=None):
        handlers = {t.name: t.handler for t in tools}
        if self.tool_first:
            await on_text("Let me look")  # an unfinished thought before a tool call: dropped, never spoken
            await handlers[self.tool_first[0]](self.tool_first[1])
        for p in self.pieces:
            await on_text(p)
        return "".join(self.pieces)


async def test_sentences_are_spoken_as_they_are_written(store):
    agent = Agent(store, FakeLLM())
    her = StreamingLLM(["Hello there, Geralt. How", " are you? *smiles* I'm well", "."],
                       tool_first=("set_action", {"action": "follow"}))
    charlotte = Charlotte(store, agent, her)
    said = []

    async def on_say(s):
        said.append(s)

    r = await charlotte.event(UID, "arrive", game={}, now=NOW, on_say=on_say)
    assert said == ["Hello there, Geralt.", "How are you?", "I'm well."]
    assert r.say == "Hello there, Geralt. How are you? I'm well." and r.action == "follow"


async def test_streamed_turn_over_http(store, tmp_path):
    import json

    from aiohttp.test_utils import TestClient, TestServer

    from macaw.reader import server as reader

    agent = Agent(store, FakeLLM())
    her = StreamingLLM(["Good evening. ", "Nothing", " more."])
    srv = reader.ReaderServer("tok", lambda uid: uid == UID, reader.Library(store, tmp_path), store,
                              charlotte=Charlotte(store, agent, her), owners=frozenset({UID}))
    c = TestClient(TestServer(srv.build()))
    await c.start_server()
    try:
        key = {"Authorization": "Bearer " + reader.make_token("tok", UID, days=365)}
        r = await c.post("/api/charlotte/event?stream=1", headers=key, json={"event": "arrive"})
        lines = [json.loads(x) for x in (await r.text()).splitlines()]
        assert [x["say"] for x in lines if "say" in x] == ["Good evening.", "Nothing more."]
        assert lines[-1]["done"]["action"] == "stay" and "thinking" in lines[-1]["done"]["timing"]
        assert (await c.post("/api/charlotte/event?stream=1", headers=key, json={"event": "x"})).status == 400
    finally:
        await c.close()

from __future__ import annotations

from types import SimpleNamespace

import pytest
from aiohttp import web

from macaw.agent import Agent
from macaw.bot.app import App
from macaw.config import Config
from macaw.voice import Transcriber, TranscriptionError

from .conftest import UID, FakeLLM


@pytest.fixture
def llm():
    return FakeLLM()


@pytest.fixture
def agent(store, llm):
    return Agent(store, llm)


# ---------- the Whisper call, against a local stand-in for Groq ----------


@pytest.fixture
async def fake_groq():
    """A local server answering like Groq's /audio/transcriptions."""
    seen: dict = {}
    reply = {"status": 200, "body": {"text": " Hello there. "}}

    async def handle(request: web.Request):
        seen["auth"] = request.headers.get("Authorization")
        form = await request.post()
        seen["model"] = form["model"]
        seen["filename"] = form["file"].filename
        seen["audio"] = form["file"].file.read()
        if reply["status"] != 200:
            return web.Response(status=reply["status"], text="slow down")
        return web.json_response(reply["body"])

    app = web.Application()
    app.router.add_post("/openai/v1/audio/transcriptions", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    yield SimpleNamespace(url=f"http://127.0.0.1:{port}/openai/v1", seen=seen, reply=reply)
    await runner.cleanup()


async def test_transcribe_sends_audio_and_returns_text(fake_groq):
    t = Transcriber("gsk_test", "whisper-large-v3", fake_groq.url)
    text = await t.transcribe(b"OggS-audio", "voice.ogg")
    assert text == "Hello there."
    assert fake_groq.seen == {
        "auth": "Bearer gsk_test",
        "model": "whisper-large-v3",
        "filename": "voice.ogg",
        "audio": b"OggS-audio",
    }


async def test_transcribe_error_is_raised(fake_groq):
    fake_groq.reply["status"] = 429
    t = Transcriber("gsk_test", base_url=fake_groq.url)
    with pytest.raises(TranscriptionError, match="429"):
        await t.transcribe(b"x")


async def test_transcribe_unreachable_server():
    t = Transcriber("gsk_test", base_url="http://127.0.0.1:9")
    with pytest.raises(TranscriptionError):
        await t.transcribe(b"x")


# ---------- the bot side ----------


class FakeTranscriber:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def transcribe(self, audio, filename="voice.ogg"):
        self.calls.append((audio, filename))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class VoiceBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append(text)
        return SimpleNamespace(message_id=len(self.sent))

    async def send_chat_action(self, *a, **kw):
        pass

    async def download(self, media, destination):
        destination.write(b"audio-of-" + media.file_id.encode())


def _voice_message(answers):
    async def answer(text, **kw):
        answers.append(text)

    return SimpleNamespace(
        text=None,
        voice=SimpleNamespace(file_id="v1"),
        audio=None,
        video_note=None,
        chat=SimpleNamespace(id=UID),
        from_user=SimpleNamespace(id=UID),
        answer=answer,
    )


def _app(store, agent, transcriber):
    cfg = Config("t", frozenset({UID}), None, "UTC", "fake", None, "INFO")
    app = App(cfg, store, agent, VoiceBot())
    app.transcriber = transcriber
    return app


async def test_voice_message_is_handled_like_typed_text(agent, llm, store):
    t = FakeTranscriber("What's on for today?")
    app = _app(store, agent, t)
    answers = []
    m = _voice_message(answers)
    llm.script.append(lambda p, tools: _ret("Nothing due yet."))

    text = await app._transcribe(m)
    await app._on_text(m, text)

    assert t.calls == [(b"audio-of-v1", "voice.ogg")]
    assert "What's on for today?" in llm.prompts[-1]
    assert app.bot.sent == ["Nothing due yet."]
    assert answers == []


async def test_failed_transcription_tells_the_user(agent, llm, store):
    app = _app(store, agent, FakeTranscriber(TranscriptionError("HTTP 500")))
    answers = []
    assert await app._transcribe(_voice_message(answers)) is None
    assert answers == ["Sorry, I couldn't make out that voice message. Try again, or type it."]
    assert llm.prompts == []


async def test_silent_voice_message(agent, llm, store):
    app = _app(store, agent, FakeTranscriber(""))
    answers = []
    assert await app._transcribe(_voice_message(answers)) is None
    assert answers == ["I didn't hear any words in that voice message."]


async def test_no_key_means_no_voice(agent, llm, store):
    app = _app(store, agent, None)
    answers = []
    assert await app._transcribe(_voice_message(answers)) is None
    assert "no Groq key" in answers[0]


def test_voice_key_reuses_the_groq_free_key(monkeypatch):
    from macaw.config import _voice_key

    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("FREE_LLM_BASE_URL", raising=False)
    monkeypatch.setenv("FREE_LLM_API_KEY", "gsk_free")
    assert _voice_key() == "gsk_free"
    monkeypatch.setenv("FREE_LLM_BASE_URL", "https://example.com/v1")
    assert _voice_key() is None
    monkeypatch.setenv("GROQ_API_KEY", "gsk_own")
    assert _voice_key() == "gsk_own"


async def _ret(text):
    return text

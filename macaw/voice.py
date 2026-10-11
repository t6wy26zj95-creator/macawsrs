"""Speech to text for voice messages, with Groq's free Whisper API.

Telegram doesn't give bots the text of a voice message, only the audio, so we
send the audio to Whisper and treat what comes back as if it had been typed.
Groq's free tier allows 2,000 transcriptions and two hours of audio an hour.
"""

from __future__ import annotations

import asyncio
import logging

import aiohttp

log = logging.getLogger(__name__)

GROQ_URL = "https://api.groq.com/openai/v1"
DEFAULT_MODEL = "whisper-large-v3"
TIMEOUT = 60


class TranscriptionError(Exception):
    pass


class Transcriber:
    def __init__(self, api_key: str, model: str = DEFAULT_MODEL, base_url: str = GROQ_URL):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")

    async def transcribe(self, audio: bytes, filename: str = "voice.ogg") -> str:
        """The words spoken in `audio`, or "" if there were none."""
        form = aiohttp.FormData()
        form.add_field("file", audio, filename=filename)
        form.add_field("model", self.model)
        form.add_field("response_format", "json")
        form.add_field("temperature", "0")
        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as session:
                async with session.post(f"{self.base_url}/audio/transcriptions", data=form, headers=headers) as r:
                    if r.status != 200:
                        raise TranscriptionError(f"HTTP {r.status}: {(await r.text())[:300]}")
                    data = await r.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise TranscriptionError(str(e) or type(e).__name__) from e
        return (data.get("text") or "").strip()

"""Runtime configuration, read from environment variables (see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


# The best model first; each next one has a bigger free allowance. Flash: 20
# requests a day, each Flash-Lite: about 500 a day, all counted separately.
GEMINI_MODELS = ("gemini-3.8-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite")
# What .env.example used to suggest; gemini-2.5-flash no longer works for new keys.
OLD_GEMINI_EXAMPLE = ("gemini-3.8-flash", "gemini-2.5-flash")
NVIDIA_MODELS = ("openai/gpt-oss-120b", "nvidia/nemotron-3-super-120b-a12b", "openai/gpt-oss-20b")


@dataclass(frozen=True)
class Config:
    bot_token: str
    owner_ids: frozenset[int]
    db_path: Path
    default_timezone: str
    llm_provider: str
    claude_model: str | None
    log_level: str
    # People who may use the bot on the free model only (never Claude).
    guest_ids: frozenset[int] = frozenset()
    free_api_key: str | None = None
    free_base_url: str = "https://api.groq.com/openai/v1"
    free_model: str = "openai/gpt-oss-120b"
    # Tried in order when the one before is at its rate limit.
    free_fallback_models: tuple[str, ...] = ("openai/gpt-oss-20b",)
    # Gemini's free tier, tried before the models above when it has a key.
    gemini_api_key: str | None = None
    gemini_models: tuple[str, ...] = GEMINI_MODELS
    # NVIDIA's free API (build.nvidia.com): about 40 requests a minute, no daily cap.
    nvidia_api_key: str | None = None
    nvidia_models: tuple[str, ...] = NVIDIA_MODELS
    # Groq key for turning voice messages into text (Whisper). Falls back to
    # FREE_LLM_API_KEY when that one is a Groq key.
    voice_api_key: str | None = None
    voice_model: str = "whisper-large-v3"
    # Book reader Mini App: its public https address (empty = not set up yet) and
    # the local port its web server listens on.
    reader_url: str | None = None
    reader_port: int = 8090

    @property
    def free_label(self) -> str:
        keys = (("Gemini", self.gemini_api_key), ("NVIDIA", self.nvidia_api_key), ("Groq", self.free_api_key))
        names = [name for name, key in keys if key]
        if not names:
            return "not set up"
        if len(names) == 1:
            return names[0]
        return f"{names[0]}, with {' and '.join(names[1:])} as backup"

    @property
    def allowed_ids(self) -> frozenset[int]:
        return self.owner_ids | self.guest_ids


def _parse_ids(raw: str) -> frozenset[int]:
    ids = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part:
            ids.add(int(part))
    return frozenset(ids)


def _parse_list(raw: str) -> tuple[str, ...]:
    return tuple(m.strip() for m in raw.split(",") if m.strip())


def _gemini_models(raw: str) -> tuple[str, ...]:
    models = _parse_list(raw)
    return GEMINI_MODELS if not models or models == OLD_GEMINI_EXAMPLE else models


def load_config() -> Config:
    token = os.environ.get("BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("BOT_TOKEN is not set. Put it in the .env file.")
    owners = _parse_ids(os.environ.get("OWNER_IDS", ""))
    if not owners:
        raise SystemExit("OWNER_IDS is not set. Put your Telegram user ID in the .env file.")
    return Config(
        bot_token=token,
        owner_ids=owners,
        db_path=Path(os.environ.get("DB_PATH", "data/macaw.sqlite3")),
        default_timezone=os.environ.get("DEFAULT_TIMEZONE", "UTC"),
        llm_provider=os.environ.get("LLM_PROVIDER", "claude"),
        claude_model=os.environ.get("CLAUDE_MODEL") or None,
        log_level=os.environ.get("LOG_LEVEL", "INFO"),
        # An ID in both lists counts as an owner.
        guest_ids=_parse_ids(os.environ.get("GUEST_IDS", "")) - owners,
        free_api_key=os.environ.get("FREE_LLM_API_KEY", "").strip() or None,
        free_base_url=os.environ.get("FREE_LLM_BASE_URL") or "https://api.groq.com/openai/v1",
        free_model=os.environ.get("FREE_LLM_MODEL") or "openai/gpt-oss-120b",
        free_fallback_models=_parse_list(os.environ.get("FREE_LLM_FALLBACK_MODELS", "openai/gpt-oss-20b")),
        gemini_api_key=os.environ.get("GEMINI_API_KEY", "").strip() or None,
        gemini_models=_gemini_models(os.environ.get("GEMINI_MODELS", "")),
        nvidia_api_key=os.environ.get("NVIDIA_API_KEY", "").strip() or None,
        nvidia_models=_parse_list(os.environ.get("NVIDIA_MODELS", "")) or NVIDIA_MODELS,
        voice_api_key=_voice_key(),
        voice_model=os.environ.get("VOICE_MODEL") or "whisper-large-v3",
        reader_url=_reader_url(os.environ.get("READER_URL", "")),
        reader_port=int(os.environ.get("READER_PORT") or 8090),
    )


def _reader_url(raw: str) -> str | None:
    url = raw.strip().rstrip("/")
    # Telegram only opens Mini Apps over https.
    return url + "/" if url.startswith("https://") else None


def _voice_key() -> str | None:
    key = os.environ.get("GROQ_API_KEY", "").strip()
    if key:
        return key
    free_url = os.environ.get("FREE_LLM_BASE_URL") or "https://api.groq.com/openai/v1"
    if "groq.com" in free_url:
        return os.environ.get("FREE_LLM_API_KEY", "").strip() or None
    return None

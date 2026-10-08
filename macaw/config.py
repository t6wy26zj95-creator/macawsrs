"""Runtime configuration, read from environment variables (see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


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
    )

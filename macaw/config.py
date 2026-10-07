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
    )

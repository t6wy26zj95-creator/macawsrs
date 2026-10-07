"""Entry point: python -m macaw"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.types import BotCommand

from .agent import Agent
from .bot.app import App, build_dispatcher
from .config import load_config
from .db import Store
from .llm import make_provider


async def main() -> None:
    config = load_config()
    logging.basicConfig(
        level=config.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Never log message text or tokens from the HTTP layer.
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)

    store = Store(config.db_path)
    store.prune_messages()
    agent = Agent(store, make_provider(config.llm_provider, config.claude_model))
    bot = Bot(config.bot_token)
    app = App(config, store, agent, bot)
    dp = build_dispatcher(app)

    await bot.set_my_commands(
        [
            BotCommand(command="decks", description="Your decks and their settings"),
            BotCommand(command="review", description="Review a card now"),
            BotCommand(command="settings", description="Timezone, quiet hours, reminders"),
        ]
    )
    ticker = asyncio.create_task(app.ticker())
    try:
        # Long polling: outbound connections only, no open ports.
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
    finally:
        ticker.cancel()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())

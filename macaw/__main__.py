"""Entry point: python -m macaw"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from aiogram import Bot
from aiogram.types import BotCommand, BotCommandScopeChat

from .agent import Agent
from .bot.app import App, build_dispatcher
from .config import load_config
from .db import Store
from .llm import Models, make_free_provider, make_provider
from .reader import server as reader


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
    claude = make_provider(config.llm_provider, config.claude_model)
    free = make_free_provider(
        config.free_api_key,
        config.free_base_url,
        config.free_model,
        config.free_fallback_models,
        config.gemini_api_key,
        config.gemini_models,
        config.nvidia_api_key,
        config.nvidia_models,
    )
    if config.guest_ids and free is None:
        logging.warning("GUEST_IDS is set but no free model key (GEMINI_API_KEY, NVIDIA_API_KEY, FREE_LLM_API_KEY) is set: guests can't chat yet")
    agent = Agent(store, claude, Models(config.owner_ids, claude, free))
    bot = Bot(config.bot_token)
    app = App(config, store, agent, bot)
    dp = build_dispatcher(app)

    commands = [
        BotCommand(command="decks", description="Your decks and their settings"),
        BotCommand(command="review", description="Review a card now"),
        BotCommand(command="export", description="Export decks as an Anki file"),
        BotCommand(command="books", description="Open your book library"),
        BotCommand(command="settings", description="Timezone, quiet hours, reminders"),
        BotCommand(command="model", description="Which AI model you're using"),
    ]
    await bot.set_my_commands(commands)
    owner_commands = commands + [
        BotCommand(command="invite", description="Invite link for a friend"),
        BotCommand(command="guests", description="Friends using the bot"),
    ]
    for owner in config.owner_ids:
        # Fails if the owner hasn't opened the bot yet; the default menu still works.
        with contextlib.suppress(Exception):
            await bot.set_my_commands(owner_commands, scope=BotCommandScopeChat(chat_id=owner))
    web = await reader.start(
        reader.ReaderServer(config.bot_token, app._allowed, app.library, store,
                            explain=app.explain_word, add_word=app.add_book_word),
        "127.0.0.1",
        config.reader_port,
    )
    ticker = asyncio.create_task(app.ticker())
    # The Books button next to the text field opens the reader once it has an https address.
    url_watch = asyncio.create_task(app.watch_reader_url())
    try:
        # Long polling: outbound connections only, no open ports.
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
    finally:
        ticker.cancel()
        url_watch.cancel()
        if web:
            await web.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())

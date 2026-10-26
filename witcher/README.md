# Charlotte, a Witcher 3 companion

Charlotte finds Geralt at calm moments in towns, talks with him by voice, walks with him on request and tests his memory on the same cards as the Telegram bot.

- `modCharlotte/`: the game mod (WitcherScript, game 5.0). Copy it to `<game>\mods\`.
- `helper/`: the program that runs next to the game on the PC. It links the game (script debug channel, port 37001), the mic, her voice (free Microsoft natural voices) and the bot. `setup.bat` once, then `Charlotte.bat` to play; `ButtonTest.bat` shows controller button names.
- The bot side is `macaw/charlotte.py` and the `/api/charlotte/*` routes on the reader server. `/charlotte` in Telegram gives the pairing code the helper asks for.

The game must run with `-net -debugscripts` (the helper starts it that way).

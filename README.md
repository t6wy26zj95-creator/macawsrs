# macawsrs

Macaw is a Telegram bot for spaced repetition that talks instead of flipping flashcards. Claude adds cards for you, brings up due cards casually during the day, grades your answers the way you'd grade yourself in Anki (Again / Hard / Good / Easy, correctable with one tap), and the code schedules the next review with FSRS, the algorithm Anki uses.

## What it does

- **Onboarding:** /start gives a short overview and suggests creating a first deck.
- **Cards by chat:** "add the word ubiquitous to my English deck". Claude fills the deck's template (vocabulary: word, meaning, example, notes), checks for duplicates, and shows a preview with Add / Edit / Skip.
- **Conversational reviews:** due cards come up naturally in conversation. Claude never reveals an answer before asking; if a due card's answer comes up anyway, the card is postponed to tomorrow instead of graded.
- **Pacing:** one card at a time by default, with pauses spread so every due card is reviewed by the end of the day. Ask for more ("let's do 5") or change "cards per session". Wrong answers come back the same day (Anki learning steps).
- **Reminders:** if you go quiet on a question, reminders follow with doubling gaps (1 h, 2 h, 4 h ...), up to 4 a day, getting playfully annoyed. Nothing is sent during quiet hours (default 00:00 to 08:00), but you can always study at night if you write first.
- **/decks:** one message with all decks; tap through card lists, daily limits, template, reverse cards, rename and delete. Every tap edits the same message.
- **/settings** shows timezone, quiet hours and reminder settings; change them by telling the bot.

Planned next: .apkg import/export for Anki and AnkiMobile, and an ebook reader.

## How it is built

| Part | File |
| --- | --- |
| Telegram handlers, /decks menu, timer loop | `macaw/bot/` |
| Conversation engine and Claude's tools | `macaw/agent.py`, `macaw/prompts.py` |
| FSRS scheduling, due queue, day boundaries | `macaw/srs.py` |
| Pacing and reminder rules | `macaw/pacing.py` |
| Answer-leak check | `macaw/leak.py` |
| Storage (SQLite) | `macaw/db.py` |
| Claude via your Pro subscription | `macaw/llm/claude.py` |
| Free model (Gemini, Groq or any OpenAI-style API) | `macaw/llm/openai_compat.py` |

Claude can only change anything through the tools in `agent.py`. Due dates are always computed by FSRS in code. The Claude provider runs Claude Code through the Claude Agent SDK with no built-in tools (no shell, no file access), authenticated with your Pro subscription token. Other model providers can be added next to it in `macaw/llm/`.

**Claude and the free model.** People in `OWNER_IDS` use Claude by default and can switch themselves to the free model and back with /model. Friends join with a one-time link from /invite (or by ID in `GUEST_IDS`); they always use the free model and can never be switched to Claude, so your Pro subscription is only ever used for you. Everyone gets their own decks, reminders and settings. /guests lists friends and removes them. Cards and chat history live in the bot's database, not in the model, so switching models mid-conversation loses nothing. The free model is Gemini (`GEMINI_API_KEY` from aistudio.google.com/apikey) and/or Groq (`FREE_LLM_API_KEY` from console.groq.com/keys). With both keys, Gemini answers first and Groq takes over whenever Gemini is busy or failing.

## Setup on the VPS

The bot runs as its own `macaw` user, lives entirely in `/opt/macaw`, runs as the `macaw` systemd service, and uses long polling, so it opens no ports and needs no firewall changes. It doesn't touch any other service.

**Before you start**, have these ready:

1. A bot token from [@BotFather](https://t.me/BotFather) (`/newbot`).
2. Your Telegram user ID from [@userinfobot](https://t.me/userinfobot).
3. A Claude token: on a computer with Claude Code logged in to your Pro account, run `claude setup-token` and copy the token.

**On the server:**

```bash
# Python 3.10+ with venv, and git (skip if already installed)
sudo apt install -y python3-venv git

# Get the installer and run it
git clone https://github.com/t6wy26zj95-creator/macawsrs.git /tmp/macawsrs
sudo bash /tmp/macawsrs/deploy/install.sh

# Fill in the tokens, your Telegram ID and your timezone
sudo nano /opt/macaw/.env

# Start it and watch the log
sudo systemctl restart macaw
sudo journalctl -u macaw -f
```

Then open your bot in Telegram and press Start.

**Updating** to the latest code: `sudo bash /opt/macaw/app/deploy/update.sh`

**Useful commands**

```bash
sudo systemctl status macaw     # is it running?
sudo journalctl -u macaw -n 100 # last 100 log lines
sudo systemctl stop macaw       # stop the bot
```

The database is `/opt/macaw/data/macaw.sqlite3`; a copy is saved daily in `/opt/macaw/data/backups` (last 14 days kept).

The service is capped at 768 MB of memory and half a CPU core so it can't starve the other bots on the server. Adjust `MemoryMax` and `CPUQuota` in `deploy/macaw.service` if needed.

## Development

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest
```

Tests use a scripted fake in place of Claude, so they run offline.

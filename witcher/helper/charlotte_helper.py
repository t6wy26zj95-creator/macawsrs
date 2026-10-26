"""Charlotte's helper: runs next to The Witcher 3 and connects the game, your mic, her voice and the bot.

Start it with Charlotte.bat. It starts the game too (with the settings the mod needs), then waits for
a calm moment in a town, sends Charlotte to you, and handles the conversation.
"""

from __future__ import annotations

import configparser
import json
import logging
import os
import queue
import random
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid

from pad import MuteButton, names
from voice import Ears, Voice, place_voice
from w3link import GameLink, GameNotRunning, parse_fields

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "charlotte.ini")
log = logging.getLogger("charlotte")

DEFAULTS = {
    "bot": {"code": ""},
    "game": {"folder": r"C:\games\The Witcher 3 Wild Hunt GOTY", "start_game": "yes",
             # who she looks like: an elegant lady; "appearance" keeps the same face every visit (saved on the first)
             "look": "rich_woman", "appearance": ""},
    "voice": {"voice": "en-GB-SoniaNeural", "speed": "+0%", "swap_sides": "no", "reactions": "yes"},
    "mute": {"combo": "LEFT_STICK+RIGHT_STICK", "key": "F9"},
    "timing": {"calm_seconds": "120", "minutes_between": "25", "quiet_nudge_seconds": "120"},
}

REGIONS = {
    "AN_Velen": "Velen (No Man's Land)", "AN_NMLandNovigrad": "Novigrad and Oxenfurt",
    "AN_Skellige_ArdSkellig": "the Skellige Isles", "AN_Kaer_Morhen": "Kaer Morhen",
    "AN_Prologue_Village": "White Orchard", "AN_Prologue_Village_Winter": "White Orchard",
    "AN_Wyzima": "Vizima", "AN_Bob": "Toussaint", "AN_Island_of_Myst": "the Isle of Mists",
    "AN_Spiral": "the Spiral",
}


def load_config() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read_dict(DEFAULTS)
    cfg.read(CONFIG, encoding="utf-8")
    if cfg["game"]["look"] == "sorceress":  # the first version's look, which turned out to be Yennefer
        cfg["game"]["look"] = DEFAULTS["game"]["look"]
        cfg["game"]["appearance"] = ""
    return cfg


def save_config(cfg: configparser.ConfigParser) -> None:
    with open(CONFIG, "w", encoding="utf-8") as f:
        cfg.write(f)


# ---------------- the bot ----------------

class Bot:
    def __init__(self, code: str):
        url, _, token = code.strip().partition("|")
        if not url.startswith("https://") or not token:
            raise ValueError("That doesn't look like a pairing code. Send /charlotte to the bot to get one.")
        self.base = url.rstrip("/") + "/api/charlotte/"
        self.token = token

    def _send(self, path: str, data: bytes | None = None, ctype: str = "application/json", timeout: int = 90):
        req = urllib.request.Request(self.base + path, data=data, method="POST" if data is not None else "GET")
        req.add_header("Authorization", "Bearer " + self.token)
        if data is not None:
            req.add_header("Content-Type", ctype)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def _stream(self, path: str, data: bytes, ctype: str = "application/json", timeout: int = 90):
        """Her reply as it comes: one JSON object per line ({"heard"}, {"say"} per sentence, {"done"})."""
        req = urllib.request.Request(self.base + path + "?stream=1", data=data, method="POST")
        req.add_header("Authorization", "Bearer " + self.token)
        req.add_header("Content-Type", ctype)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            for line in r:
                line = line.strip()
                if line:
                    yield json.loads(line.decode("utf-8"))

    def event_stream(self, kind: str, game: dict, said: str = ""):
        return self._stream("event", json.dumps({"event": kind, "said": said, "game": game}).encode("utf-8"))

    def hear_stream(self, wav: bytes, game: dict):
        body, ctype = self._form(wav, game)
        return self._stream("hear", body, ctype)

    def ready(self) -> dict:
        return self._send("ready", b"{}", timeout=20)

    def status(self) -> dict:
        return self._send("status", timeout=20)

    def event(self, kind: str, game: dict, said: str = "") -> dict:
        body = json.dumps({"event": kind, "said": said, "game": game}).encode("utf-8")
        return self._send("event", body)

    def hear(self, wav: bytes, game: dict) -> dict:
        body, ctype = self._form(wav, game)
        return self._send("hear", body, ctype)

    def _form(self, wav: bytes, game: dict) -> tuple[bytes, str]:
        boundary = uuid.uuid4().hex
        parts = [
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"game\"\r\n\r\n".encode() + json.dumps(game).encode(),
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"audio\"; filename=\"speech.wav\"\r\n"
            "Content-Type: audio/wav\r\n\r\n".encode() + wav,
        ]
        body = b"\r\n".join(parts) + f"\r\n--{boundary}--\r\n".encode()
        return body, f"multipart/form-data; boundary={boundary}"


# ---------------- the helper ----------------

MIC_HELP = ("Couldn't open your microphone, so Charlotte won't hear you yet. The helper keeps running and tries "
            "again every 30 seconds.\nThe usual cause: Windows blocks the mic for desktop apps. Open Settings > "
            "Privacy & security > Microphone and turn on 'Microphone access' and 'Let desktop apps access your "
            "microphone'. Also check your headset is plugged in and set as the input in Settings > System > Sound.")

IDLE, APPROACH, TALK = "idle", "approach", "talk"


class Helper:
    def __init__(self, cfg: configparser.ConfigParser):
        self.cfg = cfg
        self.bot = Bot(cfg["bot"]["code"])
        self.game = GameLink()
        self.voice = Voice(cfg["voice"]["voice"], cfg["voice"]["speed"])
        self.ears = Ears(self.on_speech)
        self.mute = MuteButton(cfg["mute"]["combo"], cfg["mute"]["key"])
        self.jobs: queue.Queue = queue.Queue()
        self.state = IDLE
        self.muted = False
        self.fields: dict[str, str] = {}
        self.story: list[str] = []
        self.due = 0
        self.met = False
        self.status_at = 0.0
        self.reported_at = 0.0
        self.next_visit = 0.0  # no visit before this time
        self.spawned_at = 0.0
        self.last_heard = 0.0
        self.nudged = False
        self.away_since = None
        self.calm_seconds = cfg["timing"].getint("calm_seconds")
        self.between = cfg["timing"].getint("minutes_between") * 60
        self.quiet_nudge = cfg["timing"].getint("quiet_nudge_seconds")

    # ----- game -----

    def call(self, func: str, *args, timeout: float = 3.0) -> list[str]:
        return self.game.call(func, *args, timeout=timeout)

    def note(self, text: str) -> None:
        try:
            self.call("chl_note", text)
        except Exception:
            pass

    def game_info(self) -> dict:
        f = self.fields
        info = {
            "region": REGIONS.get(f.get("area", ""), f.get("area", "")),
            "place": "in a town or village" if f.get("settle") == "1" else "outside town",
            "weather": f.get("weather", ""),
            "level": f.get("level", ""),
            "coin": f.get("coin", ""),
            "quest": f.get("quest", ""),
            "quest_goal": f.get("goal", ""),
            "finished_quests": "; ".join(self.story[-25:]),
        }
        if f.get("hour", "").isdigit():
            h = int(f["hour"])
            part = "night" if h < 5 or h >= 22 else "morning" if h < 12 else "afternoon" if h < 18 else "evening"
            info["time"] = f"{h}:00, {part}"
        return {k: v for k, v in info.items() if v}

    def poll(self, full: bool = False) -> None:
        self.fields.update(parse_fields(self.call("chl_poll", 1 if full else 0)))

    def read_story(self) -> None:
        titles, offset = [], 0
        while offset < 200:
            lines = self.call("chl_story", offset)
            page = [line[5:] for line in lines if line.startswith("done ")]
            titles += page
            if len(page) < 12:
                break
            offset += 12
        self.story = titles

    # ----- the conversation (one job at a time, on the worker thread) -----

    def worker(self) -> None:
        while True:
            job = self.jobs.get()
            try:
                job()
            except urllib.error.HTTPError as e:
                log.warning("bot said %s", e)
                self.note("Charlotte: can't reach the bot (send /charlotte for a new pairing code?)")
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                log.warning("bot unreachable: %s", e)
                self.note("Charlotte: no connection to the bot right now")
            except Exception:
                log.exception("job failed")

    def keep_face(self) -> None:
        """The same face every visit: remember the first one she had, put it back on later visits."""
        saved = self.cfg["game"]["appearance"]
        if saved:
            self.call("chl_look", saved)
            return
        fields = parse_fields(self.call("chl_where"))
        look = fields.get("where", "").partition(" look ")[2].split(" visible")[0].strip()
        if look:
            self.cfg["game"]["appearance"] = look
            save_config(self.cfg)
            log.info("her look from now on: %s", look)

    def where_she_is(self) -> tuple[float, float]:
        left, right = place_voice(self.fields.get("ear", ""))
        if self.cfg["voice"].getboolean("swap_sides"):
            left, right = right, left
        return left, right

    def in_menu(self) -> bool:
        return self.fields.get("busy") in ("menu", "paused", "fading")

    def end_visit(self) -> None:
        self.state = IDLE
        self.ears.enabled = False
        self.next_visit = time.time() + self.between
        self.status_at = 0.0

    def on_speech(self, wav: bytes) -> None:
        if self.state != TALK or self.muted:
            return
        stopped = time.monotonic()  # you just stopped talking
        self.last_heard = time.time()
        self.nudged = False
        if self.cfg["voice"].getboolean("reactions") and random.random() < 0.7:
            self.voice.react(self.where_she_is)

        def job():
            self.speak_stream(self.bot.hear_stream(wav, self.game_info()), stopped)

        self.jobs.put(job)

    def send_event(self, kind: str) -> None:
        self.jobs.put(lambda: self.speak_stream(self.bot.event_stream(kind, self.game_info()), time.monotonic()))

    def speak_stream(self, replies, since: float) -> None:
        """Speaks her sentences as they arrive (the next one is voiced while the current one plays),
        then does what she decided."""
        speaker = self.voice.speaker(self.where_she_is)
        done: dict = {}
        first = True
        self.ears.paused = True
        try:
            for obj in replies:
                if "heard" in obj:
                    log.info("You: %s", obj["heard"])
                elif "done" in obj or "action" in obj:  # the end (or an older bot that answers all at once)
                    done = obj.get("done") or obj
                    if "action" in obj and obj.get("say"):
                        speaker.add(obj["say"])
                        log.info("Charlotte: %s", obj["say"])
                elif "say" in obj and self.state != IDLE:
                    if first:
                        log.info("(first words after %.1fs; the bot had it %ss into its turn)",
                                 time.monotonic() - since, obj.get("at", "?"))
                        first = False
                    self.wait_for_menu()
                    log.info("Charlotte: %s", obj["say"])
                    speaker.add(obj["say"])
            speaker.finish()
        finally:
            speaker.stop()
            self.ears.paused = False
        t = done.get("timing") or {}
        if t:
            log.info("(hearing %ss, thinking %ss)", t.get("hearing", "-"), t.get("thinking", "?"))
        if self.state == IDLE:
            return
        action = done.get("action") or "stay"
        try:
            self.call("chl_action", action)
        except Exception as e:
            log.warning("couldn't tell the game %s: %s", action, e)
        if action == "leave":
            self.end_visit()

    def wait_for_menu(self) -> None:
        waited = 0.0
        while self.in_menu() and self.state != IDLE and waited < 600:
            time.sleep(0.3)  # she waits until you're back from the menu
            waited += 0.3

    # ----- the main loop -----

    def visit_allowed(self) -> bool:
        f = self.fields
        return (
            int(f.get("calmfor", "0")) >= self.calm_seconds
            and f.get("danger") == "0" and f.get("settle") == "1" and not f.get("busy")
            and time.time() >= self.next_visit and self.due > 0
        )

    def why_not(self) -> str:
        """What is still missing for a visit, in plain words (printed every 30 s while she isn't around)."""
        f, missing = self.fields, []
        calm = int(f.get("calmfor", "0"))
        if calm < self.calm_seconds:
            missing.append(f"calm for {calm} of {self.calm_seconds} seconds")
        if f.get("danger") != "0":
            missing.append("danger nearby (fight, enemies, dialogue or cutscene)")
        if f.get("settle") != "1":
            missing.append("not in a town or village")
        if f.get("busy"):
            missing.append("busy: " + f["busy"])
        if self.due <= 0:
            missing.append("no cards due")
        wait = int(self.next_visit - time.time())
        if wait > 0:
            missing.append(f"next visit allowed in {wait // 60} min")
        return "; ".join(missing) or "ready, she's coming"

    def tick(self) -> None:
        self.poll()
        f = self.fields
        now = time.time()
        if f.get("ingame") == "0":  # main menu or loading
            self.state = IDLE
            self.ears.enabled = False
            return
        if self.state == IDLE:
            if now - self.status_at > 180:
                s = self.bot.status()
                self.due, self.met, self.status_at = s.get("due", 0), s.get("met", False), now
            if now - self.reported_at >= 30:
                self.reported_at = now
                log.info("Waiting for a visit: %s", self.why_not())
            if self.visit_allowed():
                lines = self.call("chl_spawn", self.cfg["game"]["look"])
                if any(line.startswith("error") for line in lines):
                    log.warning("Charlotte couldn't appear: %s", lines)
                    self.next_visit = now + 300
                    return
                self.jobs.put(self.bot.ready)  # her brain gets ready while she walks up
                self.keep_face()
                log.info("Charlotte is on her way: %s", " ".join(self.call("chl_where")))
                self.state, self.spawned_at = APPROACH, now
            return

        if f.get("danger") == "1":
            log.info("something happened in the game: Charlotte slips away")
            self.call("chl_action", "vanish")
            self.state = IDLE
            self.ears.enabled = False
            self.jobs.put(lambda: self.bot.event("interrupted", self.game_info()))
            self.next_visit = now + self.between
            return

        near = int(f.get("near10", "-1"))
        if self.state == APPROACH:
            if 0 <= near <= 35:
                log.info("Charlotte arrived: %s", " ".join(self.call("chl_where")))
                self.call("chl_face")
                self.poll(full=True)
                self.read_story()
                self.state, self.last_heard, self.nudged = TALK, now, False
                self.ears.enabled = not self.muted
                self.send_event("arrive" if self.met else "first_meeting")
                self.met = True
            elif now - self.spawned_at > 40:
                self.call("chl_closer")
                self.spawned_at = now
            return

        # TALK
        if f.get("her") != "1":
            self.end_visit()
            return
        if f.get("follow") != "1" and near > 300:
            self.away_since = self.away_since or now
            if now - self.away_since > 20:
                log.info("you walked off: Charlotte leaves")
                self.call("chl_action", "leave")
                self.jobs.put(lambda: self.bot.event("walked_away", self.game_info()))
                self.end_visit()
                return
        else:
            self.away_since = None
        self.ears.enabled = not self.muted and not self.in_menu()
        if self.in_menu():
            self.last_heard = now  # time in a menu doesn't count as silence
            return
        if not self.nudged and now - self.last_heard > self.quiet_nudge and self.jobs.empty():
            self.nudged = True
            self.send_event("silence")

    def toggle_mute(self) -> None:
        self.muted = not self.muted
        self.ears.enabled = self.state == TALK and not self.muted
        self.note("Mic off" if self.muted else "Mic on")
        log.info("mic %s", "off" if self.muted else "on")
        if self.state == TALK:
            self.send_event("mic_off" if self.muted else "mic_on")

    def run(self) -> None:
        threading.Thread(target=self.worker, daemon=True).start()
        self.voice.prepare_reactions()
        mic_tried = time.time()
        if self.ears.start():
            log.info("mic ready")
        else:
            print(MIC_HELP + "\n(" + self.ears.error + ")")
        last_tick = 0.0
        waiting_said = False
        greeted = False
        while True:
            if self.mute.pressed():
                self.toggle_mute()
            if self.ears.stream is None and time.time() - mic_tried > 30:
                mic_tried = time.time()
                if self.ears.start():
                    print("The mic works now.")
            if time.time() - last_tick >= 1.0:
                last_tick = time.time()
                try:
                    self.tick()
                    if waiting_said:
                        log.info("Connected to the game. Charlotte will find you at a calm moment in a town.")
                        waiting_said = False
                        greeted = False
                    if not greeted and self.fields.get("ingame") == "1":
                        self.call("chl_note", "Charlotte is in the world")
                        greeted = True
                except GameNotRunning:
                    if not waiting_said:
                        print("Waiting for the game...")
                        waiting_said = True
                    self.state = IDLE
                    self.ears.enabled = False
                    time.sleep(4)
                except (TimeoutError, ValueError) as e:
                    log.warning("game: %s", e)
                    self.game.close()
                except (urllib.error.URLError, OSError) as e:
                    log.warning("bot: %s", e)
                    self.status_at = time.time() - 120  # try again in a minute
            time.sleep(0.05)


# ---------------- setup steps ----------------

def ask_code(cfg) -> None:
    while True:
        code = input("Paste your pairing code from the bot (send /charlotte to it in Telegram), then press Enter:\n> ")
        try:
            Bot(code)
        except ValueError as e:
            print(e)
            continue
        cfg["bot"]["code"] = code.strip()
        save_config(cfg)
        return


def start_game(cfg) -> None:
    exe = os.path.join(cfg["game"]["folder"], "bin", "x64_dx12", "witcher3.exe")
    if not os.path.exists(exe):
        print(f"Couldn't find the game at {exe}. Fix 'folder' in charlotte.ini, or start the game yourself "
              "with -net -debugscripts.")
        return
    try:
        GameLink().connect()
        return  # already running with the debug channel
    except GameNotRunning:
        pass
    print("Starting The Witcher 3...")
    subprocess.Popen([exe, "-net", "-debugscripts"], cwd=os.path.dirname(exe))


def button_test() -> None:
    print("Press buttons on your controller to see their names. Ctrl+C to stop.")
    pad, last = MuteButton(), -1
    while True:
        held = pad.buttons()
        if held != last:
            print("Held:", ", ".join(names(held)) or "nothing")
            last = held
        time.sleep(0.05)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S",
                        handlers=[logging.StreamHandler(), logging.FileHandler(os.path.join(HERE, "charlotte.log"),
                                                                               encoding="utf-8")])
    if "--buttons" in sys.argv:
        button_test()
        return
    cfg = load_config()
    if not cfg["bot"]["code"]:
        ask_code(cfg)
    if cfg["game"].getboolean("start_game"):
        start_game(cfg)
    print("Charlotte's helper is running. Keep this window open while you play.")
    print(f"Mute or unmute the mic: {cfg['mute']['combo'].replace('+', ' + ')} on the controller, or {cfg['mute']['key']}.")
    Helper(cfg).run()


if __name__ == "__main__":
    main()

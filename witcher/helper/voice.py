"""Charlotte's ears and voice on the PC.

Ears: the mic is read all the time while she's around; when you start talking (louder than the
room for a moment) recording starts, and a short pause ends it. The recording goes to the bot,
which turns it into text. With headphones the mic can't hear the game or Charlotte.

Voice: Microsoft's free natural voices (the same ones Edge's "Read aloud" uses), played through
Windows itself, so no extra player is needed.
"""

from __future__ import annotations

import asyncio
import ctypes
import io
import math
import os
import queue
import random
import tempfile
import threading
import time
import wave

import numpy as np
import sounddevice as sd

RATE = 16000
FRAME = 480  # 30 ms
PRE_ROLL = 10  # frames kept from just before you started talking
START_FRAMES = 5  # 150 ms of loud sound starts a recording
END_SILENCE = 0.7  # seconds of quiet that end it
MAX_SECONDS = 30
MIN_SECONDS = 0.4


class Ears:
    """Calls `on_speech(wav_bytes)` from a background thread each time you finish saying something."""

    def __init__(self, on_speech, device=None):
        self.on_speech = on_speech
        self.device = device
        self.enabled = False  # listening only while Charlotte is around and the mic isn't muted
        self.paused = False  # while she speaks
        self.chunks: queue.Queue = queue.Queue()
        self.in_rate = RATE
        self.error = ""
        self.floor = 300.0  # background loudness, learned as it goes
        self.stream = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> bool:
        """Opens the mic. Returns False (and keeps the helper running) if no way of opening it worked."""
        for device, rate in self._options():
            try:
                stream = sd.InputStream(samplerate=rate, channels=1, dtype="float32", device=device,
                                        callback=self._on_audio)
                stream.start()
            except Exception as e:  # PortAudio errors differ by driver
                self.error = str(e)
                continue
            self.stream, self.in_rate, self.device = stream, rate, device
            if not self.thread.is_alive():
                self.thread.start()
            return True
        return False

    def _options(self):
        """Ways to open the mic, most likely to work first: the chosen or default mic at its own rate,
        then the same mic through other Windows audio systems, then any other mic."""
        tried, out = set(), []

        def add(index):
            if index is None or index < 0 or index in tried:
                return
            try:
                info = sd.query_devices(index)
            except Exception:
                return
            if info.get("max_input_channels", 0) < 1:
                return
            tried.add(index)
            out.append((index, int(info.get("default_samplerate") or 48000)))

        if self.device is not None:
            add(self.device)
        try:
            add(sd.default.device[0])
        except Exception:
            pass
        try:
            for api in sd.query_hostapis():
                add(api.get("default_input_device"))
            for i, _ in enumerate(sd.query_devices()):
                add(i)
        except Exception:
            pass
        if not out:
            out.append((None, RATE))
        return out

    def _on_audio(self, data, frames, time_info, status) -> None:
        mono = data[:, 0]
        if self.in_rate != RATE and len(mono) > 1:
            n = max(1, int(round(len(mono) * RATE / self.in_rate)))
            mono = np.interp(np.linspace(0, len(mono) - 1, n), np.arange(len(mono)), mono)
        self.chunks.put((np.clip(mono, -1, 1) * 32767).astype(np.int16))

    def _frames(self):
        """The mic sound cut into 30 ms frames at 16 kHz."""
        buf = np.zeros(0, dtype=np.int16)
        while True:
            buf = np.concatenate([buf, self.chunks.get()])
            while len(buf) >= FRAME:
                yield buf[:FRAME]
                buf = buf[FRAME:]

    def _run(self) -> None:
        before: list[np.ndarray] = []
        speech: list[np.ndarray] = []
        loud = 0
        quiet_since = None
        for frame in self._frames():
            if not self.enabled or self.paused:
                before, speech, loud, quiet_since = [], [], 0, None
                continue
            level = float(np.sqrt(np.mean(frame.astype(np.float32) ** 2)))
            threshold = max(self.floor * 3.0, 500.0)
            if not speech:
                before = (before + [frame])[-PRE_ROLL:]
                loud = loud + 1 if level > threshold else 0
                if level <= threshold:
                    self.floor = self.floor * 0.98 + level * 0.02
                if loud >= START_FRAMES:
                    speech, quiet_since = list(before), None
                continue
            speech.append(frame)
            # counted in sound frames, not clock time, so a slow moment can't cut you off
            quiet_since = 0 if level > threshold * 0.6 else (quiet_since or 0) + 1
            seconds = len(speech) * FRAME / RATE
            if quiet_since * FRAME / RATE > END_SILENCE or seconds > MAX_SECONDS:
                audio = np.concatenate(speech)
                before, speech, loud, quiet_since = [], [], 0, None
                if seconds >= MIN_SECONDS:
                    self.on_speech(to_wav(audio))


def to_wav(samples: np.ndarray) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(samples.astype(np.int16).tobytes())
    return buf.getvalue()


# ---------------- her voice ----------------

winmm = ctypes.windll.winmm if os.name == "nt" else None


def _mci(command: str) -> None:
    if winmm is None:
        return
    err = winmm.mciSendStringW(command, None, 0, None)
    if err:
        raise RuntimeError(f"Windows couldn't play the sound (MCI error {err})")


def place_voice(ear: str) -> tuple[float, float]:
    """Left and right loudness for her voice, from the mod's 'ear' line: her position, Geralt's position
    and the camera direction (tenths of a metre). Closer is louder; to the right of the camera is right."""
    try:
        hx, hy, hz, lx, ly, lz, dx, dy, _ = (float(v) / 10 for v in ear.split())
    except ValueError:
        return 0.8, 0.8
    vx, vy = hx - lx, hy - ly
    dist = math.hypot(vx, vy, hz - lz)
    flat = math.hypot(vx, vy) or 1.0
    norm = math.hypot(dx, dy) or 1.0
    fx, fy = dx / norm, dy / norm  # where the camera looks, on the ground
    right = (vx * fy - vy * fx) / flat  # -1 fully left .. 1 fully right
    front = (vx * fx + vy * fy) / flat  # -1 behind .. 1 ahead
    loud = 1.0 if dist <= 2.5 else max(0.12, 2.5 / dist)
    if front < 0:
        loud *= 1 + 0.25 * front  # a little softer from behind
    angle = (max(-1.0, min(1.0, right * 0.85)) + 1) * math.pi / 4
    return loud * math.cos(angle) * 1.2, loud * math.sin(angle) * 1.2


# Short sounds she makes while thinking (neutral, so they fit whatever you said).
REACTIONS = ("Hm.", "Mm.", "Hmm...", "Ah.", "Mm-hm.", "Oh?", "Well...")


class Voice:
    def __init__(self, voice: str = "en-GB-SoniaNeural", rate: str = "+0%"):
        self.voice = voice
        self.rate = rate
        self.folder = tempfile.mkdtemp(prefix="charlotte-")
        self.count = 0
        self.reactions: list[str] = []
        self.reacting: threading.Thread | None = None

    def _synth(self, text: str, path: str) -> None:
        import edge_tts  # only needed here

        asyncio.run(edge_tts.Communicate(text, self.voice, rate=self.rate).save(path))

    def _play(self, path: str, where) -> None:
        try:
            self._play_placed(path, where or (lambda: (0.8, 0.8)))
        except ImportError:
            self._play_plain(path)

    def prepare_reactions(self) -> None:
        """Records her short thinking sounds once, in the background, so they can play instantly."""
        def run():
            for i, text in enumerate(REACTIONS):
                path = os.path.join(self.folder, f"react{i}.mp3")
                try:
                    self._synth(text, path)
                    self.reactions.append(path)
                except Exception:
                    pass
        threading.Thread(target=run, daemon=True).start()

    def react(self, where=None) -> None:
        """A short "Hm." the moment you stop talking, while the real answer is on its way."""
        if not self.reactions or (self.reacting and self.reacting.is_alive()):
            return
        path = random.choice(self.reactions)
        self.reacting = threading.Thread(target=self._play, args=(path, where), daemon=True)
        self.reacting.start()

    def speaker(self, where=None) -> "Speaker":
        return Speaker(self, where)

    def say(self, text: str, where=None, on_start=None) -> None:
        """Speaks `text` and returns when she's done. `where()` gives (left, right) loudness and is asked
        again while she talks, so her voice follows her as she and the camera move."""
        if not text.strip():
            return
        self.count += 1
        path = os.path.join(self.folder, f"line{self.count % 5}.mp3")
        self._synth(text, path)
        if on_start:
            on_start()
        if self.reacting:
            self.reacting.join(timeout=3)  # let the "Hm." finish first
        self._play(path, where)

    def _play_placed(self, path: str, where) -> None:
        import miniaudio

        rate = 24000
        sound = miniaudio.decode_file(path, output_format=miniaudio.SampleFormat.FLOAT32, nchannels=1,
                                      sample_rate=rate)
        samples = np.frombuffer(sound.samples, dtype=np.float32)
        pos, gains, target, asked = 0, np.array(where(), dtype=np.float32), None, 0.0
        done = threading.Event()

        def callback(out, frames, time_info, status):
            nonlocal pos, gains
            chunk = samples[pos:pos + frames]
            pos += frames
            goal = target if target is not None else gains
            ramp = np.linspace(0, 1, len(chunk), dtype=np.float32)[:, None] if len(chunk) else 0
            mix = gains + (goal - gains) * ramp  # glide to the new place, no clicks
            out[:] = 0
            out[:len(chunk)] = chunk[:, None] * mix
            gains = goal
            if pos >= len(samples):
                raise sd.CallbackStop

        with sd.OutputStream(samplerate=rate, channels=2, dtype="float32", callback=callback,
                             finished_callback=done.set):
            while not done.wait(0.1):
                if time.monotonic() - asked > 0.2:
                    asked = time.monotonic()
                    try:
                        target = np.array(where(), dtype=np.float32)
                    except Exception:
                        pass

    def _play_plain(self, path: str) -> None:
        alias = f"chl{self.count}"
        _mci(f'open "{path}" type mpegvideo alias {alias}')
        try:
            _mci(f"play {alias} wait")
        finally:
            _mci(f"close {alias}")


class Speaker:
    """Speaks sentences in order as they're added: the next one is voiced while the current one plays."""

    def __init__(self, voice: Voice, where=None):
        self.voice = voice
        self.where = where
        self.lines: queue.Queue = queue.Queue()
        self.stopped = False
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def add(self, text: str) -> None:
        if not text.strip():
            return
        self.voice.count += 1
        path = os.path.join(self.voice.folder, f"line{self.voice.count % 8}.mp3")
        ready = threading.Event()
        failed: list[Exception] = []

        def synth():
            try:
                self.voice._synth(text, path)
            except Exception as e:  # no voice for this line; the rest still plays
                failed.append(e)
            ready.set()

        threading.Thread(target=synth, daemon=True).start()
        self.lines.put((path, ready, failed))

    def _run(self) -> None:
        while True:
            item = self.lines.get()
            if item is None or self.stopped:
                return
            path, ready, failed = item
            ready.wait(30)
            if failed or self.stopped:
                continue
            if self.voice.reacting:
                self.voice.reacting.join(timeout=3)  # let the "Hm." finish first
            self.voice._play(path, self.where)

    def finish(self) -> None:
        """Waits until everything added has been spoken."""
        self.lines.put(None)
        self.thread.join()

    def stop(self) -> None:
        self.stopped = True
        self.lines.put(None)

"""The mute button: a controller combo or a keyboard key, read straight from Windows (XInput)."""

from __future__ import annotations

import ctypes
import os

# XInput button bits
BUTTONS = {
    "DPAD_UP": 0x0001, "DPAD_DOWN": 0x0002, "DPAD_LEFT": 0x0004, "DPAD_RIGHT": 0x0008,
    "START": 0x0010, "BACK": 0x0020, "LEFT_STICK": 0x0040, "RIGHT_STICK": 0x0080,
    "LB": 0x0100, "RB": 0x0200, "A": 0x1000, "B": 0x2000, "X": 0x4000, "Y": 0x8000,
}
KEYS = {f"F{i}": 0x6F + i for i in range(1, 13)}


class _Gamepad(ctypes.Structure):
    _fields_ = [("buttons", ctypes.c_ushort), ("lt", ctypes.c_ubyte), ("rt", ctypes.c_ubyte),
                ("lx", ctypes.c_short), ("ly", ctypes.c_short), ("rx", ctypes.c_short), ("ry", ctypes.c_short)]


class _State(ctypes.Structure):
    _fields_ = [("packet", ctypes.c_uint), ("pad", _Gamepad)]


def _xinput():
    if os.name != "nt":
        return None
    for name in ("xinput1_4", "xinput1_3", "xinput9_1_0"):
        try:
            return getattr(ctypes.windll, name)
        except OSError:
            continue
    return None


class MuteButton:
    """pressed() is True once per press of the combo or the key."""

    def __init__(self, combo: str = "LEFT_STICK+RIGHT_STICK", key: str = "F9"):
        self.mask = 0
        for part in combo.split("+"):
            self.mask |= BUTTONS.get(part.strip().upper(), 0)
        self.key = KEYS.get(key.strip().upper())
        self.xinput = _xinput()
        self.user32 = ctypes.windll.user32 if os.name == "nt" else None
        self.was_down = False

    def buttons(self) -> int:
        """The buttons held on any connected controller (for the button test)."""
        held = 0
        if self.xinput is not None:
            state = _State()
            for i in range(4):
                if self.xinput.XInputGetState(i, ctypes.byref(state)) == 0:
                    held |= state.pad.buttons
        return held

    def pressed(self) -> bool:
        down = bool(self.mask) and (self.buttons() & self.mask) == self.mask
        if self.key and self.user32 is not None:
            down = down or bool(self.user32.GetAsyncKeyState(self.key) & 0x8000)
        fired = down and not self.was_down
        self.was_down = down
        return fired


def names(bits: int) -> list[str]:
    return [n for n, b in BUTTONS.items() if bits & b]

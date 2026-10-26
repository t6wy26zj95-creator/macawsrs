"""Talks to The Witcher 3 over its script debug channel.

The game opens it when started with -net -debugscripts: TCP port 37001 on this PC (37000 when
it runs inside REDkit). We send "exec" packets that run the mod's chl_* functions; each one
answers with Log() lines tagged "[MCW#<seq>]" and ends with "[MCW#<seq>] end".

Wire format (all big-endian), as documented by rusty_witcher_debugger and checked on 5.0:
  packet := DE AD | u16 total length (head to tail) | items | BE EF
  item   := 2-byte tag + value: 81 08 i8, 81 16 i16, 81 32 i32, 71 32 u32, 81 64 i64,
            AC 08 utf-8 string (81 16 + i16 byte length + bytes), 9C 16 utf-16 string
  BIND    = [utf8 "BIND"][utf8 namespace]   (no reply)
  exec    = [utf8 "Remote"][i32 0x12345678][i32 0x81160008][utf8 command]
  reply   = [i32 0x12345678][i32 0x81160008][utf8 text], text = the Log() lines joined by newlines
"""

from __future__ import annotations

import itertools
import re
import socket
import struct
import threading
import time

PORTS = (37001, 37000)
HEAD, TAIL = b"\xde\xad", b"\xbe\xef"
MAX_COMMAND = 2047  # longer commands are silently dropped by the game
MARK = re.compile(r"\[MCW#(\d+)\] ?(.*)")


class GameNotRunning(ConnectionError):
    pass


def _utf8(s: str) -> bytes:
    data = s.encode("utf-8")
    return b"\xac\x08\x81\x16" + struct.pack(">h", len(data)) + data


def _i32(v: int) -> bytes:
    return b"\x81\x32" + struct.pack(">I", v & 0xFFFFFFFF)


def packet(*items: bytes) -> bytes:
    body = b"".join(items)
    return HEAD + struct.pack(">H", len(body) + 6) + body + TAIL


def exec_packet(command: str) -> bytes:
    return packet(_utf8("Remote"), _i32(0x12345678), _i32(0x81160008), _utf8(command))


def strings_in(body: bytes) -> list[str]:
    """The text values in a packet body (other items are skipped)."""
    out, i = [], 0
    sizes = {b"\x81\x08": 1, b"\x81\x16": 2, b"\x81\x32": 4, b"\x71\x32": 4, b"\x81\x64": 8}
    while i + 2 <= len(body):
        tag = body[i:i + 2]
        i += 2
        if tag in sizes:
            i += sizes[tag]
        elif tag in (b"\xac\x08", b"\x9c\x16"):
            (n,) = struct.unpack_from(">h", body, i + 2)
            i += 4
            if tag == b"\xac\x08":
                out.append(body[i:i + n].decode("utf-8", "replace"))
                i += n
            else:
                out.append(body[i:i + 2 * n].decode("utf-16-be", "replace"))
                i += 2 * n
        else:
            break  # unknown tag: stop reading this packet
    return out


def quote(value: object) -> str:
    """One exec argument. Strings go in double quotes; quotes and line breaks inside are dropped."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    text = str(value).replace('"', "'").replace("\\", "/")
    return '"' + " ".join(text.split()) + '"'


class GameLink:
    def __init__(self, host: str = "127.0.0.1"):
        self.host = host
        self.sock: socket.socket | None = None
        self.buf = b""
        self.lock = threading.Lock()
        self.seq = itertools.count(int(time.time()) % 100000 * 10)
        self.pending: dict[int, list[str]] = {}

    def connect(self) -> None:
        for port in PORTS:
            try:
                s = socket.create_connection((self.host, port), timeout=2)
            except OSError:
                continue
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            for ns in ("Remote", "scripts"):
                s.sendall(packet(_utf8("BIND"), _utf8(ns)))
            self.sock, self.buf = s, b""
            return
        raise GameNotRunning("The game isn't running with -net -debugscripts.")

    def close(self) -> None:
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None

    def call(self, func: str, *args: object, timeout: float = 3.0) -> list[str]:
        """Runs chl_<func>(seq, args...) in the game and returns its output lines (without the tags)."""
        with self.lock:
            if self.sock is None:
                self.connect()
            seq = next(self.seq)
            command = f"{func}({', '.join(quote(a) for a in (seq, *args))})"
            data = command.encode("utf-8")
            if len(data) > MAX_COMMAND:
                raise ValueError("command too long")
            try:
                self.sock.sendall(exec_packet(command))
                return self._collect(seq, timeout)
            except OSError as e:
                self.close()
                raise GameNotRunning(str(e)) from e

    def _collect(self, seq: int, timeout: float) -> list[str]:
        lines: list[str] = []
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            for text in self._read(end - time.monotonic()):
                for line in text.split("\n"):
                    m = MARK.search(line)
                    if not m or int(m.group(1)) != seq:
                        continue  # a late answer to an earlier call, or a status line
                    if m.group(2).strip() == "end":
                        return lines
                    lines.append(m.group(2).rstrip("\r"))
        raise TimeoutError(f"no answer from the game for seq {seq}")

    def _read(self, wait: float) -> list[str]:
        """Texts from the packets that arrive within `wait` seconds."""
        self.sock.settimeout(max(0.05, wait))
        try:
            chunk = self.sock.recv(65536)
        except socket.timeout:
            return []
        if not chunk:
            raise OSError("the game closed the connection")
        self.buf += chunk
        texts = []
        while True:
            start = self.buf.find(HEAD)
            if start < 0 or len(self.buf) < start + 4:
                self.buf = self.buf[start:] if start >= 0 else b""
                break
            (size,) = struct.unpack_from(">H", self.buf, start + 2)
            if size < 6:
                self.buf = self.buf[start + 2:]
                continue
            if len(self.buf) < start + size:
                self.buf = self.buf[start:]
                break
            pkt = self.buf[start:start + size]
            self.buf = self.buf[start + size:]
            if pkt[-2:] == TAIL:
                texts.extend(strings_in(pkt[4:-2]))
        return texts


def parse_fields(lines: list[str]) -> dict[str, str]:
    """'st calmfor 37 danger 0 ...' -> {'calmfor': '37', 'danger': '0', ...}; 'area X' -> {'area': 'X'}."""
    out: dict[str, str] = {}
    for line in lines:
        word, _, rest = line.partition(" ")
        if word == "st":
            parts = rest.split()
            out.update(zip(parts[0::2], parts[1::2]))
        elif word:
            out[word] = rest.strip()
    return out

from __future__ import annotations

import socket
import struct
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "witcher" / "helper"))

import w3link  # noqa: E402


def test_packets_match_the_documented_bytes():
    bind = w3link.packet(w3link._utf8("BIND"), w3link._utf8("Remote"))
    assert bind.hex() == "dead001cac0881160004" + b"BIND".hex() + "ac0881160006" + b"Remote".hex() + "beef"
    cmd = "chl_ping(17)"
    pkt = w3link.exec_packet(cmd)
    assert len(pkt) == 36 + len(cmd)
    assert pkt[4:].startswith(bytes.fromhex("ac0881160006") + b"Remote" + bytes.fromhex("8132123456788132811600 08".replace(" ", "")))


def test_quote():
    assert w3link.quote(5) == "5"
    assert w3link.quote('say "hi"\nnow') == "\"say 'hi' now\""


def test_parse_fields():
    f = w3link.parse_fields(["st calmfor 37 danger 0 settle 1", "busy ", "area AN_Velen", "quest Family Matters"])
    assert f == {"calmfor": "37", "danger": "0", "settle": "1", "busy": "", "area": "AN_Velen", "quest": "Family Matters"}


def _reply(text: str) -> bytes:
    return w3link.packet(w3link._i32(0x12345678), w3link._i32(0x81160008), w3link._utf8(text))


def test_call_against_a_fake_game():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    got = []

    def game():
        conn, _ = srv.accept()
        data = b""
        while b"chl_poll" not in data:
            data += conn.recv(4096)
        seq = int(data.split(b"chl_poll(")[1].split(b",")[0])
        got.append(seq)
        # a stale line from an earlier call, then the answer split over two packets, sent in pieces
        out = _reply("[MCW#1] old") + _reply(f"[MCW#{seq}] st calmfor 3\n[MCW#{seq}] area AN_Velen") + _reply(f"[MCW#{seq}] end")
        conn.sendall(out[:7])
        conn.sendall(out[7:])
        conn.recv(10)

    t = threading.Thread(target=game, daemon=True)
    t.start()
    link = w3link.GameLink()
    w3link.PORTS = (port,)
    try:
        lines = link.call("chl_poll", 0)
    finally:
        link.close()
        srv.close()
    assert lines == ["st calmfor 3", "area AN_Velen"]
    assert got

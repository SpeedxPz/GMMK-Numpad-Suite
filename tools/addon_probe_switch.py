"""Diagnose live LEDs vs a profile switch made on the numpad (add-on v3, peek 0x63).
Pushes the profile-1 status lights like the suite, re-pushes on every 04 F1 (now, +0.6 s, +3 s),
and logs the per-key gate 0x200002A9, the slot bytes 0x20000212/13 and the per-key table entries
that are in use whenever they change. Run for ~45 s while switching Fn+9 / Fn+7 on the numpad."""
import struct
import sys
import threading
import time

from gmmk_numpad import EventReader, Numpad

LIVE = {3: (16, 0, 30), 4: (30, 0, 0), 25: (255, 0, 0), 26: (24, 24, 24), 27: (24, 24, 24)}
lock = threading.Lock()
pad = Numpad(1, 1)
t0 = time.time()
pending = []  # times to push


def ts():
    return f"{time.time() - t0:6.2f}"


def peek(addr, n):
    with lock:
        pad._last_slot = None
        pad._send(0x63, struct.pack("<IB", addr, n), delay=0.03)
        r = pad.read()
    assert r[1] == 0x63 and r[6] == n, r[:8].hex(" ")
    return r[7:7 + n]


def push():
    with lock:
        pad.profile, pad.layer, pad._last_slot = 1, 1, None
        pad.set_live_leds(LIVE)
    print(ts(), "push live", flush=True)


def events():
    r = EventReader()
    while time.time() - t0 < float(sys.argv[1] if len(sys.argv) > 1 else 45):
        e = r.read(100)
        if e and e[0] == "profile":
            print(ts(), "F1", e[1], flush=True)
            if e[1] == (1, 1):
                now = time.time()
                pending.extend([now, now + 0.6, now + 3.0])


threading.Thread(target=events, daemon=True).start()
push()
last = None
while time.time() - t0 < float(sys.argv[1] if len(sys.argv) > 1 else 45):
    for due in [d for d in pending if d <= time.time()]:
        pending.remove(due)
        push()
    gate = peek(0x200002A9, 1)[0]
    slot = tuple(peek(0x20000212, 2))
    table = peek(0x20001A74, 210)
    used = [(table[i * 5], table[i * 5 + 1], tuple(table[i * 5 + 2:i * 5 + 5])) for i in range(42)
            if table[i * 5 + 1]]
    state = (gate, slot, used)
    if state != last:
        print(ts(), f"gate {gate} slot {slot} entries in use {len(used)}:",
              [(led, hex(vis), rgb) for led, vis, rgb in used], flush=True)
        last = state
    time.sleep(0.15)
pad.close()

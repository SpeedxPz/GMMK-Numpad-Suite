"""Add-on live LEDs vs many profile/layer switches (cmd 0x01 reloads the per-key table like Fn+9/7).
Each round: switch to a slot, push a status-light set that differs per round, then peek the per-key
table (0x63, add-on v3+) and count LEDs that did not land. Ends back on 1/1. No user needed."""
import random
import struct
import time

from gmmk_numpad import ALL_LEDS, Numpad

pad = Numpad(1, 1)


def peek(addr, n):
    pad._last_slot = None
    pad._send(0x63, struct.pack("<IB", addr, n), delay=0.03)
    r = pad.read()
    assert r[1] == 0x63 and r[6] == n, r[:8].hex(" ")
    return r[7:7 + n]


def shown():
    t = peek(0x20001A74, 210)
    return {t[i * 5]: tuple(t[i * 5 + 2:i * 5 + 5]) for i in range(42) if t[i * 5 + 1]}


random.seed(1)
slots = [(1, 1), (2, 1), (1, 2), (3, 1), (1, 3)]
missing_rounds = 0
for rnd in range(30):
    p, l = slots[rnd % len(slots)]
    pad.profile, pad.layer, pad._last_slot = p, l, None
    pad.apply_profile()
    time.sleep(0.4)
    want = {led: (rnd, 100 + rnd, 7) for led in random.sample(ALL_LEDS, 6)}
    pad.profile, pad.layer, pad._last_slot = p, l, None
    pad.set_live_leds(want)
    time.sleep(0.1)
    got = shown()
    miss = [led for led, c in want.items() if got.get(led) != c]
    missing_rounds += bool(miss)
    print(f"round {rnd:2} slot {p}/{l}: {len(miss)} of 6 missing {miss}")
pad.profile, pad.layer, pad._last_slot = 1, 1, None
pad.apply_profile()
pad.close()
print("rounds with missing LEDs:", missing_rounds, "of 30")

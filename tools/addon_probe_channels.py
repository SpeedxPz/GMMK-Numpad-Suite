"""Sample what the side LEDs actually output (add-on v3+ peek, USB): the channel buffer 0x20001466
(216 bytes, written by the per-key renderer and the effects) many times a second, plus the per-key
table entries for the side LEDs. Shows whether something overwrites the per-key colours."""
import collections
import os
import struct
import sys
import time

from gmmk_numpad import SIDE_LEFT, SIDE_RIGHT, Numpad

HERE = os.path.dirname(os.path.abspath(__file__))
flash = open(os.path.join(HERE, "..", "_re", "backup", "flash_full_20261101.bin"), "rb").read()
B = 0x08000000
R, G, Bl = (flash[a - B:a - B + 42] for a in (0x08015E6A, 0x08015E94, 0x08015EBE))  # LED -> channel

pad = Numpad()


def peek(addr, n):
    pad._last_slot = None
    pad._send(0x63, struct.pack("<IB", addr, n), delay=0.005)
    r = pad.read()
    return r[7:7 + n] if r[1] == 0x63 and r[6] == n else None


table = peek(0x20001A74, 210)
entries = {table[i * 5]: (hex(table[i * 5 + 1]), tuple(table[i * 5 + 2:i * 5 + 5])) for i in range(42) if table[i * 5 + 1]}
print("per-key entries on side LEDs:", {i: entries[i] for i in SIDE_LEFT + SIDE_RIGHT if i in entries})
seen = collections.defaultdict(collections.Counter)
t0, n = time.time(), 0
while time.time() - t0 < float(sys.argv[1] if len(sys.argv) > 1 else 4):
    ch = peek(0x20001466, 216)
    if ch is None:
        continue
    n += 1
    for i in SIDE_LEFT + SIDE_RIGHT:
        seen[i][(ch[R[i]], ch[G[i]], ch[Bl[i]])] += 1
print(f"{n} samples")
for i in SIDE_LEFT + SIDE_RIGHT:
    top = seen[i].most_common(4)
    print(f"LED {i:2}: {len(seen[i])} distinct outputs, top {top}")
pad.close()

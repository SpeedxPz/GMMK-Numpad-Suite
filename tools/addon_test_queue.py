"""Add-on v2 hardware test 2: the command queue (see .knowledge/firmware-update.md, test plan).

Old race (stock firmware switched to 1/2): 0x01 apply for 1/1, then 0x03 for 1/2 50 ms later.
With the queue: no 04 F1 01 02, still on 1/1, and the 1/2 key table holds what was written.
Then a burst of 10 frames with no delay: info must show dropped 0, max depth <= 8.
Restores the 1/2 key table at the end."""
import threading
import time

from gmmk_numpad import EventReader, Numpad

events, stop = [], threading.Event()


def watch():
    r = EventReader()
    while not stop.is_set():
        e = r.read(100)
        if e and e[0] == "profile":
            events.append((round(time.time() - t0, 3), e[1]))
    r.close()


def raw(pad, cmd, p, l, payload=b""):
    pad.profile, pad.layer, pad._last_slot = p, l, None  # bypass the suite's SLOT_CHANGE_GAP on purpose
    pad._send(cmd, payload, delay=0)


def table(pad, p, l):
    pad.profile, pad.layer, pad._last_slot = p, l, None
    pad._request(0x83)
    r = pad.read()
    assert r[1] == 0x03 and (r[2], r[3]) == (p, l), r[:8].hex(" ")
    return bytearray(r)


def addon_info(pad):
    pad.profile, pad.layer, pad._last_slot = 1, 1, None
    r = pad._request(0x60)
    return {"ver": r[4], "features": hex(r[5]), "boots": r[6], "dropped": r[7], "maxdepth": r[8]}


pad = Numpad()
t0 = time.time()
th = threading.Thread(target=watch, daemon=True)
th.start()
time.sleep(0.5)
print("before:", addon_info(pad))
orig = table(pad, 1, 2)
test = bytearray(orig)
test[28 + 16] = 0x27 if orig[28 + 16] != 0x27 else 0x1E  # slot 16 = key 0: change its code
time.sleep(0.5)
events.clear()
raw(pad, 0x01, 1, 1)
time.sleep(0.05)
raw(pad, 0x03, 1, 2, bytes(test[4:]))
time.sleep(1.5)
got = table(pad, 1, 2)
print("race: F1 events", events, "| 1/2 key-0 code wrote", hex(test[44]), "read", hex(got[44]),
      "| tables equal:", got[8:48] == test[8:48])
time.sleep(0.5)
events.clear()
t1 = table(pad, 1, 1)
for _ in range(10):  # burst: rewrite 1/1's own table 10x, no delay
    raw(pad, 0x03, 1, 1, bytes(t1[4:]))
time.sleep(1.5)
print("burst: F1 events", events, "| after:", addon_info(pad))
raw(pad, 0x03, 1, 2, bytes(orig[4:]))  # restore 1/2
time.sleep(0.5)
print("restored 1/2:", table(pad, 1, 2)[8:48] == orig[8:48])
stop.set()
pad.close()

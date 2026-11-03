"""Can a non-active profile/layer be written without cmd 0x01 (which activates it)?

Writes a recognisable key table (cmd 0x03) to profile 3 / layer 2 *without* the apply command, then
reads it back (0x83, per requested profile/layer) and listens for 04 F1 (an activation would echo).
Finally restores that slot's table to the factory default, also without apply.
"""
import threading
import time

from gmmk_numpad import DEFAULT_KEYS, EventReader, Numpad

events = []
stop = threading.Event()


def listen():
    r = EventReader()
    while not stop.is_set():
        d = r.dev.read(64, 200)
        if d and d[1] == 0xF1:
            events.append(bytes(d[:6]).hex(" "))
    r.close()


th = threading.Thread(target=listen)
th.start()
time.sleep(0.3)

pad = Numpad(profile=3, layer=2)
before = pad.read_key_table()
print("p3/l2 before:", before["profile"], before["layer"], before["types"][:8], before["codes"][:8])
table = bytearray(DEFAULT_KEYS)
table[20 + 4] = 0x68  # key 7 -> F13 (marker)
table[20 + 5] = 0x69  # key 8 -> F14 (marker)
pad._send(0x03, bytes(4) + table, delay=0.3)  # no apply_profile()
after = pad.read_key_table()
print("p3/l2 after: ", after["profile"], after["layer"], after["types"][:8], after["codes"][:8])
active = Numpad(profile=1, layer=1).read_effect()
print("active slot still answers as profile 1:", active)
pad._send(0x03, bytes(4) + bytes(DEFAULT_KEYS), delay=0.3)  # restore factory, no apply
print("p3/l2 restored:", pad.read_key_table()["codes"][:8])
pad.close()
time.sleep(0.5)
stop.set()
th.join()
print("04 F1 events during the test (activation echoes):", events or "none")

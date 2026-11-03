"""Does cmd 0x01 switch the active profile? Reads 0x8F (stored effect of the active profile) and
listens for 04 F1 events while sending 0x01 for profiles 2, 3, then back to 1."""
import threading
import time

from gmmk_numpad import EventReader, Numpad

events = []


def listen(stop):
    r = EventReader()
    while not stop.is_set():
        d = r.dev.read(64, 200)
        if d and d[1] in (0xF1, 0xF2):
            events.append((round(time.time() - t0, 2), bytes(d[:12]).hex(" ")))
    r.close()


t0 = time.time()
stop = threading.Event()
th = threading.Thread(target=listen, args=(stop,))
th.start()
time.sleep(0.3)
for p in (1, 2, 3, 1):
    pad = Numpad(profile=p)
    pad.apply_profile()
    time.sleep(0.5)
    probe = Numpad(profile=1)  # request carries profile 1: does the reply follow the request or the active one?
    print(f"after 01 p{p}: 8F reply (requested as p1) =", probe.read_effect())
    pad.close()
    probe.close()
    time.sleep(0.5)
stop.set()
th.join()
for e in events:
    print(e)

"""Which commands activate their profile/layer? Sends each data command, without cmd 0x01, to a
hidden slot (profile 3 / layer 3) while slot 1/1 is active, and records 04 F1 events after each one.
Ends by activating 1/1 again."""
import threading
import time

from gmmk_numpad import ALL_LEDS, EventReader, Numpad

events = []
stop = threading.Event()


def listen():
    r = EventReader()
    while not stop.is_set():
        d = r.dev.read(64, 100)
        if d and d[1] == 0xF1:
            events.append((time.time(), bytes(d[:6]).hex(" ")))
    r.close()


th = threading.Thread(target=listen)
th.start()
home = Numpad(profile=1, layer=1)
home.apply_profile()
time.sleep(1.0)
pad = Numpad(profile=3, layer=3)
colors = {i: (0, 0, 40) for i in ALL_LEDS}
tests = [
    ("0x04 macro modes", lambda: pad.set_macro_modes({})),
    ("0x02 effect only", lambda: pad._send(0x02, bytes([7, 0, 0, 0, 10, 0, 0, 0, 0, 0, 0, 255, 255, 255]))),
    ("0x07 properties only", lambda: pad._send(0x07, bytes([7, 0, 1, 0, 20, 0, 8, 1, 0, 20]))),
    ("0x09 knob mode", lambda: pad.set_knob_events(False, colors)),
    ("0x06 per-key LEDs", lambda: pad._send(0x06, bytes((42, 0, 20, 0)) + pad._led_page(colors, (0, 0, 0), ()))),
]
for name, fn in tests:
    mark = time.time()
    fn()
    time.sleep(1.2)
    seen = [e for t, e in events if t >= mark]
    print(f"{name:22s} -> {seen or 'no F1 (silent)'}")
home.apply_profile()
time.sleep(0.8)
stop.set()
th.join()
print("final F1:", events[-1][1] if events else None)

"""Do the add-on's live status lights survive a key-binding save in the dashboard?
Re-binds key "0" through the running suite's API (then restores it) and peeks (0x63, add-on v3+)
the numpad's per-key table after each save. Needs the suite running with OBS status lights."""
import json
import struct
import time
import urllib.request

from gmmk_numpad import Numpad

API = "http://127.0.0.1:8765"


def post(path, body):
    req = urllib.request.Request(API + path, json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "Origin": API})
    return json.load(urllib.request.urlopen(req))


def live_entries():
    pad = Numpad()
    pad._send(0x63, struct.pack("<IB", 0x20001A74, 210), delay=0.05)
    t = pad.read()[7:7 + 210]
    pad.close()
    return {t[i * 5]: tuple(t[i * 5 + 2:i * 5 + 5]) for i in range(42) if t[i * 5 + 1]}


state = json.load(urllib.request.urlopen(API + "/api/state"))
orig = state["layer"]["keymap"].get("0")
print("before:", live_entries())
post("/api/keymap", {"key": "0", "binding": {"type": "key", "code": 0x27}})
time.sleep(3)
print("after a save:", live_entries())
post("/api/keymap", {"key": "0", "binding": orig})
time.sleep(3)
print("after restore:", live_entries())

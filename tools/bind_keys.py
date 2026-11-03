"""Bind the numpad's top row to OBS actions (stored on the device).

Default (event mode): - / * send no keystroke, only a vendor-interface event that
obs_bridge.py turns into OBS WebSocket calls. Works whatever window is focused, including
elevated apps and games, and needs no OBS hotkeys.

  -  -> Toggle recording      /  -> Toggle streaming      *  -> Toggle record pause

  python bind_keys.py           event mode (use with obs_bridge.py)
  python bind_keys.py --fkeys   F13 / F14 / F15 keystrokes instead, for OBS Settings -> Hotkeys
  python bind_keys.py --reset   factory layout
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gmmk_numpad import BIND_HOST_EVENT, BIND_KEYSTROKE, HID_F13, Numpad  # noqa: E402

KEYS = ["Subtract", "Divide", "Multiply"]  # NumLock doubles as the firmware Fn key, so taps are unreliable

if "--reset" in sys.argv:
    bindings = {}
elif "--fkeys" in sys.argv:
    bindings = {k: (BIND_KEYSTROKE, HID_F13 + i) for i, k in enumerate(KEYS)}
else:
    bindings = {k: (BIND_HOST_EVENT, 0) for k in KEYS}

pad = Numpad()
pad.set_key_bindings(bindings)
pad.close()
print("bound:", bindings or "factory layout")

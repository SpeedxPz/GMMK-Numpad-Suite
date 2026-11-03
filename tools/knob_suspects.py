"""Replay one earlier write at a time to find which one disables knob turning.

  python knob_suspects.py page0   single-page per-key LED write (what obs_bridge.py sends)
  python knob_suspects.py pages3  the original 3-page per-key LED write (test_leds.py, first version)
"""
import sys
sys.path.insert(0, r"D:\User Storage\Documents\2.project\Glorious GMMK\tools")
from gmmk_numpad import KEY_LED, LED_COUNT, SIDE_LEDS, Numpad

colors = {i: (0, 0, 255) for i in SIDE_LEDS}
colors.update({i: (255, 0, 0) for i in KEY_LED.values()})
pad = Numpad()
if sys.argv[1] == "page0":
    pad.set_leds(colors, default=(255, 255, 255))
elif sys.argv[1] == "pages3":
    data = bytearray()
    for i in range(LED_COUNT):
        r, g, b = colors.get(i, (0, 0, 0))
        data += bytes((0xFF, i, r, g, b))
    data += bytes(600 - len(data))
    for page in range(3):
        pad._send(0x06, bytes((42, page, 20, 0)) + data[page * 200:(page + 1) * 200])
    pad.apply_profile()
pad.close()
print("sent", sys.argv[1])

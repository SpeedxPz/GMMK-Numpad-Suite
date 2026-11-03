"""Bisect which byte of the 0x07 properties command disables knob turning.
Usage: python props_test.py <name>=<value> ...   (payload byte index = report byte - 4)
Starts from CORE's numpad layout: [4]=7 effect, [5]=0, [6]=1, [7]=0 sides on, [8]=20, [10]=8, [13]=20.
"""
import sys
sys.path.insert(0, r"D:\User Storage\Documents\2.project\Glorious GMMK\tools")
from gmmk_numpad import Numpad
from obs_bridge import BASE, render

report = {4: 7, 5: 0, 6: 1, 7: 0, 8: 20, 10: 8, 13: 20}
for arg in sys.argv[1:]:
    k, v = arg.split("=")
    report[int(k)] = int(v, 0)
payload = bytearray(12)
for idx, val in report.items():
    payload[idx - 4] = val
p = Numpad()
p._send(0x07, payload)
p.apply_profile()
c, b = render((False, False, False))
p.set_leds(c, default=BASE, breathing=b)
p.close()
print("sent 07:", {k: report[k] for k in sorted(report)})

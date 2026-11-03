"""Switch the numpad knob between OBS mode (turns go to obs_bridge.py) and Windows volume.

The mode is saved on the numpad, so in OBS mode the knob does nothing while the bridge is not
running. obs_bridge.py re-applies the mode from KNOB_SOURCE every time it connects.

  python knob_mode.py --volume   knob = Windows volume again
  python knob_mode.py --obs      knob = OBS (via obs_bridge.py)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gmmk_numpad import Numpad  # noqa: E402
from obs_bridge import BASE, render  # noqa: E402

if "--obs" not in sys.argv and "--volume" not in sys.argv:
    sys.exit(__doc__)
colors, breathing = render((False, False, False))
pad = Numpad()
pad.set_knob_events("--obs" in sys.argv, colors, default=BASE, breathing=breathing)
pad.close()
print("knob ->", "OBS" if "--obs" in sys.argv else "Windows volume")

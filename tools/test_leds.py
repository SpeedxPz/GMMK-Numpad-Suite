import sys
sys.path.insert(0, r"D:\User Storage\Documents\2.project\Glorious GMMK\tools")
from gmmk_numpad import Numpad, KEY_LED, SIDE_LEDS
n = Numpad()
colors = {i: (0, 0, 255) for i in SIDE_LEDS}          # side lights blue
colors.update({i: (255, 0, 0) for i in KEY_LED.values()})  # keys red
colors[KEY_LED["Enter"]] = (0, 255, 0)                 # Enter green
n.set_leds(colors)
print("readback:", n.read()[:12].hex(" "))
n.close()

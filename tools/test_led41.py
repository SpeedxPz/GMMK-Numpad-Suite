import sys, time
sys.path.insert(0, r"D:\User Storage\Documents\2.project\Glorious GMMK\tools")
from gmmk_numpad import Numpad, KEY_LED, SIDE_LEDS, LED_COUNT
n = Numpad()
colors = {i: (0, 0, 255) for i in SIDE_LEDS}
colors.update({i: (255, 0, 0) for i in KEY_LED.values()})
data = bytearray()
for i in range(LED_COUNT):
    r, g, b = colors.get(i, (0, 0, 0))
    data += bytes((0xFF, i, r, g, b))
# slot 13 is an unused grid position in page 0: put LED 41's entry there (yellow so it's obvious)
data[13*5:13*5+5] = bytes((0xFF, 41, 255, 255, 0))
data += bytes(600 - len(data))
for page in range(3):
    n._send(0x06, bytes((42, page, 20, 0)) + data[page*200:(page+1)*200])
n.apply_profile()
n.close()

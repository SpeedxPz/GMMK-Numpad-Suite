"""Bluetooth (BLE 093A:2860) transport test.

Firmware (FUN_08011ae4): the BT module forwards host writes to the MCU as "+Data:<seq><19 bytes>".
seq 0 clears the buffer, seq k (1..13) stores 19 bytes at offset 1 + (k-1)*19, seq 0xFF hands the
assembled frame to the same command parser as USB (buf[1] = command). So a BT command is the USB
frame without its report ID, chunked into 21-byte output reports (ID 7) on usage page FF02.

  python bt_test.py leds     every LED magenta (cmd 06)
  python bt_test.py events   print FF01 input reports (ID 4) for 60 s
"""
import sys
import time

import hid

sys.path.insert(0, r"D:\User Storage\Documents\2.project\Glorious GMMK\tools")
from gmmk_numpad import ALL_LEDS  # noqa: E402

BT_VID, BT_PID = 0x093A, 0x2860


def open_usage(page):
    paths = [d["path"] for d in hid.enumerate(BT_VID, BT_PID) if d["usage_page"] == page]
    if not paths:
        sys.exit(f"BT collection {page:04x} not found (numpad asleep or not on Bluetooth?)")
    h = hid.device()
    h.open_path(paths[0])
    return h


def send_frame(h, frame):
    """frame = USB command bytes without the report ID: [cmd, profile, layer, ...]."""
    h.write(bytes([7, 0]) + bytes(19))
    for k in range(0, (len(frame) + 18) // 19):
        h.write(bytes([7, k + 1]) + frame[k * 19:(k + 1) * 19].ljust(19, b"\0"))
        time.sleep(0.01)
    h.write(bytes([7, 0xFF]) + bytes(19))


if sys.argv[1] == "leds":
    data = bytearray()
    for i in ALL_LEDS:
        data += bytes((0xFF, i, 255, 0, 255))
    frame = bytes([0x06, 1, 1, 42, 0, 20, 0]) + bytes(data)
    h = open_usage(0xFF02)
    send_frame(h, frame)
    time.sleep(0.2)
    send_frame(h, bytes([0x01, 1, 1]))  # apply profile
    h.close()
    print("sent", len(frame), "byte frame in", (len(frame) + 18) // 19, "chunks")
elif sys.argv[1] == "events":
    h = open_usage(0xFF01)
    end = time.time() + 60
    while time.time() < end:
        r = h.read(64, 500)
        if r and not (r[1:3] == [0xF8, 0x32]):
            print(time.strftime("%H:%M:%S"), " ".join(f"{b:02x}" for b in r[:10]), flush=True)
    h.close()

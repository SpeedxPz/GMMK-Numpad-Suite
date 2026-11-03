"""Send profile-apply (cmd 01) over each BT candidate path and watch FF01 (input ID 4) for 04 F1."""
import asyncio, sys, threading, time
import hid
sys.path.insert(0, r"D:\User Storage\Documents\2.project\Glorious GMMK\tools")
import ble_cmd

log = []
def watch(stop):
    d = [x for x in hid.enumerate(0x093A, 0x2860) if x["usage_page"] == 0xFF01][0]
    h = hid.device(); h.open_path(d["path"])
    while not stop.is_set():
        r = h.read(64, 200)
        if r: log.append((time.time(), " ".join(f"{b:02x}" for b in r[:10])))
    h.close()

def hid_frame(frame, variant):
    d = [x for x in hid.enumerate(0x093A, 0x2860) if x["usage_page"] == 0xFF02][0]
    h = hid.device(); h.open_path(d["path"])
    if variant == "chunked":
        h.write(bytes([7, 0]) + bytes(19))
        h.write(bytes([7, 1]) + frame.ljust(19, b"\0"))
        h.write(bytes([7, 0xFF]) + bytes(19))
    else:  # raw: frame directly after the report ID
        h.write(bytes([7]) + frame.ljust(20, b"\0"))
    h.close()

stop = threading.Event(); t = threading.Thread(target=watch, args=(stop,)); t.start()
time.sleep(1)
frame = bytes([0x01, 1, 1])
for name, fn in [("hid chunked", lambda: hid_frame(frame, "chunked")),
                 ("hid raw", lambda: hid_frame(frame, "raw")),
                 ("gatt chunked", lambda: asyncio.run(ble_cmd.main_send(frame)))]:
    t0 = time.time(); fn(); time.sleep(2.5)
    got = [l for ts, l in log if ts >= t0]
    print(f"{name:13s} -> {got if got else 'no events'}", flush=True)
stop.set(); t.join()

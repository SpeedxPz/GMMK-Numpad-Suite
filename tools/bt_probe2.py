"""Send a frame over BT HID (FF02, ID 7, chunked) and print every FF01 input report for 3 s."""
import sys, threading, time
import hid

def watch(stop, log):
    d = [x for x in hid.enumerate(0x093A, 0x2860) if x["usage_page"] == 0xFF01][0]
    h = hid.device(); h.open_path(d["path"])
    while not stop.is_set():
        r = h.read(64, 200)
        if r: log.append(" ".join(f"{b:02x}" for b in r[:21]))
    h.close()

def send(frame):
    d = [x for x in hid.enumerate(0x093A, 0x2860) if x["usage_page"] == 0xFF02][0]
    h = hid.device(); h.open_path(d["path"])
    h.write(bytes([7, 0]) + bytes(19))
    for k in range((len(frame) + 18) // 19):
        h.write(bytes([7, k + 1]) + frame[k * 19:(k + 1) * 19].ljust(19, b"\0")); time.sleep(0.02)
    h.write(bytes([7, 0xFF]) + bytes(19))
    h.close()

frame = bytes.fromhex(sys.argv[1])
log = []; stop = threading.Event(); t = threading.Thread(target=watch, args=(stop, log)); t.start()
time.sleep(0.5); send(frame); time.sleep(3); stop.set(); t.join()
print("\n".join(log) if log else "no events")

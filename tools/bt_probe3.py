"""BT HID command with per-chunk ack flow control: send chunk, wait for 04 FC <seq>."""
import sys, threading, time
import hid

acks = []; events = []
def watch(stop):
    d = [x for x in hid.enumerate(0x093A, 0x2860) if x["usage_page"] == 0xFF01][0]
    h = hid.device(); h.open_path(d["path"])
    while not stop.is_set():
        r = h.read(64, 100)
        for o in range(0, len(r) - 2, 9):  # events are packed 9 bytes apiece
            if r[o] == 0x04 and r[o + 1] == 0xFC: acks.append((time.time(), r[o + 2]))
            elif r[o] == 0x04 and r[o + 1]: events.append(" ".join(f"{b:02x}" for b in r[o:o + 9]))
    h.close()

def wait_ack(seq, t0, timeout=1.5):
    while time.time() - t0 < timeout:
        if any(ts >= t0 and s == seq for ts, s in acks): return round(time.time() - t0, 2)
        time.sleep(0.01)
    return None

stop = threading.Event(); t = threading.Thread(target=watch, args=(stop,)); t.start(); time.sleep(0.5)
d = [x for x in hid.enumerate(0x093A, 0x2860) if x["usage_page"] == 0xFF02][0]
h = hid.device(); h.open_path(d["path"])
frame = bytes.fromhex(sys.argv[1])
seqs = [0] + list(range(1, (len(frame) + 18) // 19 + 1)) + [0xFF]
for s in seqs:
    payload = bytes(19) if s in (0, 0xFF) else frame[(s - 1) * 19:s * 19].ljust(19, b"\0")
    t0 = time.time(); h.write(bytes([7, s]) + payload)
    print(f"seq {s:3d} ack after {wait_ack(s, t0)} s", flush=True)
h.close(); time.sleep(1); stop.set(); t.join()
print("events:", events[-3:])

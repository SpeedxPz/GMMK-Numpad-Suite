"""Print vendor input reports from the numpad (slider/encoder events) for a while."""
import sys, time, hid
P = [d["path"] for d in hid.enumerate(0x320F, 0x5088) if d["usage_page"] == 0xFF00 and d["usage"] == 0xFF00][0]
h = hid.device(); h.open_path(P); h.set_nonblocking(False)
end = time.time() + float(sys.argv[1] if len(sys.argv) > 1 else 600)
n = 0
while time.time() < end and n < 400:
    r = h.read(64, 500)
    if r:
        n += 1
        print(time.strftime("%H:%M:%S"), " ".join(f"{b:02x}" for b in r[:8]), flush=True)
h.close()
print("done", flush=True)

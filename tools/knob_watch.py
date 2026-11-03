"""Print the numpad's consumer-control reports (knob volume keys) for a while."""
import sys, time, hid
P = [d["path"] for d in hid.enumerate(0x320F, 0x5088) if d["usage_page"] == 0x000C][0]
h = hid.device(); h.open_path(P)
end = time.time() + float(sys.argv[1] if len(sys.argv) > 1 else 600)
while time.time() < end:
    r = h.read(64, 500)
    if r:
        print(time.strftime("%H:%M:%S"), " ".join(f"{b:02x}" for b in r[:8]), flush=True)
h.close()
print("done", flush=True)

"""Print input reports from every readable numpad collection (except slider spam) for a while."""
import sys, threading, time, hid
end = time.time() + float(sys.argv[1] if len(sys.argv) > 1 else 600)
def watch(d):
    tag = f"if{d['interface_number']} {d['usage_page']:04x}/{d['usage']:04x}"
    try:
        h = hid.device(); h.open_path(d["path"])
    except Exception as e:
        print(tag, "cannot open:", e, flush=True); return
    print(tag, "open", flush=True)
    while time.time() < end:
        try:
            r = h.read(64, 500)
        except Exception as e:
            print(tag, "read error:", e, flush=True); return
        if r and not (len(r) > 2 and r[0] == 4 and r[1] == 0xF8 and r[2] == 0x32):
            print(time.strftime("%H:%M:%S"), tag, " ".join(f"{b:02x}" for b in r[:10]), flush=True)
ts = [threading.Thread(target=watch, args=(d,)) for d in hid.enumerate(0x320F, 0x5088)]
[t.start() for t in ts]; [t.join() for t in ts]
print("done", flush=True)

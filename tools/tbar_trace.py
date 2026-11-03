"""Timeline of slider positions (numpad) and OBS transition/scene events, to debug the T-bar.
Reads the numpad's event collection alongside the suite (Windows gives each handle a copy)."""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "suite"))
import obsws_python as obs  # noqa: E402
import obs_link  # noqa: E402
from gmmk_numpad import EventReader  # noqa: E402

DURATION = float(sys.argv[1]) if len(sys.argv) > 1 else 90
t0 = time.time()
out = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "tbar_trace.log"), "w", buffering=1)


def log(msg):
    out.write(f"{time.time() - t0:7.3f} {msg}\n")


ev = obs.EventClient(host="localhost", port=4455, password=obs_link.obs_password())
for name in ("scene_transition_started", "scene_transition_ended", "scene_transition_video_ended",
             "current_program_scene_changed", "current_preview_scene_changed"):
    def handler(data, name=name):
        log(f"OBS {name} {getattr(data, 'scene_name', '') or getattr(data, 'transition_name', '')}")
    handler.__name__ = "on_" + name
    ev.callback.register(handler)


def slider():
    r = EventReader()
    last = None
    while time.time() - t0 < DURATION:
        e = r.read(200)
        if e and e[0] == "slider" and e[1] != last:
            last = e[1]
            log(f"slider {e[1]}")
    r.close()


threading.Thread(target=slider, daemon=True).start()
log("tracing")
time.sleep(DURATION)
log("done")
ev.disconnect()

"""Show OBS recording/streaming status on the GMMK Numpad (stock firmware), drive OBS from its
keys (bound as host events by bind_keys.py, so no OBS hotkeys or window focus are involved),
and use its slider and knob as OBS audio faders.

Polls OBS through its built-in WebSocket server and repaints the numpad only when the state
changes (each repaint is saved to the device profile, so we avoid needless writes).

Usage:
  python obs_bridge.py           run the bridge (Ctrl+C to quit)
  python obs_bridge.py --demo    cycle through every state without OBS, to preview colours
"""
import json
import logging
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gmmk_numpad import KEY_LED, SIDE_LEFT, SIDE_RIGHT, EventReader, Numpad  # noqa: E402

logging.getLogger("obsws_python").setLevel(logging.CRITICAL)  # retries while OBS is closed are expected

OBS_HOST, OBS_PORT = "localhost", 4455
POLL_SECONDS = 0.2

# Slider fader: exact OBS audio source name (Audio Mixer), or None to disable
SLIDER_SOURCE = "Mic/Aux"
# Knob: exact OBS audio source it steps, or None to keep the knob as Windows volume
KNOB_SOURCE = "Desktop Audio"
KNOB_STEP_DB = 2.0
KNOB_FLOOR_DB = -60.0  # below this the source is set to silent (-inf)

# Keys bound as host events by bind_keys.py -> OBS WebSocket request (obsws-python method)
KEY_ACTIONS = {"Subtract": "toggle_record", "Divide": "toggle_stream", "Multiply": "toggle_record_pause"}
REC_KEY, STREAM_KEY, PAUSE_KEY = KEY_LED["Subtract"], KEY_LED["Divide"], KEY_LED["Multiply"]

BASE = (255, 255, 255)        # every other key
RED, PURPLE, AMBER = (255, 0, 0), (150, 0, 255), (255, 120, 0)
DIM_RED, DIM_PURPLE, DIM_AMBER = (30, 0, 0), (16, 0, 30), (30, 13, 0)  # idle: faint, so "on" stands out
OFF = (0, 0, 0)
SPLIT = 4  # side bars hold 7 LEDs (top to bottom); when recording + streaming, the top 4 show recording


def render(state):
    """state: None (OBS not connected) or (recording, paused, streaming) -> (colors, breathing)."""
    if state is None:
        return {REC_KEY: OFF, STREAM_KEY: OFF, PAUSE_KEY: OFF}, ()
    recording, paused, streaming = state
    rec_color = AMBER if paused else RED
    colors = {
        REC_KEY: rec_color if recording else DIM_RED,
        STREAM_KEY: PURPLE if streaming else DIM_PURPLE,
        PAUSE_KEY: AMBER if paused else DIM_AMBER,
    }
    breathing = [REC_KEY] if recording and not paused else []
    # Both side bars mirror each other, so the status is visible from either viewing angle
    for bar in (SIDE_LEFT, SIDE_RIGHT):
        for pos, i in enumerate(bar):
            if recording and (not streaming or pos < SPLIT):
                colors[i] = rec_color
                if not paused:
                    breathing.append(i)
            elif streaming:
                colors[i] = PURPLE
            else:
                colors[i] = OFF
    return colors, breathing


def obs_password():
    """Read the password from OBS's own websocket config so it never has to be typed anywhere."""
    if os.environ.get("OBS_WS_PASSWORD"):
        return os.environ["OBS_WS_PASSWORD"]
    path = os.path.join(os.environ["APPDATA"], "obs-studio", "plugin_config", "obs-websocket", "config.json")
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg.get("server_password") if cfg.get("auth_required") else None
    except OSError:
        return None


PREDICT_HOLD_SECONDS = 1.5  # after an optimistic paint, ignore OBS polls that still show the old state


class Bridge:
    def __init__(self):
        self.pad = None
        self.obs = None
        self.shown = object()  # sentinel: forces the first paint
        self.predicted = None  # prediction waiting to be painted
        self.expected = None  # newest prediction, kept for the hold period
        self.hold_until = 0.0
        self.wake = threading.Event()

    def predict(self, key):
        """Called from the controls thread right after a key action: paint the expected state now
        instead of waiting for OBS to report it (saves the poll + OBS start/stop latency)."""
        # Chain from the newest prediction, so quick presses (e.g. − then /) don't each start from the
        # still-painted old state.
        basis = self.predicted or (self.expected if time.time() < self.hold_until else None) or self.shown
        if not isinstance(basis, tuple):
            return
        rec, paused, stream = basis
        if key == "Subtract":
            rec, paused = not rec, False
        elif key == "Divide":
            stream = not stream
        elif key == "Multiply" and rec:
            paused = not paused
        else:
            return
        self.predicted = self.expected = (rec, paused, stream)
        self.hold_until = time.time() + PREDICT_HOLD_SECONDS
        self.wake.set()

    def paint(self, state):
        if state == self.shown:
            return
        colors, breathing = render(state)
        if self.pad is None:
            self.pad = Numpad()
            # Animated presets (e.g. the factory default after a reset) hide per-key colours.
            # set_effect() also sets the knob click byte that CORE leaves at 0 (which silences the knob).
            self.pad.set_effect()
            self.pad.set_knob_events(KNOB_SOURCE is not None, colors, default=BASE, breathing=breathing)
        t0, retries = time.time(), self.pad.bt_retries
        self.pad.set_leds(colors, default=BASE, breathing=breathing)
        self.shown = state
        print("numpad ->", "OBS not connected" if state is None
              else "rec=%s paused=%s stream=%s" % state,
              "(%s, %.2fs%s)" % ("BT" if self.pad.bluetooth else "USB", time.time() - t0,
                                 ", %d retry" % (self.pad.bt_retries - retries) if self.pad.bt_retries > retries else ""),
              flush=True)

    def read_obs(self):
        import obsws_python as obs
        if self.obs is None:
            self.obs = obs.ReqClient(host=OBS_HOST, port=OBS_PORT, password=obs_password(), timeout=3)
        rec = self.obs.get_record_status()
        stream = self.obs.get_stream_status()
        return bool(rec.output_active), bool(rec.output_paused), bool(stream.output_active)

    def step(self):
        if self.predicted is not None:
            state, self.predicted = self.predicted, None
            self.hold_until = time.time() + PREDICT_HOLD_SECONDS
        else:
            try:
                state = self.read_obs()
            except Exception:
                self.obs = None
                state = None
            if time.time() < self.hold_until and state != self.shown:
                return  # OBS hasn't caught up with the predicted state yet
        try:
            self.paint(state)
        except Exception as e:  # numpad unplugged / asleep: retry next poll
            print("numpad unavailable:", e, flush=True)
            if self.pad is not None:
                try:
                    self.pad.close()
                except Exception:
                    pass
            self.pad = None
            self.shown = object()


def slider_to_volume(pos):
    """0..100 -> OBS volume multiplier. Cubic curve so the travel feels even (50% = about -18 dB)."""
    return (max(0, min(100, pos)) / 100) ** 3


def run_controls(slider_source, bridge):
    """Thread: numpad keys, slider and knob -> OBS. Uses its own OBS connection (the client isn't thread-safe)."""
    import obsws_python as obs
    from obsws_python.error import OBSSDKRequestError
    reader = client = None
    sent = None
    while True:
        try:
            if reader is None:
                reader = EventReader()
            event = reader.read()
            if event is None:
                continue
            kind, value = event
            if kind == "slider" and (not slider_source or value == sent):
                continue
            if kind == "knob" and not KNOB_SOURCE:
                continue
            if client is None:
                client = obs.ReqClient(host=OBS_HOST, port=OBS_PORT, password=obs_password(), timeout=3)
            try:
                if kind == "slider":
                    client.set_input_volume(slider_source, vol_mul=slider_to_volume(value))
                    sent = value
                elif kind == "knob":
                    db = client.get_input_volume(KNOB_SOURCE).input_volume_db
                    db = max(db, KNOB_FLOOR_DB) + value * KNOB_STEP_DB
                    if db <= KNOB_FLOOR_DB:
                        client.set_input_volume(KNOB_SOURCE, vol_mul=0.0)
                    else:
                        client.set_input_volume(KNOB_SOURCE, vol_db=min(db, 0.0))
                elif value in KEY_ACTIONS:
                    getattr(client, KEY_ACTIONS[value])()
                    bridge.predict(value)
                    print("key", value, "->", KEY_ACTIONS[value], flush=True)
            except OBSSDKRequestError as e:  # e.g. pause while not recording: OBS refuses, keep going
                print("OBS refused:", e, flush=True)
        except Exception:
            # numpad unplugged or OBS closed: drop both and retry
            for res, closer in ((reader, "close"), (client, "disconnect")):
                if res is not None:
                    try:
                        getattr(res, closer)()
                    except Exception:
                        pass
            reader = client = None
            sent = None
            time.sleep(2)


def demo():
    b = Bridge()
    for state in [None, (False, False, False), (True, False, False), (True, True, False),
                  (False, False, True), (True, False, True)]:
        b.paint(state)
        time.sleep(4)


def main():
    if "--demo" in sys.argv:
        return demo()
    b = Bridge()
    threading.Thread(target=run_controls, args=(SLIDER_SOURCE, b), daemon=True).start()
    print("OBS bridge running (Ctrl+C to quit)", "| slider ->", SLIDER_SOURCE, "| knob ->", KNOB_SOURCE, flush=True)
    while True:
        b.step()
        b.wake.wait(POLL_SECONDS if b.obs else 3)  # a key press wakes us early
        b.wake.clear()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass

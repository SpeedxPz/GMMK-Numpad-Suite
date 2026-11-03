"""OBS integration: status lights on the numpad, and the OBS side of key actions, slider and knob.

The poller has its own OBS WebSocket client (obsws-python clients are not thread-safe); the controls
thread (controls.py) uses another one through ObsLink.perform / slider / knob.
Status lights follow the key map: whichever keys hold an OBS action show its state. Key presses
paint the predicted state immediately; polls that disagree are ignored for a moment so the lights
don't flicker back while OBS starts or stops (see .knowledge/obs-bridge.md).
"""
import json
import logging
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))
from gmmk_numpad import KEY_LED, SIDE_LEFT, SIDE_RIGHT  # noqa: E402

logging.getLogger("obsws_python").setLevel(logging.CRITICAL)  # retries while OBS is closed are expected

POLL_SECONDS = 0.2
PREDICT_HOLD_SECONDS = 1.5
KNOB_FLOOR_DB = -60.0
TBAR_DEADBAND = 0.02  # fraction of slider travel treated as "at the end"
SPLIT = 4  # side bars: when recording + streaming, the top 4 LEDs show recording

RED, PURPLE, AMBER = (255, 0, 0), (150, 0, 255), (255, 120, 0)
DIM_RED, DIM_PURPLE, DIM_AMBER = (30, 0, 0), (16, 0, 30), (30, 13, 0)
# Scene keys work like a video switcher's tally: live = red, in preview (Studio Mode) = orange.
TALLY_LIVE, TALLY_PREVIEW, TALLY_IDLE = (255, 0, 0), (255, 90, 0), (24, 24, 24)
LIVE_GREEN, MUTED_RED = (0, 200, 60), (220, 0, 0)
# Replay buffer keys (save + start/stop): cyan while the buffer runs, white flash when a clip is saved.
CYAN, DIM_CYAN, SAVED_WHITE = (0, 200, 200), (0, 20, 20), (255, 255, 255)
SAVED_FLASH_SECONDS = 0.6
REPLAY_ACTIONS = ("obs_replay", "obs_replay_toggle")

OBS_CALLS = {"obs_record": "toggle_record", "obs_stream": "toggle_stream",
             "obs_pause": "toggle_record_pause", "obs_replay": "save_replay_buffer",
             "obs_replay_toggle": "toggle_replay_buffer"}


def overlay_for(state, keymap, scene=None, muted=None, preview=None, replay=None, saved=False):
    """OBS state + key map -> (colors, breathing). state None (OBS not connected) -> no overlay.
    scene / preview: current program / preview scene (preview None outside Studio Mode).
    replay: replay buffer running (None = unknown / off in OBS's settings); saved: a clip was just saved."""
    if state is None:
        return {}, set()
    recording, paused, streaming = state
    rec_color = AMBER if paused else RED
    colors, breathing = {}, set()
    for key, b in keymap.items():
        led = KEY_LED.get(key)
        if led is None or b.get("type") != "action":
            continue
        action = b["action"]
        if action == "obs_record":
            colors[led] = rec_color if recording else DIM_RED
            if recording and not paused:
                breathing.add(led)
        elif action == "obs_stream":
            colors[led] = PURPLE if streaming else DIM_PURPLE
        elif action == "obs_pause":
            colors[led] = AMBER if paused else DIM_AMBER
        elif action == "obs_scene" and scene is not None:
            target = b.get("scene")
            colors[led] = TALLY_LIVE if target == scene else TALLY_PREVIEW if target == preview else TALLY_IDLE
        elif action == "obs_mute" and (muted or {}).get(b.get("source")) is not None:
            colors[led] = MUTED_RED if muted[b["source"]] else LIVE_GREEN
        elif action in REPLAY_ACTIONS:
            colors[led] = SAVED_WHITE if saved and action == "obs_replay" else CYAN if replay else DIM_CYAN
    if recording or streaming:  # idle: side bars keep the user's own colours
        for bar in (SIDE_LEFT, SIDE_RIGHT):
            for pos, i in enumerate(bar):
                if recording and (not streaming or pos < SPLIT):
                    colors[i] = rec_color
                    if not paused:
                        breathing.add(i)
                else:
                    colors[i] = PURPLE
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


def slider_to_volume(pos):
    """0..100 -> OBS volume multiplier. Cubic curve so the travel feels even (50% = about -18 dB)."""
    return (max(0, min(100, pos)) / 100) ** 3


class ObsLink:
    def __init__(self, config, device, log=print):
        self.config, self.device, self.log = config, device, log
        self.state = None          # last OBS state from a poll: (recording, paused, streaming) or None
        self.scene = None          # current program scene (polled only if a key or the knob uses scenes)
        self.preview = None        # current preview scene (Studio Mode only)
        self.muted = {}            # {source: muted} for sources that a key mutes
        self.replay = None         # replay buffer running (polled only if a key uses it; None = off in OBS)
        self._replay_expected, self._replay_hold = None, 0.0
        self.saved_until = 0.0     # replay saved: the save key flashes white until then
        self.connected = False
        self.predicted = self.expected = None
        self.hold_until = 0.0
        self.wake = threading.Event()
        self._ctl = None           # client used by the controls thread
        self._tbar_from = None     # slider end (0 / 100) where the last T-bar transition finished
        self._tbar_sent = 0.0
        self._tbar_warned = False
        self._tbar_program = None  # program scene when the current T-bar transition started
    def client(self):
        import obsws_python as obs
        c = self.config.get("obs")
        return obs.ReqClient(host=c["host"], port=c["port"], password=obs_password(), timeout=3)

    def start(self):
        threading.Thread(target=self._poller, daemon=True, name="obs-poll").start()

    # ---- status -> overlay ----
    def predict(self, action):
        if action == "obs_replay_toggle":
            if self.replay is not None:
                self.replay = self._replay_expected = not self.replay
                self._replay_hold = time.time() + PREDICT_HOLD_SECONDS
            self.wake.set()
            return
        if action == "obs_replay":
            self.saved_until = time.time() + SAVED_FLASH_SECONDS
            self.wake.set()
            return
        basis = self.predicted or (self.expected if time.time() < self.hold_until else None) or self.state
        if not isinstance(basis, tuple):
            return
        rec, paused, stream = basis
        if action == "obs_record":
            rec, paused = not rec, False
        elif action == "obs_stream":
            stream = not stream
        elif action == "obs_pause" and rec:
            paused = not paused
        else:
            return
        self.predicted = self.expected = (rec, paused, stream)
        self.hold_until = time.time() + PREDICT_HOLD_SECONDS
        self.wake.set()

    def _show(self, state):
        """Hand the OBS state to the DeviceManager, which builds each slot's status lights from it
        (every profile/layer has its own key map, so the lights differ per slot)."""
        cfg = self.config.get("obs")
        if cfg["enabled"] and cfg["overlay"] and state is not None:
            self.device.request_obs({"state": list(state), "scene": self.scene, "preview": self.preview,
                                     "muted": self.muted, "replay": self.replay,
                                     "saved": time.time() < self.saved_until})
        else:
            self.device.request_obs(None)

    def _all_actions(self):
        """Action bindings of every profile and layer (their status lights are kept up to date too)."""
        out = []
        for p, prof in self.config.get("profiles").items():
            for lay in prof["layers"].values():
                out += [b for b in lay["keymap"].values() if b.get("type") == "action"]
        return out

    def _poll(self, cl):
        rec, stream = cl.get_record_status(), cl.get_stream_status()
        actions = self._all_actions()
        if any(b["action"] == "obs_scene" for b in actions):
            self.scene = cl.get_current_program_scene().current_program_scene_name
            studio = cl.get_studio_mode_enabled().studio_mode_enabled
            self.preview = cl.get_current_preview_scene().current_preview_scene_name if studio else None
        else:
            self.scene = self.preview = None
        muted = {}
        for b in actions:
            if b["action"] == "obs_mute" and b.get("source") and b["source"] not in muted:
                try:
                    muted[b["source"]] = bool(cl.get_input_mute(b["source"]).input_muted)
                except Exception:  # source renamed / removed in OBS
                    muted[b["source"]] = None
        self.muted = muted
        if any(b["action"] in REPLAY_ACTIONS for b in actions):
            from obsws_python.error import OBSSDKRequestError
            try:
                replay = bool(cl.get_replay_buffer_status().output_active)
            except OBSSDKRequestError:  # replay buffer not enabled in OBS's Output settings
                replay = None
            if time.time() < self._replay_hold and replay != self._replay_expected:
                replay = self._replay_expected  # OBS is still starting / stopping it
            self.replay = replay
        else:
            self.replay = None
        return bool(rec.output_active), bool(rec.output_paused), bool(stream.output_active)

    def _poller(self):
        cl = None
        while True:
            enabled = self.config.get("obs")["enabled"]
            if self.predicted is not None:
                state, self.predicted = self.predicted, None
                self._show(state)
            elif enabled:
                try:
                    cl = cl or self.client()
                    polled = self._poll(cl)
                    self.connected = True
                except Exception:
                    cl, polled, self.connected = None, None, False
                self.state = polled
                if not (time.time() < self.hold_until and polled != self.expected):
                    self._show(polled)
            else:
                cl, self.state, self.connected = None, None, False
                self._show(None)
            self.wake.wait(POLL_SECONDS if self.connected else 3)
            self.wake.clear()

    # ---- numpad -> OBS (called from the controls thread only) ----
    def _call(self, fn):
        from obsws_python.error import OBSSDKRequestError
        if not self.config.get("obs")["enabled"]:
            return
        try:
            self._ctl = self._ctl or self.client()
            return fn(self._ctl)
        except OBSSDKRequestError as e:  # e.g. pause while not recording: OBS refuses
            self.log(f"OBS refused: {e}")
        except Exception as e:  # OBS closed: reconnect on the next call
            if self._ctl is not None:
                try:
                    self._ctl.disconnect()
                except Exception:
                    pass
            self._ctl = None
            self.log(f"OBS not reachable: {e}")

    def perform(self, binding, phase="press"):
        action = binding["action"]
        if action == "obs_mute" and binding.get("source") and binding.get("hold") in ("on", "off"):
            # hold "on" / "off" = muted / live while held; the release sets the other one
            muted = (binding["hold"] == "on") == (phase == "press")
            self._call(lambda cl: cl.set_input_mute(binding["source"], muted))
            self.wake.set()
            return
        if phase == "release":
            return
        if action in OBS_CALLS:
            if self._call(lambda cl: getattr(cl, OBS_CALLS[action])() or True):
                self.predict(action)
        elif action == "obs_scene" and binding.get("scene"):
            def select(cl):  # like clicking the scene in OBS: into preview in Studio Mode, else live
                if cl.get_studio_mode_enabled().studio_mode_enabled:
                    cl.set_current_preview_scene(binding["scene"])
                else:
                    cl.set_current_program_scene(binding["scene"])
            self._call(select)
            self.wake.set()
        elif action == "obs_transition":
            def transition(cl):
                if cl.get_studio_mode_enabled().studio_mode_enabled:
                    cl.trigger_studio_mode_transition()
                else:
                    self.log("Transition key: Studio Mode is off in OBS, so there is no preview to send live")
            self._call(transition)
            self.wake.set()
        elif action == "obs_mute" and binding.get("source"):
            self._call(lambda cl: cl.toggle_input_mute(binding["source"]))
            self.wake.set()

    def slider_volume(self, source, pos):
        if source:
            self._call(lambda cl: cl.set_input_volume(source, vol_mul=slider_to_volume(pos)))

    def tbar(self, pos):
        """Slider as a video-mixer T-bar (needs Studio Mode). Like a hardware fader, it travels from
        the end where the last transition finished: pushing it all the way completes the transition,
        and the next one runs back the other way (the old program scene goes back into preview).

        OBS 32.2 cannot finish a T-bar transition from outside (read in its source, then verified):
          - TBarChanged clamps the manual time to 0.01..0.99, so position 1.0 never completes it;
          - TBarReleased decides finish/cancel from the on-screen T-bar widget, which the websocket
            never moves, so a release always *cancels* (eases back to the old scene);
          - until a release, OBS stays in "T-bar active": TriggerStudioModeTransition is blocked
            (0 < t < 1) and the next SetTBarPosition moves the old transition instead of a new one.
        So the end of the travel force-cuts from 99% to the target scene by leaving and re-entering
        Studio Mode (leaving does TransitionToScene(program, force=true), which also clears the
        T-bar state). On air that's invisible; the OBS window's layout blinks once.
        Pulling back to the start releases at 0, which cancels cleanly."""
        if self._tbar_from is None:
            self._tbar_from = 0 if pos < 50 else 100
        t = abs(pos - self._tbar_from) / 100
        moving = self._tbar_sent >= TBAR_DEADBAND
        if t < TBAR_DEADBAND and not moving:
            return  # resting at the start (or jitter right after a finished transition)
        done = t >= 1 - TBAR_DEADBAND

        def move(cl):
            if not cl.get_studio_mode_enabled().studio_mode_enabled:
                if not self._tbar_warned:
                    self.log("slider is set to the OBS transition T-bar, but Studio Mode is off in OBS")
                    self._tbar_warned = True
                return False
            self._tbar_warned = False
            if not moving:  # a transition starts: remember what's on air, for the swap afterwards
                self._tbar_program = cl.get_current_program_scene().current_program_scene_name
            if done:
                cl.send("SetTBarPosition", {"position": 1.0, "release": False})
                time.sleep(0.15)  # let the eased fade arrive at OBS's 99% before the cut
                cl.set_studio_mode_enabled(False)  # force-cut to the target, clears the T-bar state
                cl.set_studio_mode_enabled(True)
                if self._tbar_program:  # the old scene goes back into preview: the next push fades back
                    cl.set_current_preview_scene(self._tbar_program)
            elif t < TBAR_DEADBAND:  # pulled back to the start: cancel
                cl.send("SetTBarPosition", {"position": 0.0, "release": True})
            else:
                cl.send("SetTBarPosition", {"position": t, "release": False})
            return True
        if self._call(move):
            self._tbar_sent = 0.0 if done or t < TBAR_DEADBAND else t
            if done:
                self._tbar_from = 100 - self._tbar_from
                self.wake.set()

    def knob_volume(self, source, step_db, steps):
        if not source:
            return

        def turn(cl):
            db = cl.get_input_volume(source).input_volume_db
            db = max(db, KNOB_FLOOR_DB) + steps * step_db
            if db <= KNOB_FLOOR_DB:
                cl.set_input_volume(source, vol_mul=0.0)
            else:
                cl.set_input_volume(source, vol_db=min(db, 0.0))
        self._call(turn)

    def knob_scene(self, steps):
        """Each click moves to the next (clockwise) or previous scene, in the order OBS lists them.
        In Studio Mode it steps the Preview scene instead (like the scene keys); TAKE sends it live."""
        def turn(cl):
            scenes = [s["sceneName"] for s in reversed(cl.get_scene_list().scenes)]
            if not scenes:
                return
            studio = cl.get_studio_mode_enabled().studio_mode_enabled
            current = (cl.get_current_preview_scene().current_preview_scene_name if studio
                       else cl.get_current_program_scene().current_program_scene_name)
            i = scenes.index(current) if current in scenes else 0
            target = scenes[(i + steps) % len(scenes)]
            if studio:
                cl.set_current_preview_scene(target)
            else:
                cl.set_current_program_scene(target)
        self._call(turn)
        self.wake.set()

    def lists(self):
        """Audio sources and scenes in OBS (for the dashboard's pickers)."""
        cl = self.client()
        try:
            sources = []
            for i in cl.get_input_list().inputs:
                try:
                    cl.get_input_volume(i["inputName"])
                    sources.append(i["inputName"])
                except Exception:
                    pass
            scenes = [s["sceneName"] for s in reversed(cl.get_scene_list().scenes)]
            return {"sources": sources, "scenes": scenes}
        finally:
            cl.disconnect()

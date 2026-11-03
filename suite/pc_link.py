"""Windows integrations that need no setup: a mic mute key for every app at once, and media keys
(play / pause, next, previous) with a playing light. Status lights go through
DeviceManager.request_integration("pc", ...).

- Mic mute: the default recording device's own mute (Core Audio, pycaw), so every app hears
  silence. Windows has two defaults ("Default Device" for apps, "Default Communications Device" for
  calls) that can be different mics, so both are muted; the light is red only if all of them are.
- Media: the session Windows' media overlay controls (System Media Transport Controls, winrt), so
  it works with Spotify, browsers, players... Without a session the media keys are sent instead.

One worker thread owns COM and the winrt event loop; the controls thread only queues actions.
See .knowledge/suite.md "PC integrations".
"""
import asyncio
import ctypes
import os
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))
from gmmk_numpad import KEY_LED  # noqa: E402

POLL_SECONDS = 0.5
PC_ACTIONS = ("mic_mute", "media_play_pause", "media_next", "media_prev")
E_RENDER, E_CAPTURE, ROLES = 0, 1, (0, 2)  # eRender, eCapture; eConsole, eCommunications
DEVICE_STATE_ACTIVE = 1
PLAYING = 4                    # GlobalSystemMediaTransportControlsSessionPlaybackStatus.PLAYING
VK = {"media_play_pause": 0xB3, "media_next": 0xB0, "media_prev": 0xB1}
KEYEVENTF_KEYUP = 0x2

LIVE_GREEN, MUTED_RED = (0, 200, 60), (220, 0, 0)  # same as the other mute keys
PLAY_ON, PLAY_OFF = (0, 200, 60), (40, 25, 0)      # playing / paused or nothing playing


def pc_overlay(view, keymap):
    """{"mic_muted": bool | None, "playing": bool} + key map -> {led: colour}. view None -> no lights."""
    if not view:
        return {}
    colors = {}
    for key, b in keymap.items():
        led = KEY_LED.get(key)
        if led is None or b.get("type") != "action":
            continue
        if b["action"] == "mic_mute" and view["mic_muted"] is not None:
            colors[led] = MUTED_RED if view["mic_muted"] else LIVE_GREEN
        elif b["action"] == "media_play_pause":
            colors[led] = PLAY_ON if view["playing"] else PLAY_OFF
    return colors


def _mics():
    """IAudioEndpointVolume of each default recording device (one per distinct device)."""
    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    enum = AudioUtilities.GetDeviceEnumerator()
    seen, out = set(), []
    for role in ROLES:
        try:
            dev = enum.GetDefaultAudioEndpoint(E_CAPTURE, role)
        except Exception:  # no recording device at all
            continue
        if dev.GetId() in seen:
            continue
        seen.add(dev.GetId())
        # QueryInterface, not ctypes.cast: a cast wrapper doesn't AddRef, so the object was released twice
        # (access violation when the garbage collector got to it: the suite closed with no log).
        out.append(dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None).QueryInterface(IAudioEndpointVolume))
    return out


class PcLink:
    def __init__(self, config, device, log=print):
        self.config, self.device, self.log = config, device, log
        self.view = None            # {"mic_muted", "playing", "app"} (see pc_overlay)
        self.queue = []
        self.lock = threading.Lock()
        self.levels, self.steps = {}, []  # app volume targets from the slider / knob (worker applies them)
        self.apps = []              # programs with an audio session now (dashboard list)
        self.wake = threading.Event()

    def start(self):
        threading.Thread(target=self._worker, daemon=True, name="pc").start()

    def perform(self, binding, phase="press"):
        self.queue.append((binding["action"], binding.get("hold"), phase))
        self.wake.set()

    def slider_volume(self, app, pos):
        """Slider -> that program's volume (the Volume mixer slider): top = 100 %."""
        if app:
            with self.lock:
                self.levels[app] = max(0, min(100, pos)) / 100
            self.wake.set()

    def knob_volume(self, app, step, steps):
        """Knob -> that program's volume, `step` % per click."""
        if app:
            with self.lock:
                self.steps.append((app, step / 100 * steps))
            self.wake.set()

    # ---- worker ----
    def _worker(self):
        import comtypes
        comtypes.CoInitialize()
        loop = asyncio.new_event_loop()
        manager = None
        while True:
            try:
                if manager is None:
                    from winrt.windows.media.control import \
                        GlobalSystemMediaTransportControlsSessionManager as Manager
                    manager = loop.run_until_complete(Manager.request_async())
            except Exception as e:
                self.log(f"media controls unavailable: {e}")
                manager = False  # don't retry every poll
            while self.queue:
                action, hold, phase = self.queue.pop(0)
                try:
                    self._run(loop, manager, action, hold, phase)
                except Exception as e:
                    self.log(f"{action}: {e}")
            self._apply_volumes()
            view = {"mic_muted": None, "playing": False, "app": ""}
            try:
                mics = _mics()
                view["mic_muted"] = all(m.GetMute() for m in mics) if mics else None
            except Exception:  # a device being switched / reconfigured: keep the last state
                view["mic_muted"] = self.view["mic_muted"] if self.view else None
            try:
                for session in (manager.get_sessions() if manager else []):
                    if session.get_playback_info().playback_status == PLAYING:
                        view["playing"], view["app"] = True, session.source_app_user_model_id
                        break
            except Exception:
                pass
            try:
                self.apps = sorted({s.Process.name() for s in _sessions()}, key=str.lower)
            except Exception:
                pass
            if view != self.view:
                self.view = view
                self.device.request_integration("pc", view)
            self.wake.wait(POLL_SECONDS)
            self.wake.clear()

    def _run(self, loop, manager, action, hold, phase):
        if action == "mic_mute":
            # A press flips the mute; hold "on" / "off" = the state while held, the release the other.
            mics = _mics()
            if not mics:
                raise RuntimeError("no recording device")
            if hold in ("on", "off"):
                muted = (hold == "on") == (phase == "press")
            elif phase == "press":
                muted = not all(m.GetMute() for m in mics)
            else:
                return
            for m in mics:
                m.SetMute(muted, None)
        elif action in VK and phase == "press":
            sessions = list(manager.get_sessions()) if manager else []
            playing = [s for s in sessions if s.get_playback_info().playback_status == PLAYING]
            session = playing[0] if playing else None
            if not session:
                session = manager.get_current_session() if manager else None
            if session:
                call = {"media_play_pause": session.try_toggle_play_pause_async,
                        "media_next": session.try_skip_next_async,
                        "media_prev": session.try_skip_previous_async}[action]
                if loop.run_until_complete(call()):
                    return
            vk = VK[action]  # no session (or it refused): the media key, as a keyboard would send it
            ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
            ctypes.windll.user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)

    def _apply_volumes(self):
        with self.lock:
            levels, steps = self.levels, self.steps
            self.levels, self.steps = {}, []
        if not (levels or steps):
            return
        try:
            sessions = list(_sessions())
            for app, level in levels.items():
                for s in _matching(sessions, app):
                    s.SimpleAudioVolume.SetMasterVolume(level, None)
            for app, delta in steps:
                for s in _matching(sessions, app):
                    v = s.SimpleAudioVolume
                    v.SetMasterVolume(max(0.0, min(1.0, v.GetMasterVolume() + delta)), None)
        except Exception as e:
            self.log(f"app volume: {e}")


def _sessions():
    """Audio sessions of programs on every active playback device (the Volume mixer's rows). Not
    only the default one: with VoiceMeeter, programs often play to one of its virtual inputs."""
    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import AudioUtilities, IAudioSessionControl2, IAudioSessionManager2
    from pycaw.utils import AudioSession
    devices = AudioUtilities.GetDeviceEnumerator().EnumAudioEndpoints(E_RENDER, DEVICE_STATE_ACTIVE)
    for d in range(devices.GetCount()):
        try:
            mgr = devices.Item(d).Activate(IAudioSessionManager2._iid_, CLSCTX_ALL, None).QueryInterface(
                IAudioSessionManager2)  # not ctypes.cast, see _mics
            sessions = mgr.GetSessionEnumerator()
            for i in range(sessions.GetCount()):
                ctl = sessions.GetSession(i)
                s = AudioSession(ctl.QueryInterface(IAudioSessionControl2)) if ctl else None
                if s and s.Process:
                    yield s
        except Exception:  # a device going away mid-scan
            continue


def _matching(sessions, app):
    """A program can have several sessions (browsers): all of them follow the control."""
    return [s for s in sessions if s.Process.name().lower() == app.lower()]

"""Numpad events -> actions: host-event keys run their PC action, the slider and knob do what their
profile says (Windows volume or an OBS control), and profile changes made on the numpad (Fn+9 /
Fn+7, layers Fn+3 / Fn+1) are passed to the DeviceManager."""
import ctypes
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))
from gmmk_numpad import EventReader  # noqa: E402
from pc_link import PC_ACTIONS  # noqa: E402

ASFW_ANY = -1


class WindowsVolume:
    """Default playback device's master volume (Core Audio via pycaw). COM is per thread, so this
    is created on, and only used from, the controls thread."""

    def __init__(self):
        import comtypes
        from pycaw.pycaw import AudioUtilities
        comtypes.CoInitialize()
        self.endpoint = AudioUtilities.GetSpeakers().EndpointVolume

    def set(self, level):
        self.endpoint.SetMasterVolumeLevelScalar(max(0.0, min(1.0, level)), None)


def launch(target):
    """Open a program, file, folder or URL like the Run dialog would; extra words are arguments.

    ShellExecute (os.startfile) resolves bare names such as "notepad" through PATH and App Paths.
    The suite is a background process, so it first lets the new window take the foreground."""
    ctypes.windll.user32.AllowSetForegroundWindow(ASFW_ANY)
    target = target.strip()
    if os.path.exists(target) or "://" in target:
        return os.startfile(target)
    if target.startswith('"'):
        program, _, args = target[1:].partition('"')
    else:
        program, _, args = target.partition(" ")
    os.startfile(program, arguments=args.strip())


class Controls(threading.Thread):
    def __init__(self, config, device, obs, discord, voicemeeter, pc, log=print):
        super().__init__(daemon=True, name="controls")
        self.config, self.device, self.obs, self.discord, self.log = config, device, obs, discord, log
        self.voicemeeter, self.pc = voicemeeter, pc
        self.held = {}  # {key: binding} hold bindings pressed and not yet released
        self.slider_pos = None  # last slider position 0..100 seen (dashboard preview), None = unknown

    def run(self):
        reader, sent_slider, lost = None, None, False
        self.winvol = None
        while True:
            try:
                if reader is None:
                    reader = EventReader()
                    if lost:  # the numpad was away: the DeviceManager must repaint it
                        self.device.on_reconnected()
                    lost = False
                event = reader.read()
            except Exception:  # unplugged / BT asleep
                if reader is not None:
                    try:
                        reader.close()
                    except Exception:
                        pass
                    self.device.on_lost()
                reader, sent_slider, self.slider_pos = None, None, None
                lost = True
                time.sleep(0.5)
                continue
            if event is None:
                continue
            kind, value = event
            try:
                if kind == "profile":
                    self.device.on_device_slot(*value)
                elif kind == "press":
                    self.device.note_key()
                    self.press(value)
                elif kind == "release":
                    self.release(value)
                elif kind == "slider":
                    self.slider_pos = value
                if kind == "slider" and value != sent_slider:
                    self.slider(value)
                    sent_slider = value
                elif kind == "knob":
                    self.knob(value)
            except Exception as e:
                self.log(f"{kind} {value}: {e}")

    def slider(self, pos):
        cfg = self.config.layer()["controls"]["slider"]
        if cfg["type"] == "windows":
            # Default device can change (headset plugged in): re-resolve it after a failure.
            try:
                self.winvol = self.winvol or WindowsVolume()
                self.winvol.set(pos / 100)
            except Exception:
                self.winvol = None
                raise
        elif cfg["type"] == "obs_volume":
            self.obs.slider_volume(cfg["source"], pos)
        elif cfg["type"] == "obs_tbar":
            self.obs.tbar(pos)
        elif cfg["type"] == "app_volume":
            self.pc.slider_volume(cfg["source"], pos)
        elif cfg["type"] == "vm_gain" and self.config.get("voicemeeter")["enabled"]:
            self.voicemeeter.slider_gain(cfg["source"], pos)

    def knob(self, steps):
        # "windows" never gets here: the numpad sends its own volume keys in that mode.
        cfg = self.config.layer()["controls"]["knob"]
        if cfg["type"] == "obs_volume":
            self.obs.knob_volume(cfg["source"], cfg["step_db"], steps)
        elif cfg["type"] == "obs_scene":
            self.obs.knob_scene(steps)
        elif cfg["type"] == "app_volume":
            self.pc.knob_volume(cfg["source"], cfg["step_db"], steps)
        elif cfg["type"] == "vm_gain":
            self.voicemeeter.knob_gain(cfg["source"], cfg["step_db"], steps)

    def press(self, key):
        binding = self.config.layer()["keymap"].get(key)
        if not binding or binding.get("type") != "action":
            return
        action = binding["action"]
        self.log(f"key {key} -> {action}{' (hold)' if binding.get('hold') else ''}")
        if binding.get("hold") is True:  # first hold version (a boolean, tested as push-to-talk)
            binding = {**binding, "hold": "off"}
        if binding.get("hold"):
            self.held[key] = binding  # the release ends this binding, even if the key map changes meanwhile
        self._run(binding, "press")

    def release(self, key):
        """Hold bindings: the key's release puts back what its press changed."""
        binding = self.held.pop(key, None)
        if binding:
            self._run(binding, "release")

    def _run(self, binding, phase):
        """phase: "press" (a normal binding, or the start of a hold) or "release" (end of a hold)."""
        action = binding["action"]
        if action == "launch":
            if binding.get("target"):
                launch(binding["target"])
        elif action == "battery":
            if phase == "press":
                self.device.request_battery()
        elif action in PC_ACTIONS:
            self.pc.perform(binding, phase)
        elif action.startswith("discord_"):
            self.discord.perform(binding, phase)
        elif action.startswith("vm_"):
            if self.config.get("voicemeeter")["enabled"]:
                self.voicemeeter.perform(binding, phase)
        else:
            self.obs.perform(binding, phase)

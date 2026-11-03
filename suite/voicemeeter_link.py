"""VoiceMeeter integration: mute / routing / Macro Button keys with status lights, and strip or bus
gain on the knob and slider, through VoiceMeeter's own Remote API (VoicemeeterRemote64.dll, which
ships with VoiceMeeter; no account or setup in VoiceMeeter needed).

The API is not thread-safe, so one worker thread makes every DLL call: the controls thread only
queues actions and gain targets. Values are re-read when VoiceMeeter reports them dirty
(VBVMR_IsParametersDirty / VBVMR_MacroButton_IsDirty), so changes made in VoiceMeeter itself
show up on the numpad too. See .knowledge/suite.md "VoiceMeeter integration".
"""
import ctypes
import os
import sys
import threading
import time
import winreg

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))
from gmmk_numpad import KEY_LED  # noqa: E402

POLL_SECONDS = 0.1
LISTS_SECONDS = 5.0           # strip / bus names are re-read this often (labels can be renamed)
GAIN_MIN, GAIN_MAX = -60.0, 12.0  # VoiceMeeter's fader range
MACRO_STATE = 1               # VBVMR_MacroButton_* bitmode: the button's state (on / off)
VM_ACTIONS = ("vm_mute", "vm_route", "vm_macro_toggle", "vm_macro_press")
# Editions (VBVMR_GetVoicemeeterType): hardware strips, virtual strips, A buses, B buses
EDITIONS = {1: ("Basic", 2, 1, 1, 1), 2: ("Banana", 3, 2, 3, 2), 3: ("Potato", 5, 3, 5, 3)}
VIRTUAL_NAMES = ["Voicemeeter Input", "Voicemeeter AUX", "VAIO3"]
MACRO_BUTTONS = 80

LIVE_GREEN, MUTED_RED = (0, 200, 60), (220, 0, 0)    # muted is the state to notice: clear red (user, 2026-11-01)
ROUTE_ON, ROUTE_OFF = (0, 120, 255), (0, 12, 25)
BUTTON_ON, BUTTON_OFF = (255, 180, 0), (25, 18, 0)


def watched(binding):
    """What a VoiceMeeter key shows: ("param", name) or ("button", id), or None."""
    a, target = binding.get("action"), binding.get("vm") or ""
    if not target:
        return None
    if a == "vm_mute":
        return ("param", f"{target}.Mute")
    if a == "vm_route":
        return ("param", target)
    if a in ("vm_macro_toggle", "vm_macro_press") and target.isdigit():
        return ("button", target)
    return None


def vm_overlay(view, keymap):
    """VoiceMeeter values {"params": {name: value}, "buttons": {id: state}} + key map -> {led: colour}."""
    if not view:
        return {}
    colors = {}
    for key, b in keymap.items():
        led = KEY_LED.get(key)
        if led is None or b.get("type") != "action" or b.get("action") not in VM_ACTIONS:
            continue
        w = watched(b)
        value = view["params" if w and w[0] == "param" else "buttons"].get(w[1]) if w else None
        if value is None:
            continue
        on = value >= 0.5
        if b["action"] == "vm_mute":
            colors[led] = MUTED_RED if on else LIVE_GREEN
        elif b["action"] == "vm_route":
            colors[led] = ROUTE_ON if on else ROUTE_OFF
        else:
            colors[led] = BUTTON_ON if on else BUTTON_OFF
    return colors


def dll_path():
    """VoicemeeterRemote64.dll next to the installed VoiceMeeter (the suite is a 64-bit process)."""
    for root in (r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
                 r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, root + r"\VB:Voicemeeter {17359A74-1236-5467}") as k:
                folder = os.path.dirname(winreg.QueryValueEx(k, "UninstallString")[0])
                if os.path.exists(os.path.join(folder, "VoicemeeterRemote64.dll")):
                    return os.path.join(folder, "VoicemeeterRemote64.dll")
        except OSError:
            pass
    return r"C:\Program Files (x86)\VB\Voicemeeter\VoicemeeterRemote64.dll"


class VoicemeeterLink:
    def __init__(self, config, device=None, log=print):
        self.config, self.device, self.log = config, device, log
        self.status = "off"          # off | not_installed | not_running | connected | error
        self.error = ""
        self.edition = None          # "Potato" ...
        self.lists = {"strips": [], "buses": [], "outputs": [], "buttons": MACRO_BUTTONS}
        self.view = None
        self.wake = threading.Event()
        self.lock = threading.Lock()
        self.queue = []              # key actions from the numpad
        self.gains = {}              # {"Strip[5]": dB} absolute targets (slider), latest wins
        self.steps = []              # [(target, step_db, steps)] knob turns
        self._dll = None
        self._logged_in = False
        self._lists_at = 0.0

    def start(self):
        threading.Thread(target=self._worker, daemon=True, name="voicemeeter").start()

    # ---- called from other threads ----
    def perform(self, binding, phase="press"):
        with self.lock:
            self.queue.append((dict(binding), phase))  # release only comes for hold bindings
        self.wake.set()

    def slider_gain(self, target, pos):
        if target:
            with self.lock:
                self.gains[target] = GAIN_MIN + max(0, min(100, pos)) * (GAIN_MAX - GAIN_MIN) / 100
            self.wake.set()

    def knob_gain(self, target, step_db, steps):
        if target:
            with self.lock:
                self.steps.append((target, step_db, steps))
            self.wake.set()

    # ---- DLL (worker thread only) ----
    def _get(self, name):
        value = ctypes.c_float()
        return value.value if self._dll.VBVMR_GetParameterFloat(name.encode(), ctypes.byref(value)) == 0 else None

    def _set(self, name, value):
        if self._dll.VBVMR_SetParameterFloat(name.encode(), ctypes.c_float(value)) != 0:
            raise ValueError(f"VoiceMeeter has no parameter {name}")

    def _label(self, name):
        buf = ctypes.create_unicode_buffer(512)
        return buf.value if self._dll.VBVMR_GetParameterStringW(name.encode(), buf) == 0 else ""

    def _button(self, i):
        value = ctypes.c_float()
        r = self._dll.VBVMR_MacroButton_GetStatus(int(i), ctypes.byref(value), MACRO_STATE)
        return value.value if r == 0 else None

    def _set_button(self, i, state):
        self._dll.VBVMR_MacroButton_SetStatus(int(i), ctypes.c_float(state), MACRO_STATE)

    def _login(self):
        if self._dll is None:
            path = dll_path()
            if not os.path.exists(path):
                raise FileNotFoundError("VoiceMeeter is not installed")
            self._dll = ctypes.WinDLL(path)
        r = self._dll.VBVMR_Login()
        if r < 0:
            raise OSError(f"VoiceMeeter API login failed ({r})")
        self._logged_in = True

    def _logout(self):
        if self._logged_in:
            try:
                self._dll.VBVMR_Logout()
            except OSError:
                pass
            self._logged_in = False

    def _edition(self):
        t = ctypes.c_long()
        if self._dll.VBVMR_GetVoicemeeterType(ctypes.byref(t)) != 0:
            return None  # VoiceMeeter is not running
        return EDITIONS.get(t.value)

    def _read_lists(self, edition):
        _, hw, virt, a, b = edition
        strips = []
        for i in range(hw + virt):
            name = self._label(f"Strip[{i}].Label")
            default = f"Input {i + 1}" if i < hw else VIRTUAL_NAMES[i - hw]
            strips.append({"id": f"Strip[{i}]", "name": f"{name} ({default})" if name else default})
        outputs = [f"A{n + 1}" for n in range(a)] + [f"B{n + 1}" for n in range(b)]
        buses = []
        for i, out in enumerate(outputs):
            name = self._label(f"Bus[{i}].Label")
            buses.append({"id": f"Bus[{i}]", "name": f"{out} {name}" if name else out})
        self.lists = {"strips": strips, "buses": buses, "outputs": outputs, "buttons": MACRO_BUTTONS}

    def _run(self, binding, phase):
        """A normal press flips the state. Hold ("on" / "off" = the state while held): the press sets
        it, the release sets the other one (mute + "off" = push-to-talk). Macro "press" + hold keeps
        the button down while the key is held."""
        a, target, hold = binding["action"], binding.get("vm") or "", binding.get("hold")
        if not target:
            return
        if a == "vm_macro_press":
            if not target.isdigit():
                return
            if hold:
                self._set_button(target, 1.0 if phase == "press" else 0.0)
            else:  # a click: down, then up
                self._set_button(target, 1.0)
                time.sleep(0.05)
                self._set_button(target, 0.0)
            return
        if a == "vm_macro_toggle":
            if not target.isdigit():
                return
            get, put = (lambda: self._button(target)), (lambda v: self._set_button(target, v))
        else:
            name = f"{target}.Mute" if a == "vm_mute" else target
            get, put = (lambda: self._get(name)), (lambda v: self._set(name, v))
        if hold in ("on", "off"):
            held = 1.0 if hold == "on" else 0.0
            put(held if phase == "press" else 1.0 - held)
        elif phase == "press":
            put(0.0 if (get() or 0) >= 0.5 else 1.0)

    def _watch_list(self):
        out = set()
        for prof in self.config.get("profiles").values():
            for lay in prof["layers"].values():
                for b in lay["keymap"].values():
                    if b.get("type") == "action" and b.get("action") in VM_ACTIONS and watched(b):
                        out.add(watched(b))
        return out

    def _publish(self, view):
        self.view = view
        if self.device is not None:
            self.device.request_integration("voicemeeter", view)

    def _worker(self):
        last_watch, values = None, None
        while True:
            try:
                if not self.config.get("voicemeeter")["enabled"]:
                    self._logout()
                    with self.lock:
                        self.queue.clear(); self.gains.clear(); self.steps.clear()
                    self.status, self.error = "off", ""
                    self._publish(None)
                    self.wake.wait(1.0)
                    self.wake.clear()
                    continue
                if not self._logged_in:
                    self._login()
                edition = self._edition()
                if edition is None:
                    self.status, self.edition, last_watch, values = "not_running", None, None, None
                    self._publish(None)
                    self.wake.wait(1.0)
                    self.wake.clear()
                    continue
                if self.edition != edition[0] or time.time() - self._lists_at > LISTS_SECONDS:
                    self.edition = edition[0]
                    self._read_lists(edition)
                    self._lists_at = time.time()
                with self.lock:
                    queue, gains, steps = self.queue, self.gains, self.steps
                    self.queue, self.gains, self.steps = [], {}, []
                changed = bool(queue or gains or steps)
                for binding, phase in queue:
                    self._run(binding, phase)
                for target, db in gains.items():
                    self._set(f"{target}.Gain", round(db, 1))
                for target, step, n in steps:
                    db = self._get(f"{target}.Gain")
                    if db is not None:
                        self._set(f"{target}.Gain", max(GAIN_MIN, min(GAIN_MAX, db + step * n)))
                dirty = self._dll.VBVMR_IsParametersDirty() == 1
                dirty = self._dll.VBVMR_MacroButton_IsDirty() == 1 or dirty
                watch = self._watch_list()
                if dirty or changed or watch != last_watch or values is None:
                    params, buttons = {}, {}
                    for kind, name in watch:
                        if kind == "param":
                            params[name] = self._get(name)
                        else:
                            buttons[name] = self._button(name)
                    values, last_watch = {"params": params, "buttons": buttons}, watch
                self.status, self.error = "connected", ""
                self._publish(values)
            except FileNotFoundError as e:
                self.status, self.error = "not_installed", str(e)
                self._publish(None)
                self.wake.wait(5.0)
            except (OSError, ValueError) as e:
                if self.error != str(e):
                    self.log(f"VoiceMeeter: {e}")
                self.status, self.error = "error", str(e)
                values = None
                self.wake.wait(2.0)
            self.wake.wait(POLL_SECONDS)
            self.wake.clear()

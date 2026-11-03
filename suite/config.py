"""Settings, persisted as JSON in %APPDATA%\\GMMK Numpad Suite\\config.json.

The numpad has 3 profiles x 3 layers ("slots"). Each slot stores its own key table, macro modes,
knob mode, effect and LEDs on the device. In the suite:
  - lighting is per profile (written to all three of its layers), plus an optional side-LED layer
    indicator, so switching layers keeps the look;
  - key map and knob/slider controls are per layer;
  - macros are global on the device (stored by ID), so they live outside the profiles.
"""
import copy
import json
import os
import threading

from keymap import DEFAULT_KEYMAP

CONFIG_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "GMMK Numpad Suite")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
PROFILES = (1, 2, 3)
LAYERS = (1, 2, 3)

DEFAULT_LIGHTING = {
    "effect": 7,              # 7 = static per-key (see gmmk_numpad.EFFECTS)
    "color": [255, 255, 255],  # effect colour; also the base colour of keys without their own
    "speed": 50,              # 0..100
    "brightness": 100,        # 0..100
    "sidelights": True,
    "multicolor": False,      # animated effects in rainbow colours instead of the base colour (cmd 0x02 [10])
    "per_key": {},            # {"<led index>": [r, g, b]}
    # Side bars show the active layer: the bottom 1/2/3 LEDs of both bars light in this colour.
    "layer_indicator": {"enabled": False, "color": [0, 170, 255]},
}
# What turning the knob and moving the slider do (types: see keymap.KNOB_TYPES / SLIDER_TYPES).
DEFAULT_CONTROLS = {
    "knob": {"type": "obs_volume", "source": "Desktop Audio", "step_db": 2.0},
    "slider": {"type": "obs_volume", "source": "Mic/Aux"},
}
DEFAULT_LAYER = {"keymap": DEFAULT_KEYMAP, "controls": DEFAULT_CONTROLS}
# Out-of-the-box numpad behaviour: factory keys, knob = Windows volume, slider unused.
FACTORY_LAYER = {"keymap": {}, "controls": {"knob": {"type": "windows", "source": "", "step_db": 2.0},
                                             "slider": {"type": "none", "source": ""}}}
LEGACY_CONTROL_KEYS = ("slider_source", "knob_source", "knob_step_db")  # flat form, before knob/slider types

DEFAULTS = {
    "active_profile": 1,
    "profiles": {str(p): {"name": f"Profile {p}", "lighting": DEFAULT_LIGHTING, "active_layer": 1,
                          "layers": {str(l): DEFAULT_LAYER for l in LAYERS}}
                 for p in PROFILES},
    "macros": {},  # {"<id>": {"name": str, "events": [[hid_code, pressed, delay_ms], ...]}}
    "obs": {
        "enabled": True,
        "host": "localhost",
        "port": 4455,
        "overlay": True,                 # status lights on the keys that hold OBS actions + side bars
    },
    # Discord RPC (discord_link.py): the user's own app from the Developer Portal + its OAuth token.
    # Not part of backups (BACKUP_SECTIONS): the secret and token stay on this PC.
    "discord": {"enabled": False, "client_id": "", "client_secret": "",
                "access_token": "", "refresh_token": "", "expires_at": 0},
    "voicemeeter": {"enabled": False},  # voicemeeter_link.py (VoiceMeeter's own Remote API)
    # Over Bluetooth, at or under `level` % the side bars breathe the battery gauge for a few seconds
    # at every battery read (DeviceManager._refresh_info, every 5 min).
    "battery_warning": {"enabled": True, "level": 20},
    "server": {"port": 8765},
    # Numpad-wide settings (cmd 0x08 + properties byte [10]); see gmmk_numpad.set_device_settings
    "device": {
        "polling_hz": 1000,   # USB key report rate: 1000 / 500 / 250 / 125
        "scan_ms": 8,         # key scan interval 2..8 ms (stock 8; lower = snappier, less bounce filtering)
        "led_sleep_min": 0,   # lighting off after this many idle minutes, 0 = never
    },
}
REPLACED = {"per_key", "keymap", "macros"}  # taken as a whole from the file, never merged with defaults


def _merge(base, override):
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        mergeable = isinstance(v, dict) and isinstance(out.get(k), dict) and k not in REPLACED
        out[k] = _merge(out[k], v) if mergeable else copy.deepcopy(v)
    return out


def _controls_from_flat(flat):
    """{slider_source, knob_source, knob_step_db} (null = unused / Windows volume) -> DEFAULT_CONTROLS form."""
    out = copy.deepcopy(DEFAULT_CONTROLS)
    if "knob_source" in flat:
        out["knob"] = ({"type": "obs_volume", "source": flat["knob_source"]} if flat["knob_source"]
                       else {"type": "windows", "source": ""})
    out["knob"]["step_db"] = flat.get("knob_step_db", 2.0)
    if "slider_source" in flat:
        out["slider"] = ({"type": "obs_volume", "source": flat["slider_source"]} if flat["slider_source"]
                         else {"type": "none", "source": ""})
    return out


def _migrate(saved):
    """Older config files -> the current form (each step is a no-op on newer files)."""
    if "lighting" in saved and "profiles" not in saved:  # v1: one lighting setup
        saved["profiles"] = {str(p): {"lighting": saved["lighting"]} for p in PROFILES}
    saved.pop("lighting", None)
    legacy = {k: saved.get("obs", {}).pop(k) for k in LEGACY_CONTROL_KEYS if k in saved.get("obs", {})}
    for p in PROFILES:
        prof = saved.setdefault("profiles", {}).setdefault(str(p), {})
        # slider/knob: obs section -> per profile, flat -> typed
        controls = prof.get("controls", legacy)
        if controls and not ("knob" in controls and "slider" in controls):
            prof["controls"] = _controls_from_flat(controls)
        # key map + controls: per profile -> per layer; layers 2 and 3 start as copies of layer 1
        if "layers" not in prof and ("keymap" in prof or "controls" in prof):
            first = {k: prof.pop(k) for k in ("keymap", "controls") if k in prof}
            prof["layers"] = {str(l): copy.deepcopy(first) for l in LAYERS}
    return saved


class Config:
    def __init__(self):
        self.lock = threading.Lock()
        self.data = copy.deepcopy(DEFAULTS)
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                saved = json.load(f)
        except (OSError, ValueError):
            return
        self.data = _merge(DEFAULTS, _migrate(saved))

    def _save(self):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2)
        os.replace(tmp, CONFIG_PATH)

    def get(self, section):
        with self.lock:
            return copy.deepcopy(self.data[section])

    def update(self, section, values):
        """Merge `values` into a top-level section (obs, server) and save."""
        with self.lock:
            self.data[section] = _merge(self.data[section], values)
            self._save()
            return copy.deepcopy(self.data[section])

    # ---- active slot ----
    def active(self):
        with self.lock:
            return self.data["active_profile"]

    def active_slot(self):
        """(profile, layer) that the numpad shows."""
        with self.lock:
            p = self.data["active_profile"]
            return p, self.data["profiles"][str(p)]["active_layer"]

    def set_active(self, profile, layer=None):
        """Switch profile and/or layer (layer None = the profile's last used layer)."""
        with self.lock:
            if profile not in PROFILES or (layer is not None and layer not in LAYERS):
                return
            prof = self.data["profiles"][str(profile)]
            changed = profile != self.data["active_profile"] or (layer is not None and layer != prof["active_layer"])
            self.data["active_profile"] = profile
            if layer is not None:
                prof["active_layer"] = layer
            if changed:
                self._save()

    # ---- profiles / layers ----
    def profile(self, profile=None):
        with self.lock:
            return copy.deepcopy(self.data["profiles"][str(profile or self.data["active_profile"])])

    def layer(self, profile=None, layer=None):
        """{"keymap", "controls"} of a slot (default: the active one)."""
        with self.lock:
            prof = self.data["profiles"][str(profile or self.data["active_profile"])]
            return copy.deepcopy(prof["layers"][str(layer or prof["active_layer"])])

    def update_profile(self, profile, lighting=None, name=None):
        with self.lock:
            prof = self.data["profiles"][str(profile)]
            if lighting:
                prof["lighting"] = _merge(prof["lighting"], lighting)
            if name is not None:
                prof["name"] = name.strip()[:40] or f"Profile {profile}"
            self._save()

    def update_layer(self, profile, layer, controls=None):
        """controls: {"knob": {...}} and/or {"slider": {...}}, each replaced as a whole."""
        with self.lock:
            if controls:
                self.data["profiles"][str(profile)]["layers"][str(layer)]["controls"].update(copy.deepcopy(controls))
            self._save()

    def set_binding(self, profile, layer, key, binding):
        """binding None = factory default."""
        with self.lock:
            keymap = self.data["profiles"][str(profile)]["layers"][str(layer)]["keymap"]
            if binding is None:
                keymap.pop(key, None)
            else:
                keymap[key] = binding
            self._save()

    def copy_layer(self, profile, source, target):
        """Overwrite layer `target` of a profile with a copy of layer `source`."""
        with self.lock:
            layers = self.data["profiles"][str(profile)]["layers"]
            layers[str(target)] = copy.deepcopy(layers[str(source)])
            self._save()

    # ---- backup / restore ----
    BACKUP_SECTIONS = ("active_profile", "profiles", "macros", "obs", "device", "voicemeeter", "battery_warning")

    def export(self):
        """Everything a user configured (not the server port), as a JSON-ready dict."""
        with self.lock:
            out = {k: copy.deepcopy(self.data[k]) for k in self.BACKUP_SECTIONS}
        return {"format": "gmmk-numpad-suite-backup", "version": 1, **out}

    def import_backup(self, backup):
        """Replace the configured sections with a backup (older backups are migrated like config files)."""
        if not isinstance(backup, dict) or backup.get("format") != "gmmk-numpad-suite-backup":
            raise ValueError("not a GMMK Numpad Suite backup file")
        saved = {k: copy.deepcopy(backup[k]) for k in self.BACKUP_SECTIONS if k in backup}
        if not isinstance(saved.get("profiles"), dict):
            raise ValueError("backup has no profiles")
        merged = _merge(DEFAULTS, _migrate(saved))
        with self.lock:
            for k in self.BACKUP_SECTIONS:
                self.data[k] = merged[k]
            self._save()

    # ---- resets ----
    def reset_layer(self, profile, layer):
        """Keys, knob and slider of one layer back to how the numpad behaves out of the box."""
        with self.lock:
            self.data["profiles"][str(profile)]["layers"][str(layer)] = copy.deepcopy(FACTORY_LAYER)
            self._save()

    def reset_lighting(self, profile):
        with self.lock:
            self.data["profiles"][str(profile)]["lighting"] = copy.deepcopy(DEFAULT_LIGHTING)
            self._save()

    def reset_all(self):
        """Every profile, layer and macro back to the suite's defaults (OBS, numpad and app settings stay)."""
        with self.lock:
            for key in ("active_profile", "profiles", "macros"):
                self.data[key] = copy.deepcopy(DEFAULTS[key])
            self._save()

    # ---- macros (global) ----
    def save_macro(self, macro_id, name, events):
        """macro_id None = new. Returns the ID (as a string)."""
        with self.lock:
            macros = self.data["macros"]
            if macro_id is None:
                macro_id = str(next(i for i in range(1, 256) if str(i) not in macros))
            macros[str(macro_id)] = {"name": name.strip()[:40] or f"Macro {macro_id}", "events": events}
            self._save()
            return str(macro_id)

    def delete_macro(self, macro_id):
        """Removes the macro and turns keys that played it back into their default."""
        with self.lock:
            self.data["macros"].pop(str(macro_id), None)
            for prof in self.data["profiles"].values():
                for lay in prof["layers"].values():
                    for key in [k for k, b in lay["keymap"].items()
                                if b.get("type") == "macro" and b.get("macro") == str(macro_id)]:
                        del lay["keymap"][key]
            self._save()

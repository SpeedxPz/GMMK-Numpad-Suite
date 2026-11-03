"""Key bindings: the config form <-> the device key table, and the suite's own PC actions.

A binding in the config is a small dict (a missing key = factory default):
  {"type": "key", "code": <HID usage>, "mods": ["ctrl", "shift", "alt", "win"]}
  {"type": "media", "action": "play_pause"}           (gmmk_numpad.CONSUMER_CODES)
  {"type": "function", "action": "profile_up"}        (gmmk_numpad.KB_FUNCTION_CODES)
  {"type": "macro", "macro": "<id>", "mode": "once"}  (gmmk_numpad.MACRO_MODES)
  {"type": "action", "action": "obs_record", ...}     host event, run by the suite (ACTIONS below)
  {"type": "disabled"}
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))
from gmmk_numpad import (BIND_CONSUMER, BIND_HOST_EVENT, BIND_KB_FUNCTION, BIND_KEYSTROKE,  # noqa: E402
                         BIND_MACRO, BIND_MODIFIERS, CONSUMER_CODES, KEY_SLOT, MACRO_MODES, MOD_BITS)

REMAPPABLE = [k for k in KEY_SLOT if k != "NumLock"]  # NumLock is the firmware's Fn key
# Keyboard functions (gmmk_numpad.KB_FUNCTION_CODES): profile and layer cycling, done by the numpad.
FUNCTIONS = {"profile_up": 2, "profile_down": 1, "layer_up": 4, "layer_down": 3}

# PC actions: the key sends a host event and the suite does the work. "param" names the extra field;
# "group" is the app the dashboard files it under (ACTION_GROUPS, in display order). "hold": the
# binding may set hold="on" | "off": the state while the key is held (muted / route on / button on =
# "on"), and the release sets the other one. hold="off" on a mute = push-to-talk, "on" = push-to-mute.
ACTION_GROUPS = {"pc": "PC", "media": "Media", "obs": "OBS Studio", "discord": "Discord", "voicemeeter": "VoiceMeeter"}
ACTIONS = {
    "launch": {"group": "pc", "label": "Open a program, file or URL", "param": "target"},
    "mic_mute": {"group": "pc", "label": "Mute / unmute mic (all apps)", "param": None, "hold": True},
    "battery": {"group": "pc", "label": "Show battery on the side bars", "param": None},
    "media_play_pause": {"group": "media", "label": "Play / pause", "param": None},
    "media_next": {"group": "media", "label": "Next track", "param": None},
    "media_prev": {"group": "media", "label": "Previous track", "param": None},
    "obs_record": {"group": "obs", "label": "Start / stop recording", "param": None},
    "obs_stream": {"group": "obs", "label": "Start / stop streaming", "param": None},
    "obs_pause": {"group": "obs", "label": "Pause / resume recording", "param": None},
    "obs_replay": {"group": "obs", "label": "Save replay buffer", "param": None},
    "obs_replay_toggle": {"group": "obs", "label": "Start / stop replay buffer", "param": None},
    "obs_scene": {"group": "obs", "label": "Select scene (preview in Studio Mode)", "param": "scene"},
    "obs_transition": {"group": "obs", "label": "Transition (preview → program)", "param": None},
    "obs_mute": {"group": "obs", "label": "Mute / unmute source", "param": "source", "hold": True},
    "discord_mute": {"group": "discord", "label": "Mute / unmute mic", "param": None, "hold": True},
    "discord_deafen": {"group": "discord", "label": "Deafen / undeafen", "param": None, "hold": True},
    # "vm" holds the VoiceMeeter target: "Strip[5]" / "Bus[0]", a route "Strip[5].A1", or a button id
    "vm_mute": {"group": "voicemeeter", "label": "Mute / unmute input or output", "param": "vm", "hold": True},
    "vm_route": {"group": "voicemeeter", "label": "Route input → output on / off", "param": "vm", "hold": True},
    "vm_macro_toggle": {"group": "voicemeeter", "label": "Macro Button: toggle on / off", "param": "vm", "hold": True},
    "vm_macro_press": {"group": "voicemeeter", "label": "Macro Button: press (click)", "param": "vm", "hold": True},
}

# Knob turning and slider, per profile. group = how the dashboard nests them (Windows / OBS /
# VoiceMeeter; an app group is also its config section, whose "enabled" switches it on).
# "windows" for the knob is the numpad's own volume keys; everything else needs knob event mode
# (cmd 0x09) and is done by the suite. For VoiceMeeter, "source" is "Strip[i]" / "Bus[i]".
# For "app_volume", "source" is a program's exe name (Spotify.exe) and step_db is a step in %.
KNOB_TYPES = {
    "windows": {"group": "windows", "label": "Windows volume", "params": []},
    "app_volume": {"group": "app", "label": "One program's volume", "params": ["source", "step_db"]},
    "obs_volume": {"group": "obs", "label": "Audio volume", "params": ["source", "step_db"]},
    "obs_scene": {"group": "obs", "label": "Switch scenes (next / previous; Preview in Studio Mode)", "params": []},
    "vm_gain": {"group": "voicemeeter", "label": "Input / output gain", "params": ["source", "step_db"]},
}
SLIDER_TYPES = {
    "none": {"group": "none", "label": "Not used", "params": []},
    "windows": {"group": "windows", "label": "Windows volume", "params": []},
    "app_volume": {"group": "app", "label": "One program's volume", "params": ["source"]},
    "obs_volume": {"group": "obs", "label": "Audio volume", "params": ["source"]},
    "obs_tbar": {"group": "obs", "label": "Scene transition (T-bar)", "params": []},
    "vm_gain": {"group": "voicemeeter", "label": "Input / output gain", "params": ["source"]},
}


def clean_control(kind, value):
    """Validate a knob/slider setting from the dashboard."""
    types = {"knob": KNOB_TYPES, "slider": SLIDER_TYPES}[kind]
    t = value.get("type")
    if t not in types:
        raise ValueError(f"bad {kind} type")
    out = {"type": t, "source": str(value.get("source") or "").strip()}
    if kind == "knob":
        out["step_db"] = max(0.5, min(10.0, float(value.get("step_db", 2.0))))
    return out


def knob_event_mode(controls, config):
    """True when the suite (not the numpad's volume keys) handles knob turns: the knob's app
    (KNOB_TYPES group = config section) is switched on."""
    t = controls["knob"]["type"]
    group = KNOB_TYPES[t]["group"]
    return t != "windows" and (group == "app" or config.get(group)["enabled"])  # "app" needs no setup


DEFAULT_KEYMAP = {
    "Subtract": {"type": "action", "action": "obs_record"},
    "Divide": {"type": "action", "action": "obs_stream"},
    "Multiply": {"type": "action", "action": "obs_pause"},
}


def clean(binding):
    """Validate a binding from the dashboard. Returns the stored form, or None for the default."""
    t = (binding or {}).get("type", "default")
    if t == "default":
        return None
    if t == "key":
        code = int(binding["code"])
        if not 0 < code < 256:
            raise ValueError("bad key code")
        mods = [m for m in MOD_BITS if m in binding.get("mods", [])]
        return {"type": "key", "code": code, "mods": mods}
    if t == "media" and binding.get("action") in CONSUMER_CODES:
        return {"type": "media", "action": binding["action"]}
    if t == "function" and binding.get("action") in FUNCTIONS:
        return {"type": "function", "action": binding["action"]}
    if t == "macro":
        mode = binding.get("mode", "once")
        if mode not in MACRO_MODES:
            raise ValueError("bad macro mode")
        return {"type": "macro", "macro": str(int(binding["macro"])), "mode": mode}
    if t == "action" and binding.get("action") in ACTIONS:
        out = {"type": "action", "action": binding["action"]}
        param = ACTIONS[binding["action"]]["param"]
        if param:
            out[param] = str(binding.get(param) or "").strip()
        hold = binding.get("hold")
        if hold in ("on", "off") and ACTIONS[binding["action"]].get("hold"):
            out["hold"] = hold
        return out
    if t == "disabled":
        return {"type": "disabled"}
    raise ValueError(f"bad binding: {binding}")


def to_device(binding, macros):
    """Binding -> (binding type, code) for the key table, or None for the factory default."""
    t = (binding or {}).get("type")
    if t == "key":
        bits = sum(MOD_BITS[m] for m in binding.get("mods", []))
        return (BIND_MODIFIERS | bits if bits else BIND_KEYSTROKE), binding["code"]
    if t == "media":
        return BIND_CONSUMER, CONSUMER_CODES[binding["action"]]
    if t == "function":
        return BIND_KB_FUNCTION, FUNCTIONS[binding["action"]]
    if t == "macro":
        return (BIND_MACRO, int(binding["macro"])) if binding["macro"] in macros else (0, 0)
    if t == "action":
        return BIND_HOST_EVENT, 0
    if t == "disabled":
        return 0, 0  # the type of the factory table's unused slots 17/18
    return None


def key_table(keymap, macros):
    """{key: (type, code)} overrides for Numpad.set_key_bindings."""
    out = {}
    for key, binding in keymap.items():
        entry = to_device(binding, macros) if key in REMAPPABLE else None
        if entry is not None:
            out[key] = entry
    return out


def macro_modes(keymap, macros):
    """{key: mode} for Numpad.set_macro_modes (playback mode is stored per key slot)."""
    return {k: b.get("mode", "once") for k, b in keymap.items()
            if k in REMAPPABLE and b.get("type") == "macro" and b["macro"] in macros}

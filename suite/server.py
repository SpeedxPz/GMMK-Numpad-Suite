"""Local dashboard: static page + small JSON API, bound to 127.0.0.1 only.

Key actions can launch programs, so writes are locked to the dashboard itself: the Host header must
be ours (DNS rebinding), and POSTs must be same-origin JSON (a cross-site form can't send that
without a CORS preflight, which this server never grants).
"""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))
from gmmk_numpad import CONSUMER_CODES, EFFECTS, KEY_LED, MACRO_MAX_EVENTS, SIDE_LEFT, SIDE_RIGHT  # noqa: E402

import autostart  # noqa: E402
import firmware  # noqa: E402
import keymap  # noqa: E402
from config import LAYERS, PROFILES  # noqa: E402

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
FONTS = {"inter-latin-wght-normal.woff2", "space-grotesk-latin-wght-normal.woff2"}  # same type system as StellarLog
# The suite's firmware add-on (fw/). The dashboard recommends it on stock firmware and on older
# add-on versions. Verified on hardware 2026-11-01: stock re-flash, add-on v2..v4 (.knowledge/).
FIRMWARE_ADDON = {
    "available": firmware.IMAGES_PRESENT,  # False in builds without the firmware images
    "images": firmware.IMAGES_PRESENT,
    "version": 4,
    "benefits": ["Instant OBS status lights, no missed key taps while they change",
                 "Commands can no longer overwrite each other (command queue)",
                 "Lighting changes no longer wear the numpad's flash"],
}
LIGHTING_KEYS = {"effect", "color", "speed", "brightness", "sidelights", "multicolor", "per_key", "layer_indicator"}
OBS_KEYS = {"enabled", "overlay"}


def clean_events(events):
    out = []
    for code, pressed, delay in events[:MACRO_MAX_EVENTS]:
        code, delay = int(code), int(delay)
        if not 0 < code < 256:
            raise ValueError("bad key code")
        out.append([code, bool(pressed), max(1, min(delay, 0x7FFF))])
    return out


def make_handler(app):
    hosts = {f"127.0.0.1:{app.port}", f"localhost:{app.port}"}
    origins = {f"http://{h}" for h in hosts}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep the console quiet
            pass

        def _json(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")

        def do_GET(self):
            if self.headers.get("Host") not in hosts:
                return self.send_error(403)
            if self.path in ("/", "/index.html"):
                with open(os.path.join(WEB_DIR, "index.html"), "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path.startswith("/fonts/") and self.path[7:] in FONTS:
                with open(os.path.join(WEB_DIR, "fonts", self.path[7:]), "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "font/woff2")
                self.send_header("Cache-Control", "max-age=86400")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/api/state":
                self._json(app.state())
            elif self.path == "/api/live":  # polled fast by the dashboard: slider preview
                self._json({"slider": app.controls.slider_pos})
            elif self.path == "/api/layout":
                self._json({"keys": KEY_LED, "side_left": SIDE_LEFT, "side_right": SIDE_RIGHT,
                            "effects": {str(k): v for k, v in EFFECTS.items()},
                            "remappable": keymap.REMAPPABLE, "knob_types": keymap.KNOB_TYPES,
                            "slider_types": keymap.SLIDER_TYPES, "media": list(CONSUMER_CODES),
                            "functions": list(keymap.FUNCTIONS), "actions": keymap.ACTIONS,
                            "action_groups": keymap.ACTION_GROUPS, "action_group_order": list(keymap.ACTION_GROUPS),
                            "action_order": list(keymap.ACTIONS),  # JSON key order is not kept
                            "macro_max_events": MACRO_MAX_EVENTS, "firmware_addon": FIRMWARE_ADDON})
            elif self.path == "/api/backup":
                body = json.dumps(app.config.export(), indent=2).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Disposition",
                                 'attachment; filename="gmmk-numpad-suite-backup.json"')
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/api/obs/lists":
                try:
                    self._json(app.obs.lists())
                except Exception as e:
                    self._json({"sources": [], "scenes": [], "error": f"OBS not reachable: {e}"})
            else:
                self.send_error(404)

        def do_POST(self):
            if (self.headers.get("Host") not in hosts or self.headers.get("Origin") not in origins
                    or not (self.headers.get("Content-Type") or "").startswith("application/json")):
                return self.send_error(403)
            try:
                data = self._body()
                self._post(data)
            except (ValueError, KeyError, TypeError) as e:
                self._json({"error": str(e)}, 400)

        def _post(self, data):
            p, l = app.config.active_slot()
            if self.path == "/api/lighting":  # per profile (all three layers)
                values = {k: v for k, v in data.items() if k in LIGHTING_KEYS}
                if "layer_indicator" in values:
                    ind = values["layer_indicator"]
                    values["layer_indicator"] = {"enabled": bool(ind.get("enabled")),
                                                 "color": [max(0, min(255, int(c))) for c in ind["color"]][:3]}
                app.config.update_profile(p, lighting=values)
                app.device.request_sync()
            elif self.path == "/api/keymap":  # per layer
                if data["key"] not in keymap.REMAPPABLE:
                    raise ValueError("this key can't be remapped")
                app.config.set_binding(p, l, data["key"], keymap.clean(data.get("binding")))
                app.device.request_sync()
                app.obs.wake.set()  # status lights follow the key map
            elif self.path == "/api/controls":  # {"knob": {...}} and/or {"slider": {...}}, per layer
                values = {k: keymap.clean_control(k, data[k]) for k in ("knob", "slider") if k in data}
                app.config.update_layer(p, l, controls=values)
                app.device.request_sync()  # knob mode is stored per slot on the numpad
            elif self.path == "/api/profile":
                if "name" in data:
                    app.config.update_profile(p, name=str(data["name"]))
                if "active" in data or "layer" in data:  # switch profile and/or layer
                    profile = int(data.get("active", p))
                    layer = int(data["layer"]) if "layer" in data else None
                    if profile not in PROFILES or (layer is not None and layer not in LAYERS):
                        raise ValueError("bad profile or layer")
                    app.device.request_slot(profile, layer)
                    app.obs.wake.set()
                if "copy_layer_from" in data:  # overwrite the active layer with another layer of this profile
                    source = int(data["copy_layer_from"])
                    if source not in LAYERS or source == l:
                        raise ValueError("bad layer to copy from")
                    app.config.copy_layer(p, source, l)
                    app.device.request_sync()
                    app.obs.wake.set()
            elif self.path == "/api/device":  # polling rate, key scan interval, lighting sleep
                values = {}
                if "polling_hz" in data:
                    if int(data["polling_hz"]) not in (1000, 500, 250, 125):
                        raise ValueError("polling rate must be 1000, 500, 250 or 125")
                    values["polling_hz"] = int(data["polling_hz"])
                if "scan_ms" in data:
                    values["scan_ms"] = max(2, min(8, int(data["scan_ms"])))
                if "led_sleep_min" in data:
                    values["led_sleep_min"] = max(0, min(254, int(data["led_sleep_min"])))
                app.config.update("device", values)
                app.device.request_sync()
            elif self.path == "/api/restore":  # body: a backup file's contents
                app.config.import_backup(data)
                app.device.request_slot(*app.config.active_slot())
                app.device.request_sync()
                app.obs.wake.set()
            elif self.path == "/api/reset":  # {"what": "layer" | "lighting" | "all"}
                what = data.get("what")
                if what == "layer":
                    app.config.reset_layer(p, l)
                elif what == "lighting":
                    app.config.reset_lighting(p)
                elif what == "all":
                    app.config.reset_all()
                    app.device.request_slot(*app.config.active_slot())
                else:
                    raise ValueError("reset what?")
                app.device.request_sync()
                app.obs.wake.set()
            elif self.path == "/api/firmware":  # {"action": "install" | "restore"}
                if not FIRMWARE_ADDON["available"]:
                    raise ValueError("firmware updates from the dashboard are not enabled yet "
                                     "(the add-on has not been verified on real hardware)")
                app.firmware.start(data.get("action"))
            elif self.path == "/api/resync":
                app.device.request_resync()
            elif self.path == "/api/macro":
                mid = data.get("id")
                if mid is not None and str(mid) not in app.config.get("macros"):
                    raise ValueError("no such macro")
                mid = app.config.save_macro(mid, str(data.get("name", "")), clean_events(data["events"]))
                app.device.request_sync()
                return self._json({**app.state(), "saved_macro": mid})
            elif self.path == "/api/macro/delete":
                app.config.delete_macro(data["id"])
                app.device.request_sync()
            elif self.path == "/api/obs":
                app.config.update("obs", {k: v for k, v in data.items() if k in OBS_KEYS})
                app.device.request_sync()  # knob mode follows OBS on/off
                app.obs.wake.set()
            elif self.path == "/api/discord":  # {enabled, client_id, client_secret, connect, forget}
                values = {}
                if "enabled" in data:
                    values["enabled"] = bool(data["enabled"])
                if "client_id" in data:
                    cid = str(data["client_id"]).strip()
                    if cid and not cid.isdigit():
                        raise ValueError("the client ID is a number (Developer Portal → OAuth2)")
                    values["client_id"] = cid
                if data.get("client_secret"):  # empty = keep the stored one
                    values["client_secret"] = str(data["client_secret"]).strip()
                old = app.config.get("discord")
                if any(k in values and values[k] != old[k] for k in ("client_id", "client_secret")):
                    # another app: its old token is useless
                    values.update(access_token="", refresh_token="", expires_at=0)
                app.config.update("discord", values)
                if data.get("forget"):
                    app.discord.forget()
                else:
                    app.discord.reconnect()
            elif self.path == "/api/voicemeeter":  # {enabled}
                app.config.update("voicemeeter", {"enabled": bool(data.get("enabled"))})
                app.device.request_sync()  # knob mode follows VoiceMeeter on/off
                app.voicemeeter.wake.set()
            elif self.path == "/api/battery_warning":  # {enabled, level}
                values = {}
                if "enabled" in data:
                    values["enabled"] = bool(data["enabled"])
                if "level" in data:
                    values["level"] = max(5, min(50, int(data["level"])))
                app.config.update("battery_warning", values)
            elif self.path == "/api/autostart":
                autostart.set_enabled(bool(data.get("enabled")))
            else:
                return self.send_error(404)
            self._json(app.state())

    return Handler


def serve(app):
    httpd = ThreadingHTTPServer(("127.0.0.1", app.port), make_handler(app))
    threading.Thread(target=httpd.serve_forever, daemon=True, name="http").start()
    return httpd

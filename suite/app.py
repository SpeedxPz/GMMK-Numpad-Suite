"""GMMK Numpad Suite: tray app + local web dashboard (lighting, key mapping, macros, profiles, OBS).

Run:  pythonw -I suite\\app.py      (or run_suite.bat). A second launch just opens the dashboard.
"""
import faulthandler
import os
import socket
import sys
import threading
import time
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)  # -I (isolated mode) does not add the script directory

import autostart  # noqa: E402
from config import CONFIG_DIR, PROFILES, Config  # noqa: E402
from controls import Controls  # noqa: E402
from firmware import FirmwareUpdater  # noqa: E402
from device import DeviceManager  # noqa: E402
from discord_link import DiscordLink  # noqa: E402
from voicemeeter_link import VoicemeeterLink  # noqa: E402
from pc_link import PcLink  # noqa: E402
from obs_link import ObsLink  # noqa: E402
import server  # noqa: E402

LOG_PATH = os.path.join(CONFIG_DIR, "suite.log")


class App:
    def __init__(self):
        self.config = Config()
        self.port = self.config.get("server")["port"]
        self.device = DeviceManager(self.config, log=self.log)
        self.obs = ObsLink(self.config, self.device, log=self.log)
        self.discord = DiscordLink(self.config, self.device, log=self.log)
        self.voicemeeter = VoicemeeterLink(self.config, self.device, log=self.log)
        self.pc = PcLink(self.config, self.device, log=self.log)
        self.controls = Controls(self.config, self.device, self.obs, self.discord, self.voicemeeter,
                                 self.pc, log=self.log)
        self.firmware = FirmwareUpdater(self.device, log=self.log)
        self.log_lines = []

    def log(self, msg):
        line = time.strftime("%H:%M:%S ") + msg
        self.log_lines = (self.log_lines + [line])[-200:]
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass

    def state(self):
        obs_cfg = self.config.get("obs")
        return {
            "device": dict(self.device.status),
            "active_profile": self.config.active(),
            "profiles": {str(p): self.config.profile(p)["name"] for p in PROFILES},
            "active_layer": self.config.active_slot()[1],
            "profile": {k: v for k, v in self.config.profile().items() if k != "layers"},
            "layer": self.config.layer(),
            "macros": self.config.get("macros"),
            "obs": {**obs_cfg, "connected": self.obs.connected, "state": self.obs.state, "scene": self.obs.scene, "preview": self.obs.preview,
                    "muted": self.obs.muted, "replay": self.obs.replay},
            "pc": {**(self.pc.view or {}), "apps": self.pc.apps},
            "discord": self.discord_state(),
            "voicemeeter": {**self.config.get("voicemeeter"), "status": self.voicemeeter.status,
                            "error": self.voicemeeter.error, "edition": self.voicemeeter.edition,
                            "lists": self.voicemeeter.lists, "view": self.voicemeeter.view},
            "device_settings": self.config.get("device"),
            "battery_warning": self.config.get("battery_warning"),
            "firmware_job": dict(self.firmware.job),
            "autostart": autostart.is_enabled(),
            "log": self.log_lines[-30:],
        }

    def discord_state(self):
        cfg = self.config.get("discord")  # the secret and tokens never go to the browser
        return {"enabled": cfg["enabled"], "client_id": cfg["client_id"], "has_secret": bool(cfg["client_secret"]),
                "authorized": bool(cfg["refresh_token"] or cfg["access_token"]),
                "status": self.discord.status, "error": self.discord.error, "voice": self.discord.voice}

    def url(self):
        return f"http://127.0.0.1:{self.port}/"


def already_running(port):
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def make_icon():
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((4, 4, 60, 60), 10, fill=(30, 30, 36, 255))
    for row in range(3):
        for col in range(3):
            c = (255, 40, 40, 255) if (row, col) == (0, 2) else (200, 200, 210, 255)
            d.rounded_rectangle((12 + col * 15, 12 + row * 15, 23 + col * 15, 23 + row * 15), 3, fill=c)
    return img


def main():
    # A crash in native code (COM, ctypes) ends the windowed exe without a word in suite.log: keep a
    # Python traceback of it in crash.log instead.
    os.makedirs(CONFIG_DIR, exist_ok=True)
    faulthandler.enable(open(os.path.join(CONFIG_DIR, "crash.log"), "a"))
    app = App()
    if already_running(app.port):
        webbrowser.open(app.url())
        return
    app.device.start()
    app.obs.start()
    app.discord.start()
    app.voicemeeter.start()
    app.pc.start()
    app.controls.start()
    server.serve(app)
    app.log(f"started, dashboard at {app.url()}")

    import pystray

    def toggle_obs(icon, item):
        app.config.update("obs", {"enabled": not app.config.get("obs")["enabled"]})
        app.device.request_sync()
        app.obs.wake.set()

    def toggle_autostart(icon, item):
        autostart.set_enabled(not autostart.is_enabled())

    icon = pystray.Icon("gmmk-numpad-suite", make_icon(), "GMMK Numpad Suite", menu=pystray.Menu(
        pystray.MenuItem("Open dashboard", lambda: webbrowser.open(app.url()), default=True),
        pystray.MenuItem("OBS integration", toggle_obs, checked=lambda item: app.config.get("obs")["enabled"]),
        pystray.MenuItem("Start with Windows", toggle_autostart, checked=lambda item: autostart.is_enabled()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", lambda icon: icon.stop()),
    ))
    if "--no-browser" not in sys.argv:
        threading.Timer(1.0, lambda: webbrowser.open(app.url())).start()
    icon.run()


if __name__ == "__main__":
    main()

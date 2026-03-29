"""Halo system tray + global hotkey"""

import threading
from typing import Callable, Optional

from PIL import Image, ImageDraw
from pynput import keyboard
import pystray

from modules.logger import get_logger, log_session_event


def _create_icon(color: str = "green", size: int = 64) -> Image.Image:
    """Generate a simple colored circle icon."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    colors = {
        "green": (0, 200, 80),
        "grey": (128, 128, 128),
        "red": (200, 50, 50),
        "yellow": (220, 180, 0),
    }
    rgb = colors.get(color, colors["grey"])
    draw.ellipse([4, 4, size - 4, size - 4], fill=rgb)
    # Draw 'H' in center
    try:
        draw.text((size // 2 - 6, size // 2 - 8), "H", fill="white")
    except Exception:
        pass
    return img


class HaloTray:
    def __init__(
        self,
        on_toggle: Callable[[], None],
        on_reindex: Callable[[], None] = None,
        on_quit: Callable[[], None] = None,
        hotkey: str = "ctrl+space",
    ):
        self.logger = get_logger()
        self._on_toggle = on_toggle
        self._on_reindex = on_reindex or (lambda: None)
        self._on_quit = on_quit or (lambda: None)
        self._hotkey = hotkey
        self._active = False
        self._icon: Optional[pystray.Icon] = None
        self._hotkey_listener: Optional[keyboard.GlobalHotKeys] = None
        self._vault_count = 0
        self._last_event = ""
        self._debug_mode = False

    @property
    def is_active(self) -> bool:
        return self._active

    def _toggle(self):
        self._active = not self._active
        log_session_event("toggle", "ON" if self._active else "OFF")
        self._update_icon()
        self._on_toggle()

    def _update_icon(self):
        if self._icon:
            color = "green" if self._active else "grey"
            self._icon.icon = _create_icon(color)
            status = "Listening" if self._active else "Off"
            self._icon.title = f"Halo -- {status} | {self._vault_count} notes | {self._last_event}"

    def update_status(self, vault_count: int = None, last_event: str = None):
        if vault_count is not None:
            self._vault_count = vault_count
        if last_event is not None:
            self._last_event = last_event
        self._update_icon()

    def _build_menu(self) -> pystray.Menu:
        return pystray.Menu(
            pystray.MenuItem(
                lambda _: "Stop Listening" if self._active else "Start Listening",
                lambda: self._toggle(),
                default=True,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Re-index Vault", lambda: self._on_reindex()),
            pystray.MenuItem(
                lambda _: "Debug: ON" if self._debug_mode else "Debug: OFF",
                lambda: self._toggle_debug(),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit Halo", lambda: self._quit()),
        )

    def _toggle_debug(self):
        self._debug_mode = not self._debug_mode
        log_session_event("debug_mode", "ON" if self._debug_mode else "OFF")

    def _quit(self):
        log_session_event("quit", "user requested")
        self._active = False
        self._on_quit()
        if self._icon:
            self._icon.stop()

    def _parse_hotkey(self) -> str:
        """Convert config hotkey format to pynput format."""
        # e.g. 'ctrl+space' -> '<ctrl>+<space>'
        parts = self._hotkey.lower().split("+")
        return "+".join(f"<{p.strip()}>" for p in parts)

    def start(self):
        # Global hotkey
        hotkey_str = self._parse_hotkey()
        try:
            self._hotkey_listener = keyboard.GlobalHotKeys({hotkey_str: self._toggle})
            self._hotkey_listener.daemon = True
            self._hotkey_listener.start()
            self.logger.info(f"TRAY:hotkey {self._hotkey} registered")
        except Exception as e:
            self.logger.error(f"TRAY:failed to register hotkey '{self._hotkey}': {e}")
            self.logger.error("TRAY:Halo will run without global hotkey. Use the tray menu instead.")

        # System tray icon
        self._icon = pystray.Icon(
            name="Halo",
            icon=_create_icon("grey"),
            title="Halo -- Off",
            menu=self._build_menu(),
        )
        log_session_event("tray_started")

        # Run tray in its own thread (blocking)
        tray_thread = threading.Thread(target=self._icon.run, daemon=True)
        tray_thread.start()
        self.logger.info("TRAY:system tray started")

    def stop(self):
        if self._hotkey_listener:
            try:
                self._hotkey_listener.stop()
            except Exception:
                pass
            self._hotkey_listener = None
        if self._icon:
            try:
                self._icon.stop()
            except Exception:
                pass
            self._icon = None
        self.logger.info("TRAY:stopped")

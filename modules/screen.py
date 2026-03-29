"""Halo screen capture -- periodic screenshots sent to Gemini Live as video frames"""

import threading
import time
from typing import Optional

import mss
from PIL import Image

from modules.config import Config
from modules.logger import get_logger

# Debug preview window title
PREVIEW_TITLE = "Halo Vision"


class ScreenCapture:
    """Captures desktop screenshots at a configurable interval."""

    def __init__(self, config: Config):
        self.config = config
        self.logger = get_logger()
        self._interval = config.screen_capture_interval
        self._target_width = config.get("screen_width", 768)
        self._jpeg_quality = config.get("screen_quality", 50)
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._latest: Optional[Image.Image] = None
        self._lock = threading.Lock()
        self._sct = None
        self._debug = config.debug
        self._preview_thread: Optional[threading.Thread] = None

    def capture(self) -> Image.Image:
        """Take a single screenshot, downscaled for Gemini."""
        if not self._sct:
            self._sct = mss.mss()

        monitor = self._sct.monitors[0]  # Full desktop (all monitors combined)
        raw = self._sct.grab(monitor)
        img = Image.frombytes("RGB", (raw.width, raw.height), raw.rgb)

        # Downscale to target width, preserving aspect ratio
        ratio = self._target_width / img.width
        new_h = int(img.height * ratio)
        img = img.resize((self._target_width, new_h), Image.LANCZOS)

        return img

    def get_latest(self) -> Optional[Image.Image]:
        """Get the most recent screenshot (thread-safe)."""
        with self._lock:
            return self._latest

    def _capture_loop(self):
        """Background loop that captures screenshots at the configured interval."""
        self.logger.info(f"SCREEN:capture loop started ({self._interval}s interval)")
        while self._running:
            try:
                img = self.capture()
                with self._lock:
                    self._latest = img
            except Exception as e:
                self.logger.warning(f"SCREEN:capture error: {e}")
            time.sleep(self._interval)

    def _preview_loop(self):
        """Debug preview -- shows a live OpenCV window of what Halo sees."""
        try:
            import cv2
            import numpy as np
        except ImportError:
            self.logger.warning("SCREEN:opencv not installed, skipping debug preview (pip install opencv-python)")
            return

        self.logger.info("SCREEN:debug preview window started")
        preview_width = self._target_width // 2
        while self._running:
            with self._lock:
                img = self._latest
            if img is not None:
                # Resize for preview
                ratio = preview_width / img.width
                preview = img.resize((preview_width, int(img.height * ratio)), Image.LANCZOS)
                # PIL RGB -> OpenCV BGR
                frame = cv2.cvtColor(np.array(preview), cv2.COLOR_RGB2BGR)
                cv2.imshow(PREVIEW_TITLE, frame)
            # 30ms wait -- also handles window events
            key = cv2.waitKey(30) & 0xFF
            if key == 27:  # ESC to close preview (does not stop Halo)
                break
        cv2.destroyWindow(PREVIEW_TITLE)
        self.logger.info("SCREEN:debug preview window closed")

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._thread.start()

        # Launch debug preview window if --debug
        if self._debug:
            self._preview_thread = threading.Thread(target=self._preview_loop, daemon=True)
            self._preview_thread.start()

        self.logger.info("SCREEN:started")

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        if self._preview_thread:
            self._preview_thread.join(timeout=3)
            self._preview_thread = None
        if self._sct:
            self._sct.close()
            self._sct = None
        self.logger.info("SCREEN:stopped")

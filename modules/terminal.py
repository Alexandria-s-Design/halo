"""Halo terminal monitor -- watches Claude Code CLI output across multiple sessions"""

import re
import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

from modules.config import Config
from modules.logger import get_logger

# Patterns that indicate useful output (keep these lines)
KEEP_PATTERNS = [
    re.compile(r"(?i)(error|exception|traceback|failed|fatal)", re.IGNORECASE),
    re.compile(r"(?i)(completed|done|success|finished|passed)", re.IGNORECASE),
    re.compile(r"(?i)(created|wrote|updated|deleted|modified)\s", re.IGNORECASE),
    re.compile(r"(?i)^(#\d+\.?\s)", re.IGNORECASE),  # Task list items
    re.compile(r"(?i)(tool result|tool call)", re.IGNORECASE),
    re.compile(r"(?i)(commit|push|pull|merge|branch)", re.IGNORECASE),
    re.compile(r"(?i)(test.*pass|test.*fail|tests?\s+\d+)", re.IGNORECASE),
    re.compile(r"(?i)(warning:)", re.IGNORECASE),
    re.compile(r"(?i)(installed|uninstalled|upgraded)", re.IGNORECASE),
    re.compile(r"^[\$\>]", re.MULTILINE),  # Shell prompts (commands being run)
]

# Patterns to always skip (noise)
SKIP_PATTERNS = [
    re.compile(r"^\s*$"),  # Empty lines
    re.compile(r"^[\s\u2502\u251c\u2514\u2500\u250c\u2510\u2518\u2524\u252c\u2534\u253c]+$"),  # Box drawing only
    re.compile(r"^\s*\.\.\.\s*$"),  # Ellipsis
    re.compile(r"^\x1b\["),  # Raw ANSI escape sequences only
    re.compile(r"^Downloading\s"),  # pip download progress
    re.compile(r"^\s+Using cached\s"),  # pip cache
    re.compile(r"^  "),  # Deeply indented (usually streaming token output)
]


class TerminalBuffer:
    """Rolling buffer for one terminal session."""

    def __init__(self, name: str, max_lines: int = 200):
        self.name = name
        self.max_lines = max_lines
        self._lines: deque = deque(maxlen=max_lines)
        self._raw_pos = 0  # File read position
        self.lock = threading.Lock()

    def add_lines(self, lines: list[str]):
        with self.lock:
            for line in lines:
                stripped = line.rstrip()
                if self._should_keep(stripped):
                    self._lines.append(stripped)

    def _should_keep(self, line: str) -> bool:
        # Skip noise first
        for pat in SKIP_PATTERNS:
            if pat.search(line):
                return False
        # Keep if matches useful pattern
        for pat in KEEP_PATTERNS:
            if pat.search(line):
                return True
        # Keep lines that look substantial (>20 chars, not just whitespace/symbols)
        clean = re.sub(r"\x1b\[[0-9;]*m", "", line)  # Strip ANSI
        return len(clean.strip()) > 20

    def get_lines(self) -> list[str]:
        with self.lock:
            return list(self._lines)

    def get_context(self, last_n: int = 50) -> str:
        lines = self.get_lines()
        recent = lines[-last_n:] if len(lines) > last_n else lines
        return "\n".join(recent)


class TerminalMonitor:
    """Monitors multiple Claude Code CLI log files."""

    def __init__(self, config: Config):
        self.config = config
        self.logger = get_logger()
        self.max_lines = config.max_terminal_lines
        self._buffers: dict[str, TerminalBuffer] = {}
        self._observer: Optional[Observer] = None
        self._log_dir: Optional[Path] = None
        self._lock = threading.Lock()
        self._poll_thread: Optional[threading.Thread] = None
        self._running = False

    def register_log(self, name: str, log_path: str) -> TerminalBuffer:
        buf = TerminalBuffer(name, self.max_lines)
        with self._lock:
            self._buffers[name] = buf
            buf._log_path = Path(log_path)
        self.logger.info(f"TERMINAL:registered '{name}' -> {log_path}")
        return buf

    def auto_discover_logs(self, log_dir: str = None):
        """Look for Claude Code session logs."""
        search_dirs = []
        if log_dir:
            search_dirs.append(Path(log_dir))

        # Common Claude Code log locations on Windows
        appdata = Path.home() / "AppData" / "Local"
        search_dirs.extend([
            appdata / "claude" / "logs",
            appdata / "claude-code" / "logs",
            Path.home() / ".claude" / "logs",
        ])

        # Also check for user-created log files in ~/.halo/terminal/
        terminal_dir = Path.home() / ".halo" / "terminal"
        terminal_dir.mkdir(parents=True, exist_ok=True)
        search_dirs.append(terminal_dir)

        for d in search_dirs:
            if d.exists():
                for f in sorted(d.glob("*.log"), key=lambda x: x.stat().st_mtime, reverse=True)[:6]:
                    name = f.stem
                    if name not in self._buffers:
                        self.register_log(name, str(f))

        self.logger.info(f"TERMINAL:discovered {len(self._buffers)} log sources")

    def _poll_files(self):
        """Poll registered log files for new content."""
        while self._running:
            with self._lock:
                buffers = dict(self._buffers)

            for name, buf in buffers.items():
                try:
                    path = getattr(buf, "_log_path", None)
                    if not path or not path.exists():
                        continue
                    size = path.stat().st_size
                    if size <= buf._raw_pos:
                        continue
                    with open(path, "r", encoding="utf-8", errors="ignore") as f:
                        f.seek(buf._raw_pos)
                        new_text = f.read()
                        buf._raw_pos = f.tell()
                    if new_text:
                        lines = new_text.splitlines()
                        buf.add_lines(lines)
                except Exception:
                    pass

            time.sleep(2)  # Poll every 2 seconds

    def start(self):
        if self._running:
            return
        self._running = True
        self._poll_thread = threading.Thread(target=self._poll_files, daemon=True)
        self._poll_thread.start()
        self.logger.info("TERMINAL:monitor started (polling mode)")

    def stop(self):
        self._running = False
        if self._poll_thread:
            self._poll_thread.join(timeout=5)
            self._poll_thread = None

    def get_terminal_context(self, last_n_per_tab: int = 30) -> str:
        """Get filtered terminal context from all tabs, combined."""
        with self._lock:
            buffers = dict(self._buffers)

        if not buffers:
            return "(no terminal sessions being monitored)"

        parts = []
        for name, buf in buffers.items():
            ctx = buf.get_context(last_n_per_tab)
            if ctx.strip():
                parts.append(f"=== Terminal: {name} ===\n{ctx}")

        return "\n\n".join(parts) if parts else "(no recent terminal output)"

    def inject_test_output(self, name: str, lines: list[str]):
        """For testing -- inject lines into a named buffer."""
        if name not in self._buffers:
            self._buffers[name] = TerminalBuffer(name, self.max_lines)
        self._buffers[name].add_lines(lines)

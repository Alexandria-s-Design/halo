"""Halo tools -- dispatcher model: read-only tools + async Claude Code delegation"""

import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from pynput.keyboard import Controller as KBController, Key

from modules.config import Config
from modules.vault import VaultIndexer
from modules.terminal import TerminalMonitor
from modules.ssh_client import SSHClient
from modules.logger import get_logger, log_tool_call, log_error

_kb = KBController()

# Maximum characters in a tool response sent back to Gemini
MAX_TOOL_RESPONSE_CHARS = 5000


def _clean_env_for_claude() -> dict:
    """Build a clean env for claude --print subprocess."""
    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("GOOGLE_API_KEY", None)
    env.pop("HALO_DEBUG", None)
    return env


def _truncate_result(result: str) -> str:
    """Cap tool results at MAX_TOOL_RESPONSE_CHARS before sending to Gemini."""
    if len(result) > MAX_TOOL_RESPONSE_CHARS:
        return result[:MAX_TOOL_RESPONSE_CHARS] + "\n...(truncated)"
    return result


def _send_telegram(message: str, bot_token: str, chat_id: str, parse_mode: str = None):
    """Send a message via Telegram Bot API. Only sends if bot_token and chat_id are set."""
    if not bot_token or not chat_id:
        return
    try:
        import urllib.request
        payload = {
            "chat_id": chat_id,
            "text": message[:4096],  # Telegram max
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        get_logger().error(f"TELEGRAM:send failed: {e}")


class ToolRegistry:
    def __init__(self, config: Config, vault: VaultIndexer, terminal: TerminalMonitor):
        self.config = config
        self.vault = vault
        self.terminal = terminal
        self.ssh = SSHClient(config)
        self.logger = get_logger()
        self._active_tasks = 0

    def handle_tool_call(self, tool_name: str, args: dict) -> str:
        start = time.time()
        try:
            result = self._dispatch(tool_name, args)
        except Exception as e:
            result = f"Error: {e}"
            log_error(f"tool:{tool_name}", e)

        # Truncate before sending back to Gemini
        result = _truncate_result(result)

        latency = (time.time() - start) * 1000
        log_tool_call(tool_name, args, str(result)[:500], latency)
        return result

    def _dispatch(self, name: str, args: dict) -> str:
        handlers = {
            # Direct (real-time)
            "open_app": self._open_app,
            # Dispatch (async)
            "dispatch_to_claude": self._dispatch_to_claude,
            # Read-only (sync)
            "search_vault": self._search_vault,
            "read_note": self._read_note,
            "get_terminal_context": self._get_terminal_context,
            "get_project_status": self._get_project_status,
            "get_claude_session": self._get_claude_session,
            "save_memory": self._save_memory,
            "recall_memory": self._recall_memory,
        }

        handler = handlers.get(name)
        if not handler:
            return f"Unknown tool: {name}"
        return handler(**args)

    # === DISPATCH (runs in background, Claude has full MCP access) ===

    def _dispatch_to_claude(self, task: str, **_) -> str:
        """Fire-and-forget: spawn Claude Code with full MCP tools in background."""
        if not self.config.claude_dispatch_enabled:
            return "Claude Code dispatch is disabled in config."

        # Check that Claude CLI is available
        claude_path = self._find_claude()
        if not claude_path or not shutil.which(claude_path):
            # Try the common Windows location
            npm_claude = Path.home() / "AppData" / "Roaming" / "npm" / "claude.cmd"
            if not npm_claude.exists():
                return "Error: Claude CLI not found. Install it with: npm install -g @anthropic-ai/claude-code"

        self._active_tasks += 1
        task_num = self._active_tasks

        # Enrich task with delivery instructions if notifications are configured
        enriched_task = task
        delivery_parts = []
        if self.config.telegram_bot_token and self.config.telegram_chat_id:
            delivery_parts.append(
                f"When done, send a summary to Telegram chat ID {self.config.telegram_chat_id} "
                f"using the Telegram MCP tool if available."
            )
        if self.config.notification_email:
            delivery_parts.append(
                f"Also send a summary via email to {self.config.notification_email} if email tools are available."
            )
        if delivery_parts:
            enriched_task += "\n\nDELIVERY INSTRUCTIONS:\n" + "\n".join(delivery_parts)

        # Notify via Telegram that task started
        if self.config.telegram_bot_token and self.config.telegram_chat_id:
            _send_telegram(
                f"Halo Task #{task_num} dispatched to Claude Code...\n\n{task[:400]}",
                self.config.telegram_bot_token,
                self.config.telegram_chat_id,
            )

        # Run in background thread
        thread = threading.Thread(
            target=self._run_claude_with_mcp,
            args=(enriched_task, task_num),
            daemon=True,
        )
        thread.start()

        return f"Task #{task_num} dispatched. Claude Code is working on it in the background."

    def _run_claude_with_mcp(self, task: str, task_num: int):
        """Background worker: run Claude Code with optional MCP config."""
        self.logger.info(f"DISPATCH:task #{task_num} started")

        claude_path = self._find_claude()
        cmd = [
            claude_path,
            "--print",
            "--output-format", "json",
            "--permission-mode", "bypassPermissions",
        ]

        # Add MCP config if specified
        if self.config.claude_mcp_config:
            mcp_path = os.path.expanduser(self.config.claude_mcp_config)
            if Path(mcp_path).exists():
                cmd.extend(["--strict-mcp-config", "--mcp-config", mcp_path])

        cmd.extend(["-p", task])

        try:
            result = subprocess.run(
                cmd,
                cwd=str(self.config.vault_path),
                capture_output=True,
                text=True,
                timeout=self.config.claude_dispatch_timeout,
                env=_clean_env_for_claude(),
            )

            # Parse JSON output
            output = result.stdout.strip()
            try:
                data = json.loads(output)
                result_text = data.get("result", output)
                is_error = data.get("is_error", False)
                duration = data.get("duration_ms", 0)
                self.logger.info(
                    f"DISPATCH:task #{task_num} done in {duration}ms, error={is_error}"
                )
                if is_error and self.config.telegram_bot_token:
                    _send_telegram(
                        f"Task #{task_num} completed with errors:\n{result_text[:1000]}",
                        self.config.telegram_bot_token,
                        self.config.telegram_chat_id,
                    )
            except json.JSONDecodeError:
                result_text = output

            if not result_text and self.config.telegram_bot_token:
                _send_telegram(
                    f"Task #{task_num}: Claude returned no output.",
                    self.config.telegram_bot_token,
                    self.config.telegram_chat_id,
                )

            self.logger.info(f"DISPATCH:task #{task_num} delivered")

        except subprocess.TimeoutExpired:
            timeout_min = self.config.claude_dispatch_timeout // 60
            if self.config.telegram_bot_token:
                _send_telegram(
                    f"Task #{task_num} timed out after {timeout_min} minutes.",
                    self.config.telegram_bot_token,
                    self.config.telegram_chat_id,
                )
            self.logger.warning(f"DISPATCH:task #{task_num} timed out")
        except FileNotFoundError:
            self.logger.error(f"DISPATCH:task #{task_num} failed -- Claude CLI not found")
        except Exception as e:
            if self.config.telegram_bot_token:
                _send_telegram(
                    f"Task #{task_num} failed: {e}",
                    self.config.telegram_bot_token,
                    self.config.telegram_chat_id,
                )
            log_error(f"dispatch:task_{task_num}", e)

    # === DIRECT (real-time) ===

    def _open_app(self, name: str, url: str = "", **_) -> str:
        """Open an app or URL. Handles Win+R timing automatically."""
        # Win+R to open Run dialog
        _kb.press(Key.cmd)
        _kb.press("r")
        _kb.release("r")
        _kb.release(Key.cmd)
        time.sleep(0.7)  # Wait for Run dialog

        # Type the command
        cmd = name
        if url:
            cmd = f"{name} {url}"
        for ch in cmd:
            _kb.type(ch)
            time.sleep(0.02)
        time.sleep(0.1)

        # Press Enter
        _kb.press(Key.enter)
        _kb.release(Key.enter)

        return f"Opened {cmd}"

    # === READ-ONLY TOOLS (sync, fast) ===

    def _search_vault(self, query: str, **_) -> str:
        parts = []

        # Check Halo's own memory first
        mem = _recall_memory(query)
        if mem and not mem.startswith("("):
            parts.append(f"=== Halo Memory ===\n{mem}")

        # Embedding search
        results = self.vault.search(query, top_k=5)
        if results:
            for r in results:
                # Skip SVG/binary-looking results
                if "<svg" in r["content"].lower() or "<path" in r["content"].lower():
                    continue
                preview = r["content"][:300].replace("\n", " ")
                parts.append(f"[{r['score']:.2f}] {r['path']}: {preview}")

        if not parts:
            return "No results found."
        return "\n\n".join(parts)

    def _read_note(self, path: str, **_) -> str:
        return self.vault.read_note(path)

    def _get_terminal_context(self, **_) -> str:
        return self.terminal.get_terminal_context()

    def _find_claude(self) -> str:
        claude = shutil.which("claude")
        if claude:
            return claude
        npm_claude = Path.home() / "AppData" / "Roaming" / "npm" / "claude.cmd"
        if npm_claude.exists():
            return str(npm_claude)
        return "claude"

    def _get_project_status(self, project_name: str, **_) -> str:
        projects_dir = self.config.vault_path / "01_Projects"

        # Try exact match first
        state_path = projects_dir / project_name / ".planning" / "STATE.md"
        if state_path.exists():
            return state_path.read_text(encoding="utf-8", errors="ignore")[:2000]
        alt_path = projects_dir / project_name / "STATE.md"
        if alt_path.exists():
            return alt_path.read_text(encoding="utf-8", errors="ignore")[:2000]

        # Fuzzy match: find directories containing the key words
        search_terms = set(
            w.lower() for w in project_name.replace("--", "-").split("-") if len(w) > 2
        )
        matches = []
        if projects_dir.exists():
            for d in projects_dir.iterdir():
                if not d.is_dir():
                    continue
                dir_lower = d.name.lower().replace("--", "-")
                dir_words = set(w for w in dir_lower.split("-") if len(w) > 2)
                overlap = search_terms & dir_words
                if overlap and len(overlap) >= len(search_terms) * 0.5:
                    matches.append((len(overlap), d))

        if matches:
            matches.sort(key=lambda x: x[0], reverse=True)
            best = matches[0][1]
            for state_file in [best / ".planning" / "STATE.md", best / "STATE.md"]:
                if state_file.exists():
                    return (
                        f"(matched project: {best.name})\n"
                        + state_file.read_text(encoding="utf-8", errors="ignore")[:2000]
                    )
            contents = [f.name for f in sorted(best.iterdir())[:20]]
            return (
                f"Found project '{best.name}' but no STATE.md.\n"
                f"Contents: {', '.join(contents)}"
            )

        return f"No project matching '{project_name}' found in 01_Projects/"

    def _get_claude_session(self, last_n: int = 20, **_) -> str:
        """Read the latest Claude Code conversation history."""
        return _read_claude_history(last_n)

    def _save_memory(self, fact: str, **_) -> str:
        """Save a fact to persistent memory."""
        return _save_memory(fact)

    def _recall_memory(self, query: str, **_) -> str:
        """Search persistent memory."""
        return _recall_memory(query)

    def close(self):
        self.ssh.close()


# === Claude Code Session Reader ===

_HISTORY_PATH = Path.home() / ".claude" / "history.jsonl"
_SESSIONS_DIR = Path.home() / ".claude" / "sessions"


def _read_claude_history(last_n: int = 20) -> str:
    """Read the last N entries from Claude Code history.jsonl."""
    if not _HISTORY_PATH.exists():
        return "(no Claude Code history found)"

    try:
        with open(_HISTORY_PATH, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()

        recent = lines[-last_n:] if len(lines) > last_n else lines
        entries = []
        for line in recent:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                display = d.get("display", "")
                sid = d.get("sessionId", "")[:8]
                if display:
                    if len(display) > 500:
                        display = display[:400] + "...(truncated)"
                    entries.append(f"[{sid}] {display}")
            except json.JSONDecodeError:
                continue

        if not entries:
            return "(no recent Claude Code conversation)"
        return "\n---\n".join(entries)
    except Exception as e:
        return f"Error reading Claude history: {e}"


def get_active_sessions() -> list[dict]:
    """List all active Claude Code sessions."""
    sessions = []
    if not _SESSIONS_DIR.exists():
        return sessions
    for f in _SESSIONS_DIR.glob("*.json"):
        try:
            d = json.load(open(f, encoding="utf-8"))
            sessions.append({
                "sessionId": d.get("sessionId", ""),
                "pid": d.get("pid", 0),
                "cwd": d.get("cwd", ""),
            })
        except Exception:
            pass
    return sessions


# === Persistent Memory ===

_MEMORY_PATH = Path.home() / ".halo" / "memory.md"


def _normalize_for_dedup(text: str) -> str:
    """Normalize text for deduplication: lowercase, strip articles, collapse whitespace."""
    text = text.strip().lower()
    # Remove common articles
    text = re.sub(r"\b(the|a|an)\b", "", text)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _load_memory() -> str:
    """Load persistent memory from disk."""
    if not _MEMORY_PATH.exists():
        return ""
    try:
        text = _MEMORY_PATH.read_text(encoding="utf-8", errors="ignore")
        # Cap at 2000 chars to save context tokens
        if len(text) > 2000:
            lines = text.strip().split("\n")
            # Keep the most recent entries (bottom of file)
            while len("\n".join(lines)) > 2000 and len(lines) > 5:
                lines.pop(0)
            text = "\n".join(lines)
        return text
    except Exception:
        return ""


def _save_memory(fact: str) -> str:
    """Append a fact to persistent memory."""
    _MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        timestamp = time.strftime("%Y-%m-%d %H:%M")
        entry = f"- [{timestamp}] {fact}\n"

        # Deduplicate with normalization
        normalized_new = _normalize_for_dedup(fact)
        if _MEMORY_PATH.exists():
            existing = _MEMORY_PATH.read_text(encoding="utf-8", errors="ignore")
            existing_facts = set()
            for line in existing.strip().split("\n"):
                if line.startswith("- ["):
                    bracket_end = line.find("] ", 3)
                    if bracket_end > 0:
                        existing_facts.add(_normalize_for_dedup(line[bracket_end + 2:]))
            if normalized_new in existing_facts:
                return f"Already in memory: {fact}"

        with open(_MEMORY_PATH, "a", encoding="utf-8") as f:
            f.write(entry)
        return f"Saved to memory: {fact}"
    except Exception as e:
        return f"Memory save error: {e}"


def _recall_memory(query: str) -> str:
    """Search persistent memory for entries matching a query."""
    if not _MEMORY_PATH.exists():
        return "(no memory saved yet)"
    try:
        text = _MEMORY_PATH.read_text(encoding="utf-8", errors="ignore")
        if not text.strip():
            return "(memory is empty)"

        # Simple keyword match -- return lines that contain any query word
        query_words = set(query.lower().split())
        matches = []
        for line in text.strip().split("\n"):
            line_lower = line.lower()
            if any(w in line_lower for w in query_words):
                matches.append(line)

        if matches:
            return "\n".join(matches[-20:])  # Last 20 matches
        return f"No memory entries matching '{query}'"
    except Exception as e:
        return f"Memory recall error: {e}"

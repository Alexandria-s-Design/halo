"""Halo configuration -- loads config.yaml + .env"""

import os
import sys
from pathlib import Path
from dotenv import load_dotenv
import yaml


class ConfigError(Exception):
    """Raised when configuration validation fails."""
    pass


class Config:
    def __init__(self, config_path: str = None):
        project_root = Path(__file__).parent.parent
        load_dotenv(project_root / ".env", override=True)

        config_path = config_path or str(project_root / "config.yaml")
        if not Path(config_path).exists():
            raise ConfigError(f"Config file not found: {config_path}")
        with open(config_path, "r") as f:
            self._data = yaml.safe_load(f) or {}

        # Core paths
        vault_path_str = self._data.get("vault_path", "")
        if vault_path_str:
            self.vault_path = Path(os.path.expanduser(vault_path_str))
        else:
            self.vault_path = Path.home() / "obsidian-vault"

        self.project_root = project_root
        self.log_dir = Path.home() / ".halo" / "logs"
        self.index_dir = Path.home() / ".halo" / "vault_index"

        # VPS/SSH (optional)
        self.vps_host = self._data.get("vps_host", "")
        self.vps_user = self._data.get("vps_user", "")

        # Voice
        self.voice = self._data.get("voice", "Kore")
        self.gemini_model = self._data.get("gemini_model", "gemini-3.1-flash-live-preview")

        # Screen
        self.screen_capture_interval = self._data.get("screen_capture_interval", 2)

        # Session
        self.hotkey = self._data.get("hotkey", "ctrl+space")
        self.max_terminal_lines = self._data.get("max_terminal_lines", 200)
        self.session_reset_minutes = self._data.get("session_reset_minutes", 120)
        self.max_reconnect_attempts = self._data.get("max_reconnect_attempts", 3)

        # Claude Code dispatch
        self.claude_dispatch_enabled = self._data.get("claude_dispatch_enabled", True)
        self.claude_dispatch_timeout = self._data.get("claude_dispatch_timeout", 300)
        self.claude_mcp_config = self._data.get("claude_mcp_config", "")

        # Context files (knowledge base loaded at session start)
        self.context_files = self._data.get("context_files", [])

        # Notifications (optional)
        self.telegram_bot_token_env = self._data.get("telegram_bot_token_env", "")
        self.telegram_chat_id_env = self._data.get("telegram_chat_id_env", "")
        self.notification_email = self._data.get("notification_email", "")

        # Debug
        self.debug = (
            self._data.get("debug", False)
            or os.environ.get("HALO_DEBUG", "").lower() == "true"
        )

        # Secrets from .env
        gemini_key_env = self._data.get("gemini_api_key_env", "GEMINI_API_KEY")
        self.gemini_api_key = os.environ.get(gemini_key_env, "") or os.environ.get("GOOGLE_API_KEY", "")

        # Telegram from env (if configured)
        self.telegram_bot_token = ""
        self.telegram_chat_id = ""
        if self.telegram_bot_token_env:
            self.telegram_bot_token = os.environ.get(self.telegram_bot_token_env, "")
        if self.telegram_chat_id_env:
            self.telegram_chat_id = os.environ.get(self.telegram_chat_id_env, "")

    def validate(self) -> list[str]:
        """Validate configuration. Returns list of error messages (empty = valid)."""
        errors = []

        if not self.gemini_api_key:
            errors.append("GEMINI_API_KEY is not set. Set it in .env or configure gemini_api_key_env in config.yaml.")

        if not self.vault_path.exists():
            errors.append(f"Vault path does not exist: {self.vault_path}")

        return errors

    def get(self, key: str, default=None):
        return self._data.get(key, default)

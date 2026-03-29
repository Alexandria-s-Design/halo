"""Halo SSH client -- paramiko connection to remote server"""

import os
from pathlib import Path

import paramiko

from modules.config import Config
from modules.logger import get_logger, log_error


class SSHClient:
    def __init__(self, config: Config):
        self.config = config
        self.logger = get_logger()
        self._client = None

    def _connect(self):
        if not self.config.vps_host:
            raise ConnectionError("No VPS host configured. Set vps_host in config.yaml.")

        if self._client:
            try:
                self._client.exec_command("echo ok", timeout=5)
                return  # Already connected
            except Exception:
                self._client = None

        self._client = paramiko.SSHClient()
        self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        # Try SSH key first, then agent
        key_path = Path.home() / ".ssh" / "id_rsa"
        key_ed = Path.home() / ".ssh" / "id_ed25519"

        # Also check SSH_KEY_PATH env var
        env_key = os.environ.get("SSH_KEY_PATH", "")
        if env_key:
            env_key_path = Path(os.path.expanduser(env_key))
            if env_key_path.exists():
                key_path = env_key_path

        kwargs = {
            "hostname": self.config.vps_host,
            "username": self.config.vps_user or "root",
            "timeout": 10,
        }

        if key_ed.exists():
            kwargs["key_filename"] = str(key_ed)
        elif key_path.exists():
            kwargs["key_filename"] = str(key_path)

        self._client.connect(**kwargs)
        self.logger.info(f"SSH:connected to {self.config.vps_host}")

    def run(self, command: str, timeout: int = 30) -> str:
        try:
            self._connect()
            _, stdout, stderr = self._client.exec_command(command, timeout=timeout)
            out = stdout.read().decode("utf-8", errors="replace")
            err = stderr.read().decode("utf-8", errors="replace")
            exit_code = stdout.channel.recv_exit_status()

            result = out
            if err:
                result += f"\nSTDERR: {err}"
            if exit_code != 0:
                result += f"\n(exit code: {exit_code})"

            return result.strip()
        except Exception as e:
            log_error("ssh_run", e)
            return f"SSH ERROR: {e}"

    def test_connection(self) -> bool:
        try:
            self._connect()
            result = self.run("echo vps_ok")
            return "vps_ok" in result
        except Exception:
            return False

    def close(self):
        if self._client:
            self._client.close()
            self._client = None

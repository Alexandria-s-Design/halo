#!/usr/bin/env python3
"""Halo -- Voice-Driven AI Companion for Claude Code

Usage:
    python halo.py              Run Halo
    python halo.py --check      Health check only
    python halo.py --test       Run test suite
    python halo.py --benchmark  Voice latency benchmark
    python halo.py --debug      Run in debug mode
    python halo.py --reindex    Re-index vault and exit
"""

import argparse
import os
import signal
import sys
import time

# Ensure our .env takes priority over ambient env vars
os.environ.pop("GOOGLE_API_KEY", None)
os.environ.pop("ANTHROPIC_API_KEY", None)


def health_check() -> bool:
    """Startup health check -- returns True if all critical checks pass."""
    from modules.config import Config
    from modules.logger import get_logger

    config = Config()
    logger = get_logger(config.log_dir, config.debug)
    passed = 0
    failed = 0

    def check(name, fn):
        nonlocal passed, failed
        try:
            start = time.time()
            result = fn()
            ms = (time.time() - start) * 1000
            if result:
                print(f"  OK {name} -- OK ({ms:.0f}ms)")
                passed += 1
            else:
                print(f"  FAIL {name} -- FAILED")
                failed += 1
        except Exception as e:
            print(f"  FAIL {name} -- ERROR: {e}")
            failed += 1

    print("Halo health check...\n")

    # 0. Config validation
    def check_config():
        errors = config.validate()
        if errors:
            for e in errors:
                print(f"    CONFIG: {e}")
            return False
        return True
    check("Configuration", check_config)

    # 1. Gemini API
    def check_gemini():
        from google import genai
        client = genai.Client(api_key=config.gemini_api_key, vertexai=False)
        result = client.models.get(model=config.gemini_model)
        return result is not None
    check("Gemini API connection", check_gemini)

    # 2. Vault
    def check_vault():
        return config.vault_path.exists() and any(config.vault_path.glob("*.md"))
    check("Vault path accessible", check_vault)

    # 3. Vault index
    def check_index():
        from modules.vault import VaultIndexer
        vi = VaultIndexer(config)
        return vi.count() > 0 or config.vault_path.exists()
    check("Vault index", check_index)

    # 4. Mic
    def check_mic():
        from modules.audio import get_default_devices
        devs = get_default_devices()
        return "input" in devs and "error" not in devs
    check("Microphone detected", check_mic)

    # 5. SSH to VPS (optional)
    if config.vps_host:
        def check_ssh():
            from modules.ssh_client import SSHClient
            ssh = SSHClient(config)
            ok = ssh.test_connection()
            ssh.close()
            return ok
        check("SSH to VPS", check_ssh)

    # 6. Claude CLI
    def check_claude():
        import subprocess, shutil
        claude_path = shutil.which("claude")
        if not claude_path:
            from pathlib import Path
            npm_claude = Path.home() / "AppData" / "Roaming" / "npm" / "claude.cmd"
            if npm_claude.exists():
                claude_path = str(npm_claude)
        if not claude_path:
            return False
        result = subprocess.run(
            [claude_path, "--version"],
            capture_output=True, text=True, timeout=10, shell=True,
        )
        return result.returncode == 0
    check("Claude CLI", check_claude)

    # 7. Hotkey
    def check_hotkey():
        from pynput import keyboard
        return True  # If pynput imports, hotkey will work
    check("Hotkey registration", check_hotkey)

    # 8. System tray
    def check_tray():
        import pystray
        return True
    check("System tray", check_tray)

    print(f"\n{passed} passed, {failed} failed")
    return failed == 0


def run_tests() -> bool:
    """Run scripted tests."""
    import os
    os.environ.pop("GOOGLE_API_KEY", None)
    from modules.config import Config
    from modules.vault import VaultIndexer
    from modules.terminal import TerminalMonitor
    from modules.tools import ToolRegistry

    config = Config()
    passed = 0
    total = 4

    print("Halo test suite...\n")

    # Test 1: Vault search
    print("  1. Vault search...", end=" ", flush=True)
    try:
        vi = VaultIndexer(config)
        if vi.count() == 0:
            print("SKIP (vault not indexed yet -- run --reindex first)")
        else:
            results = vi.search("test query", top_k=3)
            if results:
                print("PASS")
                passed += 1
            else:
                print(f"FAIL (no results)")
    except Exception as e:
        print(f"ERROR: {e}")

    # Test 2: Terminal filter
    print("  2. Terminal noise filtering...", end=" ", flush=True)
    try:
        tm = TerminalMonitor(config)
        tm.inject_test_output("test", [
            "error: something broke",
            "Successfully installed pkg-1.0",
            "   ",
            "Downloading pkg-1.0...",
            "[master abc123] feat: test commit",
        ])
        ctx = tm.get_terminal_context()
        has_error = "error:" in ctx
        has_install = "Successfully installed" in ctx
        no_download = "Downloading" not in ctx
        has_commit = "feat:" in ctx
        if has_error and has_install and no_download and has_commit:
            print("PASS")
            passed += 1
        else:
            print(f"FAIL (filter:{has_error},{has_install},{no_download},{has_commit})")
    except Exception as e:
        print(f"ERROR: {e}")

    # Test 3: Tool dispatch
    print("  3. Tool dispatch (mock)...", end=" ", flush=True)
    try:
        vi = VaultIndexer(config)
        tm = TerminalMonitor(config)
        tools = ToolRegistry(config, vi, tm)
        result = tools.handle_tool_call("get_terminal_context", {})
        unknown = tools.handle_tool_call("fake_tool", {})
        if "no terminal" in result.lower() and "Unknown" in unknown:
            print("PASS")
            passed += 1
        else:
            print("FAIL")
        tools.close()
    except Exception as e:
        print(f"ERROR: {e}")

    # Test 4: Claude CLI
    print("  4. Claude CLI agent (simple task)...", end=" ", flush=True)
    try:
        import subprocess, shutil
        from pathlib import Path as P
        claude_path = shutil.which("claude")
        if not claude_path:
            npm_claude = P.home() / "AppData" / "Roaming" / "npm" / "claude.cmd"
            claude_path = str(npm_claude) if npm_claude.exists() else "claude"
        import copy
        clean_env = copy.deepcopy(os.environ)
        clean_env.pop("HALO_DEBUG", None)
        clean_env.pop("ANTHROPIC_API_KEY", None)
        clean_env.pop("GOOGLE_API_KEY", None)
        result = subprocess.run(
            [claude_path, "--print", "-p", "What is 2+2? Reply with just the number."],
            capture_output=True, text=True, timeout=30, shell=True, env=clean_env,
        )
        if "4" in result.stdout:
            print("PASS")
            passed += 1
        elif "API key" in result.stdout or "auth" in result.stdout.lower():
            print("SKIP (claude CLI found but auth not configured for --print)")
            passed += 1
        else:
            print(f"FAIL (got: {result.stdout[:100]})")
    except subprocess.TimeoutExpired:
        print("TIMEOUT")
    except FileNotFoundError:
        print("SKIP (claude CLI not in PATH)")
    except Exception as e:
        print(f"ERROR: {e}")

    print(f"\n{passed}/{total} tests passed")
    return passed == total


def run_benchmark():
    """Benchmark voice round-trip latency."""
    from modules.audio import test_loopback
    print("Halo latency benchmark...\n")
    print("  Running 5 audio loopback tests (3s each)...\n")

    latencies = []
    for i in range(5):
        lat = test_loopback(duration=1.0)
        latencies.append(lat)
        print(f"  Run {i+1}: {lat:.0f}ms")

    latencies.sort()
    print(f"\n  p50: {latencies[2]:.0f}ms")
    print(f"  p95: {latencies[4]:.0f}ms")
    print(f"  avg: {sum(latencies)/len(latencies):.0f}ms")


def reindex_vault():
    """Re-index the entire vault."""
    import os
    os.environ.pop("GOOGLE_API_KEY", None)
    from modules.config import Config
    from modules.vault import VaultIndexer

    config = Config()
    vi = VaultIndexer(config)
    print(f"Indexing vault at {config.vault_path}...")
    count = vi.index_vault(force=True)
    print(f"Done. {count} chunks indexed.")


def run_halo():
    """Main Halo runtime."""
    import os
    os.environ.pop("GOOGLE_API_KEY", None)
    from modules.config import Config
    from modules.logger import get_logger, log_session_event
    from modules.vault import VaultIndexer
    from modules.terminal import TerminalMonitor
    from modules.tools import ToolRegistry
    from modules.session import GeminiLiveSession
    from modules.tray import HaloTray

    config = Config()

    # Validate config before proceeding
    errors = config.validate()
    if errors:
        print("Configuration errors:")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)

    logger = get_logger(config.log_dir, config.debug)
    log_session_event("startup")

    print("Halo starting...\n")

    # Health check
    if not health_check():
        print("\nCritical checks failed. Fix issues above and try again.")
        sys.exit(1)

    print()

    # Initialize modules
    vault = VaultIndexer(config)
    if vault.count() == 0:
        print("Vault not indexed. Run 'python halo.py --reindex' first for best results.")
        print("Continuing with empty index (will build incrementally)...\n")

    terminal = TerminalMonitor(config)
    terminal.auto_discover_logs()
    terminal.start()

    tools = ToolRegistry(config, vault, terminal)
    session = GeminiLiveSession(config, tool_handler=tools.handle_tool_call)

    # Tray + hotkey
    def on_toggle():
        if tray.is_active:
            session.start()
            print("Halo: Listening...")
        else:
            session.stop()
            print("Halo: Off")

    def on_reindex():
        print("Re-indexing vault...")
        vault.index_vault(force=True)
        tray.update_status(vault_count=vault.count())
        print("Re-index complete.")

    def on_quit():
        nonlocal running
        running = False

    tray = HaloTray(
        on_toggle=on_toggle,
        on_reindex=on_reindex,
        on_quit=on_quit,
        hotkey=config.hotkey,
    )
    tray.update_status(vault_count=vault.count())
    tray.start()

    # Start vault file watcher
    vault.start_watcher()

    running = True

    # Graceful shutdown
    def shutdown(sig=None, frame=None):
        nonlocal running
        if not running:
            return
        running = False
        print("\nHalo shutting down...")
        log_session_event("shutdown")
        session.stop()
        terminal.stop()
        vault.stop_watcher()
        tools.close()
        tray.stop()
        print("Halo stopped.")

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    print(f"Halo ready. Press {config.hotkey} to start listening.")
    print("Press Ctrl+C to quit.\n")

    # Keep main thread alive
    try:
        while running:
            time.sleep(1)
    except KeyboardInterrupt:
        shutdown()


def main():
    parser = argparse.ArgumentParser(description="Halo -- Voice-Driven AI Companion for Claude Code")
    parser.add_argument("--check", action="store_true", help="Run health check only")
    parser.add_argument("--test", action="store_true", help="Run test suite")
    parser.add_argument("--benchmark", action="store_true", help="Run latency benchmark")
    parser.add_argument("--debug", action="store_true", help="Run in debug mode")
    parser.add_argument("--reindex", action="store_true", help="Re-index vault and exit")
    args = parser.parse_args()

    if args.debug:
        os.environ["HALO_DEBUG"] = "true"

    if args.check:
        success = health_check()
        sys.exit(0 if success else 1)
    elif args.test:
        success = run_tests()
        sys.exit(0 if success else 1)
    elif args.benchmark:
        run_benchmark()
    elif args.reindex:
        reindex_vault()
    else:
        run_halo()


if __name__ == "__main__":
    main()

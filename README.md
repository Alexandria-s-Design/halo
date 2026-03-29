# Halo -- Voice-Driven AI Companion for Claude Code

A real-time voice AI companion that uses Gemini 2.0 Flash Live for bidirectional audio conversation. Halo can see your screen, dispatch tasks to Claude Code, search your Obsidian vault via RAG, and remember things across sessions. It runs as a system tray app on Windows with a global hotkey.

## Features

- **Real-time voice conversation** -- Bidirectional audio via Gemini Live API with sub-second latency
- **Screen awareness** -- Periodic screenshots let Halo see what you are working on
- **Claude Code dispatch** -- Delegate complex tasks (writing docs, sending emails, building files) to Claude Code running in the background with full MCP tool access
- **Obsidian vault RAG** -- Semantic search across your entire vault using Gemini embeddings + ChromaDB
- **Persistent memory** -- Halo remembers facts, preferences, and decisions across sessions
- **Claude Code context** -- Tails your active Claude Code session so Halo knows what you and Claude are working on
- **System tray integration** -- Runs quietly in the tray with a global hotkey toggle
- **SSH dispatch** -- Run commands on a remote server via paramiko
- **Auto-reindex** -- Watches your vault for changes and re-indexes automatically
- **Configurable everything** -- Voice, hotkey, screen capture interval, context files, notifications

## Architecture

```
YOU (voice) -----> Ctrl+Space hotkey
                        |
              GEMINI 2.0 FLASH LIVE
         (WebSocket bidiGenerateContent)

Inputs:  your voice (VAD) + screen captures + vault RAG + Claude Code context
Outputs: voice response + tool calls

Tool calls dispatch to:
  +------------------+-------------------------------------------+
  | Tool             | What it does                              |
  +------------------+-------------------------------------------+
  | open_app         | Open any Windows app or URL via Win+R     |
  | dispatch_to_claude | Spawn Claude Code for background tasks  |
  | search_vault     | Semantic search across Obsidian vault     |
  | read_note        | Read full content of a vault note         |
  | get_terminal_context | Filtered Claude Code terminal output  |
  | get_project_status | Read .planning/STATE.md for a project   |
  | get_claude_session | Read latest Claude Code conversation    |
  | save_memory      | Save a fact to persistent memory          |
  | recall_memory    | Search persistent memory by keywords      |
  +------------------+-------------------------------------------+
```

## Requirements

- Python 3.11+
- Windows 10 or 11
- A Gemini API key (free from [aistudio.google.com/apikey](https://aistudio.google.com/apikey))
- A working microphone and speakers
- Claude Code CLI installed and authenticated (for dispatch feature)
- An Obsidian vault (or any folder of Markdown files)

## Quick Start

```bash
# 1. Clone the repository
git clone https://github.com/your-org/halo.git
cd halo

# 2. Create a virtual environment
python -m venv .venv
source .venv/Scripts/activate   # Git Bash
# or: .venv\Scripts\activate    # PowerShell

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure
cp .env.example .env
# Edit .env -- set your GEMINI_API_KEY
cp config.example.yaml config.yaml
# Edit config.yaml -- set your vault_path at minimum

# 5. Run
python halo.py --check    # Verify everything works
python halo.py --reindex  # Index your vault (first time only)
python halo.py            # Launch Halo
```

## Usage

```bash
python halo.py              # Normal mode (system tray)
python halo.py --check      # Health check only
python halo.py --test       # Run test suite
python halo.py --benchmark  # Voice latency benchmark
python halo.py --debug      # Debug mode (verbose logs + preview window)
python halo.py --reindex    # Re-index vault and exit
```

### Controls

| Control | Action |
|---------|--------|
| Ctrl+Space | Toggle listening on/off (global hotkey, configurable) |
| System tray | Right-click for menu (Start/Stop, Re-index, Debug, Quit) |
| Ctrl+C | Graceful shutdown |
| ESC | Close debug preview window (does not stop Halo) |

### Voice Examples

Halo responds to natural speech:
- "Search my vault for project status updates"
- "What are we working on?" (references active Claude Code session)
- "Open VS Code"
- "Build me a Python script that parses CSV files" (dispatches to Claude Code)
- "Remember that the deadline is next Friday"

## Configuration Reference

### config.yaml

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| vault_path | string | ~/obsidian-vault | Path to your Obsidian vault |
| gemini_api_key_env | string | GEMINI_API_KEY | Env var name for Gemini API key |
| voice | string | Kore | Gemini voice (see Voice Options below) |
| gemini_model | string | gemini-2.0-flash-live-001 | Gemini model for Live API |
| screen_capture_interval | int | 2 | Seconds between screenshots |
| screen_width | int | 768 | Screenshot width in pixels |
| screen_quality | int | 50 | JPEG quality (1-100) |
| session_reset_minutes | int | 120 | Minutes before auto context reset |
| max_reconnect_attempts | int | 3 | Max reconnect tries on failure |
| hotkey | string | ctrl+space | Global hotkey to toggle listening |
| claude_dispatch_enabled | bool | true | Enable Claude Code dispatch tool |
| claude_dispatch_timeout | int | 300 | Max seconds per dispatched task |
| claude_mcp_config | string | "" | Path to MCP config JSON for dispatch |
| context_files | list | [] | Files to load into system prompt |
| telegram_bot_token_env | string | "" | Env var for Telegram bot token |
| telegram_chat_id_env | string | "" | Env var for Telegram chat ID |
| notification_email | string | "" | Email for task notifications |
| vps_host | string | "" | Remote server hostname for SSH |
| vps_user | string | "" | SSH username |
| max_terminal_lines | int | 200 | Max lines per terminal buffer |
| debug | bool | false | Enable debug mode |

### Voice Options

| Voice | Description |
|-------|-------------|
| Kore | Clear, neutral female voice |
| Aoede | Warm, expressive female voice |
| Puck | Energetic, youthful voice |
| Charon | Deep, authoritative male voice |
| Fenrir | Strong, confident male voice |

## Claude Code Integration

When you ask Halo to do something complex ("write a proposal", "deploy the app", "send an email"), it dispatches the task to Claude Code running in the background.

### How dispatch works

1. You speak a request to Halo
2. Halo recognizes it needs Claude Code and calls `dispatch_to_claude`
3. A background thread spawns `claude --print` with your task
4. Claude Code runs with full access to your tools (optionally via MCP config)
5. Results are optionally sent to Telegram and/or email
6. Halo confirms the task was dispatched

### MCP configuration

To give Claude Code access to external tools during dispatch, create an MCP config JSON:

```json
{
  "mcpServers": {
    "playwright": {
      "command": "npx",
      "args": ["@playwright/mcp@latest", "--isolated"]
    }
  }
}
```

Set `claude_mcp_config` in config.yaml to point to this file.

## Vault RAG

Halo indexes your Obsidian vault using Gemini embeddings and ChromaDB for fast semantic search.

### Setup

1. Set `vault_path` in config.yaml to your vault location
2. Run `python halo.py --reindex` to build the initial index
3. Halo watches for file changes and re-indexes automatically

### What gets indexed

- .md, .txt, .yaml, .yml, .json, .csv files
- Files under 500KB
- Hidden directories and node_modules are skipped

### Context files

For files you want Halo to always have loaded (not just searchable), add them to `context_files` in config.yaml:

```yaml
context_files:
  - MEMORY.md
  - projects/status.md
  - ~/.claude/projects/my-project/memory/MEMORY.md
```

These are loaded into the system prompt at session start and do not require searching.

## Troubleshooting

### "GEMINI_API_KEY is not set"
Make sure your `.env` file contains `GEMINI_API_KEY=your_key_here` and the file is in the halo directory.

### "Vault path does not exist"
Update `vault_path` in config.yaml to point to your actual Obsidian vault or Markdown folder.

### No audio / microphone not detected
- Run `python -c "import sounddevice; print(sounddevice.query_devices())"` to check available devices
- On Windows, sounddevice ships with PortAudio. If you get errors: `pip install sounddevice --force-reinstall`

### Hotkey not working
- Some applications capture Ctrl+Space (e.g., input method editors). Try a different hotkey in config.yaml.
- Run with `--debug` to check if the hotkey registration logged an error.

### Claude Code dispatch fails
- Verify Claude CLI is installed: `claude --version`
- Verify authentication: `claude --print -p "hello"`
- Check that the MCP config path (if set) points to a valid JSON file

### ChromaDB errors on startup
Halo will attempt to auto-repair a corrupted ChromaDB index. If it fails, delete `~/.halo/vault_index/` and run `--reindex`.

### Debug preview window
Run with `--debug` to see a live preview of what Halo sees (requires `opencv-python`):
```bash
pip install opencv-python
python halo.py --debug
```

## Contributing

Contributions are welcome. Please:

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Run `python halo.py --check` and `python halo.py --test` to verify
5. Submit a pull request

## License

MIT License. See [LICENSE](LICENSE) for details.

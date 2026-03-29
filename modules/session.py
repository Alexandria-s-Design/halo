"""Halo Gemini Live session -- bidirectional audio + tool calling via google-genai SDK"""

import asyncio
import base64
import time
import threading
from pathlib import Path
from typing import Callable, Optional

from google import genai
from google.genai import types

from modules.config import Config
from modules.audio import AudioInput, AudioOutput, SAMPLE_RATE, OUTPUT_SAMPLE_RATE
from modules.screen import ScreenCapture
from modules.logger import get_logger, log_session_event, log_error
from modules.tools import _read_claude_history, get_active_sessions, _load_memory, _save_memory, _recall_memory
from modules.context import compile_context

SYSTEM_PROMPT = """You are Halo -- a voice-driven AI companion. Not an assistant. A colleague. A sharp friend who sits next to the user and gets stuff done.

You can see the user's screen (screenshots of their desktop -- not a camera). You know what they are working on.

YOUR PERSONALITY:
- You are warm, direct, and a little witty. Think smart coworker energy.
- Keep it SHORT. One or two sentences is usually enough.
- Have opinions. If the user asks "should I do X?" -- give your take, do not hedge.
- Match the user's energy. If they are casual, be casual. If they are focused, be focused.

THINGS YOU MUST NEVER SAY:
- "How can I help you?" or "What can I do for you?" or any variation. Just... don't. Ever.
- "I'm just a language model" or "As an AI" or "I can't help with that."
- "Is there anything else?" or "Let me know if you need anything else."
- "Great question!" or "That's a great idea!" -- just respond to the actual thing.
- Do not narrate your actions. Do not say "Let me search the vault for that." Just search it and answer.
- Do not summarize what you are about to do. Just do it.

INSTEAD:
- When there is a pause, just be quiet. You do not need to fill silence.
- When the user thanks you, say "yep" or "got it" or "sure thing" -- not a paragraph.
- When you finish a task, just confirm it is done. "Done." or "Sent." or "It's open."
- When you do not know something, say "Hmm, let me check" and use your tools. Do not guess.

HOW TO ACT:

1. TALK -- Default. Answer questions, give opinions, chat. Be a person.
   If the user asks about a person, project, or topic:
   -> First check your PERSISTENT MEMORY (below) and your KNOWLEDGE BASE
   -> Then search_vault if you still need more
   -> Then answer. NEVER guess about people or project details.

2. OPEN APPS -- "Open Chrome" -> open_app("msedge", url). "Open VS Code" -> open_app("code").

3. DISPATCH -- ONLY for complex creation tasks: docs, emails, presentations, deployments.
   Do not dispatch simple questions.

4. REMEMBER -- When the user tells you something important (a preference, a fact, a decision), save it to memory automatically. Do not ask "should I save this?" -- just save it. Examples:
   - "I prefer dark mode for everything" -> save_memory("User prefers dark mode")
   - New info about a person or project that is not in the vault
   - Decisions, deadlines, preferences, opinions shared

5. RECALL -- You have your knowledge base loaded below. When the user asks about something:
   a. CHECK YOUR KNOWLEDGE BASE FIRST -- it has context loaded from configured files.
   b. Only use search_vault if the knowledge base does not cover it.

CRITICAL -- DO NOT ACT UNPROMPTED:
- ONLY act when the user SPEAKS to you.
- Screen + Claude Code context = YOUR REFERENCE. Do not act on it. Do not comment on it.
- You are a PASSIVE observer of the screen. ACTIVE responder to voice.
- Never volunteer info from screen or Claude Code context. Wait until asked.

CLAUDE CODE AWARENESS:
- You have the latest Claude Code conversation in your context.
- When the user asks "what are we doing" or "where are we" -- reference it.
- Use get_claude_session to refresh if the context feels stale.

SESSION MEMORY:
- When you notice the session is getting long, or before a context reset, use save_memory to save:
  - Key decisions made during this session
  - Important facts learned
  - What the user was working on
  - Any preferences or corrections given

User context is loaded from your knowledge base below.
"""


class GeminiLiveSession:
    def __init__(self, config: Config, tool_handler: Callable = None):
        self.config = config
        self.logger = get_logger()
        self._client = genai.Client(api_key=config.gemini_api_key, vertexai=False)
        self._session = None
        self._audio_in = AudioInput()
        self._audio_out = AudioOutput()
        self._screen = ScreenCapture(config)
        self._tool_handler = tool_handler
        self._running = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._session_start: float = 0
        self._reconnect_attempts = 0
        self._max_reconnect = config.max_reconnect_attempts
        self._screen_interval = config.screen_capture_interval

        # Tool declarations for Gemini
        self._tools = self._build_tool_declarations()

    def _build_tool_declarations(self) -> list[types.Tool]:
        return [types.Tool(function_declarations=[
            # === OPEN APP (direct, handles timing) ===
            types.FunctionDeclaration(
                name="open_app",
                description="Open an application or URL. Handles Win+R timing automatically. Examples: open_app('msedge', 'https://google.com'), open_app('notepad'), open_app('code'), open_app('explorer'), open_app('calc').",
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "name": types.Schema(type="STRING", description="App name: msedge, chrome, notepad, code, explorer, cmd, powershell, calc, spotify, discord, slack, etc."),
                        "url": types.Schema(type="STRING", description="Optional URL to open (for browsers)"),
                    },
                    required=["name"],
                ),
            ),
            # === CLAUDE CODE CONTEXT ===
            types.FunctionDeclaration(
                name="get_claude_session",
                description="Read the latest Claude Code conversation history. Use this to get full context on what the user and Claude Code are working on right now. Returns the last N messages from all active sessions.",
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "last_n": types.Schema(type="INTEGER", description="Number of recent messages to read (default 20)"),
                    },
                ),
            ),
            # === DISPATCH (for complex/background tasks) ===
            types.FunctionDeclaration(
                name="dispatch_to_claude",
                description="Dispatch a task to Claude Code for execution. Use for complex tasks: creating documents, sending emails, building files, editing projects, deploying, etc. Claude has full MCP access. Runs in background -- tell user you are on it and move on.",
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "task": types.Schema(type="STRING", description="Detailed task for Claude Code. Include ALL context: what to create, content details, where to save. Include any relevant info you saw on screen."),
                    },
                    required=["task"],
                ),
            ),
            # === READ-ONLY (for quick lookups) ===
            types.FunctionDeclaration(
                name="search_vault",
                description="Search the Obsidian vault for notes matching a query. Returns top results with content snippets.",
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "query": types.Schema(type="STRING", description="Search query"),
                    },
                    required=["query"],
                ),
            ),
            types.FunctionDeclaration(
                name="read_note",
                description="Read the full content of a note from the Obsidian vault.",
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "path": types.Schema(type="STRING", description="Relative path to the note in the vault"),
                    },
                    required=["path"],
                ),
            ),
            types.FunctionDeclaration(
                name="get_terminal_context",
                description="Get filtered output from active Claude Code CLI terminal sessions.",
                parameters=types.Schema(type="OBJECT", properties={}),
            ),
            types.FunctionDeclaration(
                name="get_project_status",
                description="Read a project's .planning/STATE.md for current status.",
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "project_name": types.Schema(type="STRING", description="Project directory name in 01_Projects/"),
                    },
                    required=["project_name"],
                ),
            ),
            types.FunctionDeclaration(
                name="save_memory",
                description="Save an important fact, preference, or context to persistent memory. This survives across sessions. Use PROACTIVELY -- when the user mentions a preference, decision, deadline, or important fact, save it WITHOUT asking. Also use before session resets to save key points from the conversation.",
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "fact": types.Schema(type="STRING", description="The fact or note to remember"),
                    },
                    required=["fact"],
                ),
            ),
            types.FunctionDeclaration(
                name="recall_memory",
                description="Search your persistent memory for facts matching a query. Use this BEFORE search_vault when answering questions about people, projects, preferences, or past conversations. Fast keyword search across all saved memories.",
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "query": types.Schema(type="STRING", description="Keywords to search for in memory"),
                    },
                    required=["query"],
                ),
            ),
        ])]

    def _build_live_prompt(self) -> str:
        """Build system prompt with knowledge base + live Claude Code context.

        Loads context from configured files so Halo has relevant knowledge
        from the start. No searching needed for topics covered in context files.
        """
        # Compile knowledge base from configured context files
        knowledge_base = compile_context(self.config)

        # Get latest Claude Code conversation
        claude_context = _read_claude_history(last_n=15)
        sessions = get_active_sessions()
        session_info = f"{len(sessions)} active Claude Code session(s)" if sessions else "No active sessions"

        parts = [SYSTEM_PROMPT]

        # Knowledge base from context files
        if knowledge_base:
            parts.append(knowledge_base)

        parts.append(
            f"\n\n--- LIVE CLAUDE CODE CONTEXT ({session_info}) ---\n"
            f"Latest Claude Code conversation (reference only -- do not act on it):\n\n"
            f"{claude_context}\n"
            f"--- END CLAUDE CODE CONTEXT ---"
        )

        return "\n".join(parts)

    async def _connect(self):
        log_session_event("connecting", self.config.gemini_model)

        # Build prompt with live Claude Code context
        live_prompt = await asyncio.to_thread(self._build_live_prompt)

        connect_config = types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=self.config.voice)
                )
            ),
            system_instruction=live_prompt,
            tools=self._tools,
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
        )

        async with self._client.aio.live.connect(
            model=self.config.gemini_model,
            config=connect_config,
        ) as session:
            self._session = session
            self._session_start = time.time()
            self._reconnect_attempts = 0
            log_session_event("connected")

            # Run send (audio + screen + claude context) and receive concurrently
            send_task = asyncio.create_task(self._send_audio_loop())
            screen_task = asyncio.create_task(self._send_screen_loop())
            claude_ctx_task = asyncio.create_task(self._send_claude_context_loop())
            recv_task = asyncio.create_task(self._receive_loop())

            try:
                await asyncio.gather(send_task, screen_task, claude_ctx_task, recv_task)
            except asyncio.CancelledError:
                pass
            except Exception as e:
                log_error("session_loop", e)
            finally:
                send_task.cancel()
                screen_task.cancel()
                claude_ctx_task.cancel()
                recv_task.cancel()

    async def _send_audio_loop(self):
        """Stream mic audio to Gemini."""
        while self._running:
            chunk = await asyncio.to_thread(self._audio_in.read_chunk, 0.1)
            if chunk and self._session:
                try:
                    await self._session.send_realtime_input(
                        audio=types.Blob(data=chunk, mime_type=f"audio/pcm;rate={SAMPLE_RATE}")
                    )
                except Exception as e:
                    log_error("send_audio", e)
                    break
            await asyncio.sleep(0.01)

    async def _send_claude_context_loop(self):
        """Tail ~/.claude/history.jsonl and feed new messages into the session."""
        import json as _json
        history_path = Path.home() / ".claude" / "history.jsonl"
        if not history_path.exists():
            self.logger.warning("CLAUDE_CTX:history.jsonl not found")
            return

        # Start at end of file
        file_pos = history_path.stat().st_size
        check_interval = 5  # check every 5 seconds

        while self._running and self._session:
            await asyncio.sleep(check_interval)
            try:
                current_size = history_path.stat().st_size
                if current_size <= file_pos:
                    continue

                # Read new lines
                new_text = await asyncio.to_thread(
                    self._read_new_history, history_path, file_pos
                )
                file_pos = current_size

                if new_text and self._session:
                    await self._session.send_realtime_input(
                        text=f"[CLAUDE CODE UPDATE] {new_text}"
                    )
                    if self.config.debug:
                        self.logger.debug(f"CLAUDE_CTX:injected {len(new_text)} chars")
            except Exception as e:
                log_error("claude_context_loop", e)

    @staticmethod
    def _read_new_history(path, start_pos: int) -> str:
        """Read new lines from history.jsonl starting at byte position."""
        import json as _json
        entries = []
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                f.seek(start_pos)
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = _json.loads(line)
                        display = d.get("display", "")
                        if display:
                            if len(display) > 400:
                                display = display[:350] + "...(truncated)"
                            entries.append(display)
                    except _json.JSONDecodeError:
                        continue
        except Exception:
            pass
        return "\n---\n".join(entries) if entries else ""

    async def _send_screen_loop(self):
        """Send periodic screenshots to Gemini as video frames."""
        last_sent = 0
        while self._running:
            now = time.time()
            if now - last_sent >= self._screen_interval and self._session:
                img = await asyncio.to_thread(self._screen.get_latest)
                if img:
                    try:
                        await self._session.send_realtime_input(video=img)
                        last_sent = now
                        if self.config.debug:
                            self.logger.debug(f"SCREEN:sent frame {img.size}")
                    except Exception as e:
                        log_error("send_screen", e)
            await asyncio.sleep(0.5)

    async def _receive_loop(self):
        """Receive audio responses and tool calls from Gemini."""
        while self._running and self._session:
            try:
                async for msg in self._session.receive():
                    if not self._running:
                        break

                    server_content = msg.server_content
                    tool_call = msg.tool_call

                    # Handle audio response
                    if server_content and server_content.model_turn:
                        for part in server_content.model_turn.parts:
                            if part.inline_data and part.inline_data.data:
                                audio_bytes = part.inline_data.data
                                if isinstance(audio_bytes, str):
                                    audio_bytes = base64.b64decode(audio_bytes)
                                self._audio_out.play_chunk(audio_bytes)
                            if part.text:
                                if self.config.debug:
                                    self.logger.debug(f"GEMINI TEXT: {part.text}")

                    # Handle input transcription
                    if server_content and server_content.input_transcription:
                        text = server_content.input_transcription.text
                        if text and self.config.debug:
                            self.logger.debug(f"USER SAID: {text}")

                    # Handle output transcription
                    if server_content and server_content.output_transcription:
                        text = server_content.output_transcription.text
                        if text and self.config.debug:
                            self.logger.debug(f"HALO SAID: {text}")

                    # Handle tool calls
                    if tool_call and tool_call.function_calls:
                        await self._handle_tool_calls(tool_call.function_calls)

                    # Check session reset timer
                    elapsed_min = (time.time() - self._session_start) / 60
                    if elapsed_min >= self.config.session_reset_minutes:
                        log_session_event("context_reset", f"after {elapsed_min:.0f}min")
                        _save_memory(f"Session ended after {elapsed_min:.0f}min (context reset)")
                        break

            except Exception as e:
                log_error("receive_loop", e)
                break

    async def _handle_tool_calls(self, function_calls):
        """Dispatch tool calls and send results back."""
        if not self._tool_handler:
            self.logger.warning("TOOL:no handler registered")
            return

        responses = []
        for fc in function_calls:
            tool_name = fc.name
            args = dict(fc.args) if fc.args else {}
            self.logger.info(f"TOOL CALL: {tool_name}({args})")

            try:
                result = await asyncio.to_thread(self._tool_handler, tool_name, args)
            except Exception as e:
                result = f"Error: {e}"
                log_error(f"tool:{tool_name}", e)

            self.logger.info(f"TOOL RESULT: {tool_name} -> {str(result)[:200]}")
            responses.append(types.FunctionResponse(
                name=tool_name,
                response={"result": str(result)},
                id=fc.id,
            ))

        if responses and self._session:
            try:
                await self._session.send_tool_response(function_responses=responses)
            except Exception as e:
                log_error("send_tool_response", e)

    async def _run_with_reconnect(self):
        """Run session with auto-reconnect on failure."""
        while self._running and self._reconnect_attempts < self._max_reconnect:
            try:
                await self._connect()
                if not self._running:
                    break
                # If we get here, session ended normally (context reset) -- reconnect
                log_session_event("reconnecting", "session ended, starting new")
            except Exception as e:
                self._reconnect_attempts += 1
                wait = min(3 * self._reconnect_attempts, 15)
                log_error(f"session_connect (attempt {self._reconnect_attempts})", e)
                if self._running:
                    await asyncio.sleep(wait)

        if self._reconnect_attempts >= self._max_reconnect:
            log_session_event("gave_up", f"after {self._max_reconnect} reconnect attempts")

    def _run_async_loop(self):
        """Run the async event loop in a background thread."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._run_with_reconnect())
        finally:
            self._loop.close()

    def start(self):
        if self._running:
            return
        self._running = True

        # Start audio
        self._audio_in.start()
        self._audio_out.start(sample_rate=OUTPUT_SAMPLE_RATE)

        # Start screen capture
        self._screen.start()

        # Start session in background thread
        self._thread = threading.Thread(target=self._run_async_loop, daemon=True)
        self._thread.start()
        log_session_event("started")

    def stop(self):
        self._running = False
        self._audio_in.stop()
        self._audio_out.stop()
        self._screen.stop()
        if self._thread:
            self._thread.join(timeout=10)
            self._thread = None
        log_session_event("stopped")

    @property
    def is_running(self) -> bool:
        return self._running

"""Halo context compiler -- builds a knowledge brief at session start.

Reads user-configured context files and compiles them into a single context string
that gets injected into Halo's system prompt. This way Halo knows relevant context
from the start without needing to search.
"""

import os
import time
from pathlib import Path
from modules.config import Config
from modules.logger import get_logger

logger = get_logger()

# Max chars per file (keeps context manageable)
MAX_FILE_CHARS = 15000


def compile_context(config: Config) -> str:
    """Compile a knowledge brief from configured context files + Halo memory.

    Reads files listed in config.context_files and combines them with
    Halo's own persistent memory. Returns a single string ready to
    inject into the system prompt.

    Called once per session connect (every ~2 hours by default).
    """
    start = time.time()
    sections = []

    # Load user-configured context files
    for file_path_str in config.context_files:
        file_path = Path(os.path.expanduser(file_path_str))

        # If path is relative, treat it as relative to vault
        if not file_path.is_absolute():
            file_path = config.vault_path / file_path_str

        if not file_path.exists():
            logger.warning(f"CONTEXT:missing {file_path_str}")
            continue

        try:
            text = file_path.read_text(encoding="utf-8", errors="ignore")
            if len(text) > MAX_FILE_CHARS:
                text = text[:MAX_FILE_CHARS] + "\n...(truncated)"
            label = file_path.name
            sections.append(f"=== {label} ===\n{text}")
        except Exception as e:
            logger.error(f"CONTEXT:error reading {file_path_str}: {e}")

    # Load Halo's own persistent memory
    halo_memory = Path.home() / ".halo" / "memory.md"
    if halo_memory.exists():
        try:
            mem_text = halo_memory.read_text(encoding="utf-8", errors="ignore")
            if mem_text.strip():
                sections.append(f"=== YOUR PERSISTENT MEMORY (things you saved) ===\n{mem_text}")
        except Exception as e:
            logger.error(f"CONTEXT:error reading Halo memory: {e}")

    elapsed = (time.time() - start) * 1000
    total_chars = sum(len(s) for s in sections)
    logger.info(f"CONTEXT:compiled {len(sections)} sources, {total_chars} chars in {elapsed:.0f}ms")

    if not sections:
        return ""

    return (
        "\n\n--- YOUR KNOWLEDGE BASE ---\n"
        "Context loaded from configured files. Use this information to answer questions.\n"
        "Only use search_vault if the knowledge base does not cover the topic.\n\n"
        + "\n\n".join(sections)
        + "\n\n--- END KNOWLEDGE BASE ---"
    )

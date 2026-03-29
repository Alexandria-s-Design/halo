"""Halo vault -- Obsidian RAG with Gemini text-embedding-004 + ChromaDB"""

import hashlib
import time
import threading
from pathlib import Path
from typing import Optional

import chromadb
from google import genai
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

from modules.config import Config
from modules.logger import get_logger, log_vault_query, log_error

# Gemini embedding config
EMBED_MODEL = "gemini-embedding-001"
CHUNK_SIZE = 2000  # chars per chunk
MAX_FILE_SIZE = 500_000  # skip files over 500KB
SUPPORTED_EXTENSIONS = {".md", ".txt", ".yaml", ".yml", ".json", ".csv"}


class VaultIndexer:
    def __init__(self, config: Config):
        self.config = config
        self.vault_path = config.vault_path
        self.logger = get_logger()

        # Gemini client for embeddings
        self._client = genai.Client(api_key=config.gemini_api_key, vertexai=False)

        # ChromaDB -- persistent local store
        self.index_dir = config.index_dir
        self.index_dir.mkdir(parents=True, exist_ok=True)

        try:
            self._chroma = chromadb.PersistentClient(path=str(self.index_dir))
            self._collection = self._chroma.get_or_create_collection(
                name="obsidian_vault",
                metadata={"hnsw:space": "cosine"},
            )
        except Exception as e:
            self.logger.error(f"VAULT:ChromaDB init failed: {e}. Attempting repair...")
            try:
                import shutil
                shutil.rmtree(str(self.index_dir), ignore_errors=True)
                self.index_dir.mkdir(parents=True, exist_ok=True)
                self._chroma = chromadb.PersistentClient(path=str(self.index_dir))
                self._collection = self._chroma.get_or_create_collection(
                    name="obsidian_vault",
                    metadata={"hnsw:space": "cosine"},
                )
                self.logger.info("VAULT:ChromaDB repaired successfully")
            except Exception as repair_err:
                log_error("vault_chromadb_repair", repair_err)
                raise

        # Watchdog
        self._observer: Optional[Observer] = None
        self._reindex_timer: Optional[threading.Timer] = None
        self._pending_changes: set = set()
        self._lock = threading.Lock()

    def _get_files(self) -> list[Path]:
        files = []
        for ext in SUPPORTED_EXTENSIONS:
            files.extend(self.vault_path.rglob(f"*{ext}"))
        # Skip hidden dirs, .git, node_modules, .venv
        return [
            f for f in files
            if not any(part.startswith(".") or part in ("node_modules", "__pycache__") for part in f.parts)
            and f.stat().st_size <= MAX_FILE_SIZE
        ]

    def _chunk_text(self, text: str, path: str) -> list[dict]:
        chunks = []
        for i in range(0, len(text), CHUNK_SIZE):
            chunk = text[i : i + CHUNK_SIZE]
            if chunk.strip():
                chunk_id = hashlib.md5(f"{path}:{i}".encode()).hexdigest()
                chunks.append({"id": chunk_id, "text": chunk, "path": path, "offset": i})
        return chunks

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        result = self._client.models.embed_content(
            model=EMBED_MODEL,
            contents=texts,
        )
        return [e.values for e in result.embeddings]

    def index_vault(self, force: bool = False):
        start = time.time()
        files = self._get_files()
        self.logger.info(f"VAULT:indexing {len(files)} files from {self.vault_path}")

        all_chunks = []
        for f in files:
            try:
                text = f.read_text(encoding="utf-8", errors="ignore")
                rel_path = str(f.relative_to(self.vault_path))
                all_chunks.extend(self._chunk_text(text, rel_path))
            except Exception as e:
                log_error(f"vault_read:{f}", e)

        if not all_chunks:
            self.logger.warning("VAULT:no chunks to index")
            return 0

        # Check what is already indexed (paginate to avoid SQLite variable limit)
        if not force:
            existing_ids = set()
            total = self._collection.count()
            batch_size = 5000
            for offset in range(0, total, batch_size):
                batch = self._collection.get(limit=batch_size, offset=offset)
                existing_ids.update(batch["ids"])
            new_chunks = [c for c in all_chunks if c["id"] not in existing_ids]
        else:
            # Wipe and rebuild
            self._chroma.delete_collection("obsidian_vault")
            self._collection = self._chroma.create_collection(
                name="obsidian_vault",
                metadata={"hnsw:space": "cosine"},
            )
            new_chunks = all_chunks

        if not new_chunks:
            elapsed = (time.time() - start) * 1000
            self.logger.info(f"VAULT:index up to date ({len(all_chunks)} chunks) {elapsed:.0f}ms")
            return len(all_chunks)

        # Embed in batches of 100 (Gemini limit)
        batch_size = 100
        for i in range(0, len(new_chunks), batch_size):
            batch = new_chunks[i : i + batch_size]
            texts = [c["text"] for c in batch]
            try:
                embeddings = self._embed_batch(texts)
                self._collection.add(
                    ids=[c["id"] for c in batch],
                    embeddings=embeddings,
                    documents=texts,
                    metadatas=[{"path": c["path"], "offset": c["offset"]} for c in batch],
                )
            except Exception as e:
                log_error(f"vault_embed:batch_{i}", e)

        elapsed = (time.time() - start) * 1000
        total = self._collection.count()
        self.logger.info(f"VAULT:indexed {len(new_chunks)} new chunks ({total} total) {elapsed:.0f}ms")
        return total

    def search(self, query: str, top_k: int = 5) -> list[dict]:
        start = time.time()
        try:
            query_embedding = self._embed_batch([query])[0]
            results = self._collection.query(
                query_embeddings=[query_embedding],
                n_results=min(top_k, self._collection.count() or 1),
                include=["documents", "metadatas", "distances"],
            )
        except Exception as e:
            log_error("vault_search", e)
            return []

        hits = []
        if results and results["ids"] and results["ids"][0]:
            for i, doc_id in enumerate(results["ids"][0]):
                hits.append({
                    "path": results["metadatas"][0][i]["path"],
                    "content": results["documents"][0][i],
                    "score": 1 - results["distances"][0][i],  # cosine distance -> similarity
                })

        elapsed = (time.time() - start) * 1000
        log_vault_query(query, len(hits), elapsed)
        return hits

    def read_note(self, rel_path: str) -> str:
        full_path = self.vault_path / rel_path
        if not full_path.exists():
            return f"ERROR: Note not found: {rel_path}"
        try:
            return full_path.read_text(encoding="utf-8", errors="ignore")
        except Exception as e:
            return f"ERROR reading {rel_path}: {e}"

    def write_note(self, rel_path: str, content: str) -> str:
        full_path = self.vault_path / rel_path
        try:
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(content, encoding="utf-8")
            return f"OK: wrote {rel_path} ({len(content)} chars)"
        except Exception as e:
            return f"ERROR writing {rel_path}: {e}"

    def append_to_note(self, rel_path: str, content: str) -> str:
        full_path = self.vault_path / rel_path
        if not full_path.exists():
            return self.write_note(rel_path, content)
        try:
            with open(full_path, "a", encoding="utf-8") as f:
                f.write("\n" + content)
            return f"OK: appended to {rel_path} ({len(content)} chars)"
        except Exception as e:
            return f"ERROR appending to {rel_path}: {e}"

    # --- Watchdog for auto-reindex ---

    def _debounced_reindex(self, path: str):
        with self._lock:
            self._pending_changes.add(path)
            if self._reindex_timer:
                self._reindex_timer.cancel()
            self._reindex_timer = threading.Timer(30.0, self._process_changes)
            self._reindex_timer.daemon = True
            self._reindex_timer.start()

    def _process_changes(self):
        with self._lock:
            changes = self._pending_changes.copy()
            self._pending_changes.clear()
        if changes:
            self.logger.info(f"VAULT:re-indexing due to {len(changes)} file changes")
            self.index_vault(force=False)

    def start_watcher(self):
        if self._observer:
            return

        class VaultHandler(FileSystemEventHandler):
            def __init__(self, indexer):
                self.indexer = indexer

            def on_any_event(self, event):
                if event.is_directory:
                    return
                path = Path(event.src_path)
                if path.suffix in SUPPORTED_EXTENSIONS:
                    self.indexer._debounced_reindex(str(path))

        self._observer = Observer()
        self._observer.schedule(VaultHandler(self), str(self.vault_path), recursive=True)
        self._observer.daemon = True
        self._observer.start()
        self.logger.info("VAULT:file watcher started")

    def stop_watcher(self):
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None

    def count(self) -> int:
        return self._collection.count()

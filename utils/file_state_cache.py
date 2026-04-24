"""
File state cache — tracks files read during a session/agent run.
Mirrors src/utils/fileStateCache.ts.

Purpose:
  • FileReadTool records content + mtime when it reads a file
  • FileEditTool consults the cache to detect concurrent modifications
    (file changed on disk since the agent last read it)
  • When spawning a sub-agent, the parent's cache is CLONED:
      - fork-style sub-agents inherit it (same context)
      - fresh sub-agents get an empty clone
  This mirrors cloneFileStateCache() / createFileStateCacheWithSizeLimit()

FileState fields match the TypeScript original:
  content        — raw bytes read from disk (what we actually stored)
  timestamp      — monotonic time of the read (float seconds)
  offset         — line offset passed to Read tool (1-based), or None
  limit          — line limit passed to Read tool, or None
  is_partial_view — True when the model saw less than the full file
                    (e.g. lines 1-2000 of a 5000-line file), so Edit must
                    require an explicit re-Read first
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Iterator

# Max entries kept in the LRU (mirrors READ_FILE_STATE_CACHE_SIZE = 100)
_MAX_ENTRIES = 100


@dataclass
class FileState:
    """Mirrors the FileState type in fileStateCache.ts."""
    content: str
    timestamp: float = field(default_factory=time.monotonic)
    offset: int | None = None
    limit: int | None = None
    is_partial_view: bool = False
    # os.path.getmtime() value captured at read time — used by FileEditTool to
    # detect concurrent disk modifications (mirrors the mtime check in source).
    mtime_at_read: float | None = None


class FileStateCache:
    """
    LRU-bounded file-read cache.
    Mirrors FileStateCache class in fileStateCache.ts.

    Path keys are normalised (os.path.normpath) before storage, matching
    the TypeScript `normalize(key)` behaviour.
    """

    def __init__(self, max_entries: int = _MAX_ENTRIES) -> None:
        self._max = max_entries
        # Ordered dict used as LRU: oldest at front, newest at back
        self._cache: dict[str, FileState] = {}

    # ── Internal helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _key(path: str) -> str:
        return os.path.normpath(path)

    def _evict_if_full(self) -> None:
        while len(self._cache) >= self._max:
            oldest = next(iter(self._cache))
            del self._cache[oldest]

    # ── Public interface (mirrors .get / .set / .has / .delete / .clear) ─────

    def get(self, path: str) -> FileState | None:
        key = self._key(path)
        if key not in self._cache:
            return None
        # Move to end (most recently used)
        state = self._cache.pop(key)
        self._cache[key] = state
        return state

    def set(self, path: str, state: FileState) -> None:
        key = self._key(path)
        if key in self._cache:
            del self._cache[key]
        else:
            self._evict_if_full()
        self._cache[key] = state

    def has(self, path: str) -> bool:
        return self._key(path) in self._cache

    def delete(self, path: str) -> bool:
        key = self._key(path)
        if key in self._cache:
            del self._cache[key]
            return True
        return False

    def clear(self) -> None:
        self._cache.clear()

    def __len__(self) -> int:
        return len(self._cache)

    def __iter__(self) -> Iterator[str]:
        return iter(self._cache)

    # ── Clone (mirrors cloneFileStateCache) ───────────────────────────────────

    def clone(self) -> "FileStateCache":
        """
        Return a shallow copy of the cache for a sub-agent.
        Mirrors cloneFileStateCache() in fileStateCache.ts.

        The sub-agent starts with the parent's read history but writes
        to its own copy — parent and child don't share mutations.
        """
        new = FileStateCache(max_entries=self._max)
        new._cache = dict(self._cache)
        return new


def create_empty_cache() -> FileStateCache:
    """
    Create an empty FileStateCache for a fresh sub-agent.
    Mirrors createFileStateCacheWithSizeLimit() in fileStateCache.ts.
    """
    return FileStateCache()

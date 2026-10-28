"""Content-addressed retention of imported PDF originals.

PDF imports run in cloud-only transient mode: task state and the in-memory
source are dropped shortly after completion, which historically made it
impossible to re-crop figures for already imported questions. This store
keeps the *original* PDF bytes on disk, deduplicated by SHA-256, under a
strict space budget so retention can never silently fill the disk.

Policy knobs (environment variables, read in main.py):
- MATHBANK_PDF_RETENTION_MB   total budget in MB; 0 disables retention.
- MATHBANK_PDF_RETENTION_DAYS entries older than this are evicted; 0 keeps
  entries regardless of age.

Eviction order: oldest ``last_accessed_at`` first, pinned entries last.
Deleting an entry here never touches cropped figure assets in
``static/uploads`` — those are independent copies.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import threading
import uuid
from pathlib import Path

SCHEMA = "mathbank.pdf-retention.v1"

_SHA_RE = re.compile(r"[0-9a-f]{64}")


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(value: dt.datetime) -> str:
    # Microsecond precision keeps LRU ordering stable within the same second.
    return value.isoformat(timespec="microseconds")


class PdfRetentionStore:
    """Deduplicated, budget-bounded disk store for PDF originals."""

    def __init__(self, root: Path, *, max_total_bytes: int | None = 2 * 1024**3,
                 max_age_days: int | None = 365):
        self.root = Path(root)
        self.max_total_bytes = max_total_bytes
        self.max_age_days = max_age_days
        self.enabled = max_total_bytes is None or max_total_bytes > 0
        self.lock = threading.RLock()
        self._entries: dict[str, dict] = {}
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._load_index()

    # ------------------------------------------------------------------ paths
    def _files_root(self) -> Path:
        return self.root / "files"

    def _entry_path(self, sha: str) -> Path:
        return self._files_root() / sha[:2] / f"{sha}.pdf"

    def _index_path(self) -> Path:
        return self.root / "index.json"

    def _atomic_write(self, path: Path, content: bytes) -> None:
        temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with open(temporary, "xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    # ------------------------------------------------------------------ index
    def _load_index(self) -> None:
        try:
            raw = json.loads(self._index_path().read_text("utf-8"))
            entries = raw.get("entries") if isinstance(raw, dict) else None
            if not isinstance(entries, dict):
                raise ValueError
        except (OSError, ValueError):
            entries = {}
        known = {}
        for sha, value in entries.items():
            if not isinstance(sha, str) or not _SHA_RE.fullmatch(sha):
                continue
            if not isinstance(value, dict):
                continue
            known[sha] = self._normalize_entry(value)
        # Reconcile with files actually on disk (index may lag or corrupt).
        for path in self._files_root().glob("*/*.pdf"):
            sha = path.stem
            if _SHA_RE.fullmatch(sha) and sha not in known:
                known[sha] = self._normalize_entry({
                    "sha256": sha,
                    "filename": f"{sha}.pdf",
                    "bytes": path.stat().st_size if path.is_file() else 0,
                })
        self._entries = known

    def _normalize_entry(self, value: dict) -> dict:
        now = _iso(_utcnow())
        try:
            size = int(value.get("bytes") or 0)
        except (TypeError, ValueError):
            size = 0
        return {
            "sha256": str(value.get("sha256") or ""),
            "filename": str(value.get("filename") or "")[:200],
            "bytes": size,
            "first_imported_at": str(value.get("first_imported_at") or now),
            "last_accessed_at": str(value.get("last_accessed_at") or now),
            "imports": int(value.get("imports") or 1),
            "pinned": bool(value.get("pinned")),
        }

    def _save_index(self) -> None:
        payload = {"schema": SCHEMA, "entries": self._entries}
        try:
            self._atomic_write(self._index_path(),
                               json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        except OSError:
            # A failed index write must not fail the import; the file itself
            # is already on disk and the index reconciles on next startup.
            pass

    # ------------------------------------------------------------------- api
    def save(self, content: bytes, filename: str) -> dict | None:
        """Retain a PDF original; returns the entry (deduplicated)."""
        if not self.enabled:
            return None
        sha = hashlib.sha256(content).hexdigest()
        now = _iso(_utcnow())
        with self.lock:
            entry = self._entries.get(sha)
            if entry is not None:
                entry["imports"] = int(entry.get("imports") or 1) + 1
                entry["last_accessed_at"] = now
                if filename:
                    entry["filename"] = filename[:200]
                self._save_index()
                return dict(entry)
            try:
                target = self._entry_path(sha)
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                self._atomic_write(target, content)
            except OSError:
                return None
            entry = self._normalize_entry({
                "sha256": sha,
                "filename": filename or f"{sha}.pdf",
                "bytes": len(content),
                "first_imported_at": now,
                "last_accessed_at": now,
            })
            self._entries[sha] = entry
            self._save_index()
            self.enforce()
            return dict(entry)

    def open(self, sha: str) -> bytes | None:
        """Load a retained original and refresh its LRU timestamp."""
        if not isinstance(sha, str) or not _SHA_RE.fullmatch(sha):
            return None
        with self.lock:
            path = self._entry_path(sha)
            try:
                if path.is_symlink() or not path.is_file():
                    return None
                content = path.read_bytes()
            except OSError:
                return None
            entry = self._entries.get(sha)
            if entry is not None:
                entry["last_accessed_at"] = _iso(_utcnow())
                self._save_index()
            return content

    def delete(self, sha: str) -> bool:
        if not isinstance(sha, str) or not _SHA_RE.fullmatch(sha):
            return False
        with self.lock:
            removed = self._entries.pop(sha, None) is not None
            try:
                self._entry_path(sha).unlink(missing_ok=True)
            except OSError:
                return removed
            self._save_index()
            return removed

    def set_pinned(self, sha: str, pinned: bool) -> bool:
        with self.lock:
            entry = self._entries.get(sha)
            if entry is None:
                return False
            entry["pinned"] = bool(pinned)
            self._save_index()
            return True

    def entries(self) -> list[dict]:
        with self.lock:
            return [dict(value) for value in self._entries.values()]

    def usage(self) -> dict:
        with self.lock:
            total = sum(int(entry.get("bytes") or 0) for entry in self._entries.values())
            return {
                "file_count": len(self._entries),
                "total_bytes": total,
                "budget_bytes": self.max_total_bytes,
                "retention_days": self.max_age_days,
            }

    def enforce(self, *, now: dt.datetime | None = None) -> int:
        """Apply age + budget policy; returns the number of evicted entries."""
        if not self.enabled:
            return 0
        now = now or _utcnow()
        with self.lock:
            evicted = 0
            if self.max_age_days and self.max_age_days > 0:
                deadline = now - dt.timedelta(days=self.max_age_days)
                for sha in sorted(self._entries):
                    entry = self._entries[sha]
                    if entry.get("pinned"):
                        continue
                    try:
                        accessed = dt.datetime.fromisoformat(entry["last_accessed_at"])
                    except (KeyError, ValueError):
                        accessed = now
                    if accessed < deadline and self.delete(sha):
                        evicted += 1
            if self.max_total_bytes is not None:
                def _age(entry: dict) -> str:
                    return str(entry.get("last_accessed_at") or "")

                ordered = sorted(self._entries.values(),
                                 key=lambda entry: (bool(entry.get("pinned")), _age(entry)))
                total = sum(int(entry.get("bytes") or 0) for entry in ordered)
                for entry in ordered:
                    if total <= self.max_total_bytes:
                        break
                    if entry.get("pinned"):
                        continue
                    sha = entry["sha256"]
                    if self.delete(sha):
                        total -= int(entry.get("bytes") or 0)
                        evicted += 1
            return evicted


def retention_settings_from_env(environ=None) -> dict:
    """Read budget/age knobs; None means the knob is unlimited."""
    env = os.environ if environ is None else environ

    def _int(name: str, default: int | None) -> int | None:
        raw = str(env.get(name) or "").strip()
        if not raw:
            return default
        try:
            return int(raw)
        except ValueError:
            return default

    return {
        "max_total_bytes": _int("MATHBANK_PDF_RETENTION_MB", 2048) * 1024 * 1024,
        "max_age_days": _int("MATHBANK_PDF_RETENTION_DAYS", 365),
    }

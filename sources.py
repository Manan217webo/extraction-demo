"""File-server sources: where scanned CRFs are picked up from.

Sources are configured by an administrator in SOURCES_FILE (default
sources.json), never from the browser:

    {"sources": [
        {"id": "site04", "name": "ICR-23-001 · Site 04", "kind": "local",
         "location": "/mnt/fileserver/ICR-23-001/Site04"}
    ]}

`kind: "local"` covers any path the server can read. `~/Desktop/eSource` is the
portable one: `~` is whoever is running the app, and a missing folder under
their home is created on first look. Drop PDFs and folders in it and scan.
An absolute path still works for an SMB/CIFS or NFS share mounted on the host.

Every path that arrives from the browser is relative to a source root and is
checked to stay inside it, symlinks included. Only PDFs are listed or read.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Optional, Protocol

MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_LIST = 5000
MAX_SCAN_FILES = int(os.getenv("SCAN_MAX_FILES") or 2000)
FS_TIMEOUT = float(os.getenv("SOURCE_TIMEOUT_SECONDS") or 20)


class SourceError(RuntimeError):
    """A problem a person can act on: bad path, unreachable share, not a PDF."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class Entry:
    name: str
    path: str  # relative to the source root, "/"-separated, no leading slash
    type: str  # "dir" | "pdf"
    size: Optional[int]
    modified: Optional[str]


class Connector(Protocol):
    id: str
    name: str
    kind: str

    def describe(self) -> str: ...
    async def list_dir(self, rel: str) -> tuple[list[Entry], bool]: ...
    async def walk_pdfs(self, rel: str, limit: int) -> list[Entry]: ...
    async def read(self, rel: str) -> bytes: ...
    async def stat(self, rel: str) -> Entry: ...


def clean_rel(rel: Optional[str]) -> str:
    """A browser-supplied path, normalised, or a SourceError."""
    raw = (rel or "").replace("\\", "/").strip()
    if "\x00" in raw:
        raise SourceError("That path isn't valid.")
    parts = [p for p in PurePosixPath("/" + raw).parts[1:] if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise SourceError("That path isn't valid.")
    return "/".join(parts)


def _iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _hidden(name: str) -> bool:
    return name.startswith(".") or name.startswith("~$") or name.lower() in {"thumbs.db", "desktop.ini"}


class LocalConnector:
    kind = "local"

    def __init__(self, id: str, name: str, location: str) -> None:
        self.id, self.name = id, name
        self.root = Path(location).expanduser()

    def describe(self) -> str:
        return str(self.root)

    def _ensure_root(self) -> Path:
        """The configured folder. A missing folder under this user's home is
        created, so another computer can run the app, drop PDFs on the Desktop,
        and scan them. A missing path outside the home is left alone."""
        if self.root.is_dir():
            return self.root.resolve(strict=True)
        home = Path.home().resolve()
        target = self.root.resolve()
        try:
            target.relative_to(home)
        except ValueError as exc:
            raise SourceError(f"The file server folder for “{self.name}” can't be reached.", 503) from exc
        if target.exists() and not target.is_dir():
            raise SourceError(f"“{self.name}” isn't a folder.", 503)
        target.mkdir(parents=True, exist_ok=True)
        return target.resolve(strict=True)

    def _resolve(self, rel: str) -> Path:
        rel = clean_rel(rel)
        root = self._ensure_root()
        target = (root / rel).resolve()
        if os.path.commonpath([str(root), str(target)]) != str(root):
            raise SourceError("That path is outside the configured folder.")
        return target

    def _entry(self, root: Path, path: Path, st: os.stat_result) -> Entry:
        rel = path.relative_to(root).as_posix()
        is_dir = path.is_dir()
        return Entry(name=path.name, path=rel, type="dir" if is_dir else "pdf",
                     size=None if is_dir else st.st_size, modified=_iso(st.st_mtime))

    def _list(self, rel: str) -> tuple[list[Entry], bool]:
        target = self._resolve(rel)
        root = self.root.resolve()
        if not target.is_dir():
            raise SourceError("That folder no longer exists.", 404)
        out: list[Entry] = []
        truncated = False
        with os.scandir(target) as it:
            for item in it:
                if _hidden(item.name):
                    continue
                path = Path(item.path)
                try:
                    resolved = path.resolve()
                    if os.path.commonpath([str(root), str(resolved)]) != str(root):
                        continue  # a symlink pointing outside the source
                    if item.is_dir():
                        out.append(self._entry(root, path, item.stat()))
                    elif item.is_file() and item.name.lower().endswith(".pdf"):
                        out.append(self._entry(root, path, item.stat()))
                except OSError:
                    continue
                if len(out) >= MAX_LIST:
                    truncated = True
                    break
        out.sort(key=lambda e: (e.type != "dir", e.name.lower()))
        return out, truncated

    def _walk(self, rel: str, limit: int) -> list[Entry]:
        target = self._resolve(rel)
        root = self.root.resolve()
        if target.is_file():
            if not target.name.lower().endswith(".pdf"):
                raise SourceError(f"“{target.name}” isn't a PDF.")
            return [self._entry(root, target, target.stat())]
        found: list[Entry] = []
        for dirpath, dirnames, filenames in os.walk(target, followlinks=False):
            dirnames[:] = sorted(d for d in dirnames if not _hidden(d))
            for name in sorted(filenames):
                if _hidden(name) or not name.lower().endswith(".pdf"):
                    continue
                path = Path(dirpath) / name
                try:
                    resolved = path.resolve()
                    if os.path.commonpath([str(root), str(resolved)]) != str(root):
                        continue
                    found.append(self._entry(root, path, path.stat()))
                except OSError:
                    continue
                if len(found) > limit:
                    raise SourceError(
                        f"That selection has more than {limit} PDFs. Pick a smaller folder."
                    )
        return found

    def _read(self, rel: str) -> bytes:
        target = self._resolve(rel)
        if not target.is_file() or not target.name.lower().endswith(".pdf"):
            raise SourceError("That file no longer exists.", 404)
        if target.stat().st_size > MAX_FILE_BYTES:
            raise SourceError(f"“{target.name}” is over {MAX_FILE_BYTES // (1024 * 1024)} MB.")
        data = target.read_bytes()
        if not data.startswith(b"%PDF-"):
            raise SourceError(f"“{target.name}” isn't a readable PDF.")
        return data

    def _stat(self, rel: str) -> Entry:
        target = self._resolve(rel)
        return self._entry(self.root.resolve(), target, target.stat())

    async def _run(self, fn, *args):
        try:
            return await asyncio.wait_for(asyncio.to_thread(fn, *args), FS_TIMEOUT)
        except asyncio.TimeoutError as exc:
            raise SourceError(f"The file server for “{self.name}” isn't answering. Try again.", 504) from exc
        except PermissionError as exc:
            raise SourceError("The server isn't allowed to read that location.", 403) from exc
        except FileNotFoundError as exc:
            raise SourceError("That location no longer exists.", 404) from exc

    async def list_dir(self, rel: str) -> tuple[list[Entry], bool]:
        return await self._run(self._list, rel)

    async def walk_pdfs(self, rel: str, limit: int = MAX_SCAN_FILES) -> list[Entry]:
        return await self._run(self._walk, rel, limit)

    async def read(self, rel: str) -> bytes:
        return await self._run(self._read, rel)

    async def stat(self, rel: str) -> Entry:
        return await self._run(self._stat, rel)


KINDS = {"local": LocalConnector}


def _config_path() -> Path:
    raw = (os.getenv("SOURCES_FILE") or "sources.json").strip()
    path = Path(raw)
    return path if path.is_absolute() else Path(__file__).resolve().parent / path


def load() -> dict[str, Connector]:
    """The configured sources. Re-read on every call so an edit needs no restart."""
    path = _config_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: dict[str, Connector] = {}
    for item in data.get("sources") or []:
        kind = item.get("kind") or "local"
        cls = KINDS.get(kind)
        if not cls or not item.get("id") or not item.get("location"):
            continue
        out[str(item["id"])] = cls(str(item["id"]), str(item.get("name") or item["id"]),
                                   str(item["location"]))
    return out


def get(source_id: str) -> Connector:
    found = load().get(source_id)
    if not found:
        raise SourceError("That file source isn't configured.", 404)
    return found


def summary(conn: Connector) -> dict[str, Any]:
    return {"id": conn.id, "name": conn.name, "kind": conn.kind, "location": conn.describe()}

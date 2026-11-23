"""Resume support: an append-only manifest of finished files kept in the output folder."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from .core import FileResult
from .identity import COMMAND_NAME

MANIFEST_NAME = f".{COMMAND_NAME}-manifest.jsonl"


class Manifest:
    """One JSON line per finished file. A line is written right after the file is done,
    so a crash or Ctrl-C loses at most the file that was in progress."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.entries = {}
        self.stale = 0  # entries made with different options (not reused)

    def load(self) -> None:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    entry = json.loads(line)
                    self.entries[entry["src"]] = entry  # last line wins
                except (ValueError, KeyError):
                    continue  # ignore a half-written last line

    def lookup(self, rel: Path, size: int, mtime_ns: int, fingerprint: str,
               dst_dir: Path) -> Optional[dict]:
        """The earlier entry if this exact file was already processed with the same options."""
        entry = self.entries.get(rel.as_posix())
        if entry is None:
            return None
        if entry.get("size") != size or entry.get("mtime_ns") != mtime_ns:
            return None  # source file changed
        if entry.get("opts") != fingerprint:
            self.stale += 1
            return None
        if not (dst_dir / entry["dst"]).exists():
            return None  # output was deleted
        return entry

    def record(self, res: FileResult, size: int, mtime_ns: int, fingerprint: str,
               dst_dir: Path) -> None:
        if res.dst is None:
            return
        entry = {
            "src": res.rel.as_posix(),
            "size": size,
            "mtime_ns": mtime_ns,
            "opts": fingerprint,
            "dst": res.dst.relative_to(dst_dir).as_posix(),
            "status": res.status,
            "bytes_in": res.bytes_in,
            "bytes_out": res.bytes_out,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")

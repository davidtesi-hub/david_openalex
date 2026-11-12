"""Reversible zlib storage for the OpenAlex journal SQLite databases.

Only the journal paper database is compressed. Coverage and Crossref SQLite
files retain their existing format. Never open a compressed stream as SQLite.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import zlib
from pathlib import Path

CHUNK = 1024 * 1024
GITHUB_LIMIT = 100 * 1024 * 1024


def restore(path: Path) -> Path:
    archive = path.with_suffix(".db")
    if not archive.exists():
        return path
    if path.exists() and path.stat().st_mtime_ns >= archive.stat().st_mtime_ns:
        with path.open("rb") as current:
            if current.read(16) == b"SQLite format 3\x00":
                return path
    temp = path.with_name(path.name + ".restore.tmp")
    decoder = zlib.decompressobj()
    try:
        with archive.open("rb") as source, temp.open("wb") as target:
            for chunk in iter(lambda: source.read(CHUNK), b""):
                target.write(decoder.decompress(chunk))
            target.write(decoder.flush())
            target.flush()
            os.fsync(target.fileno())
        if not decoder.eof or decoder.unused_data:
            raise ValueError(f"Incomplete or trailing zlib data: {archive}")
        db = sqlite3.connect(temp.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            result = db.execute("PRAGMA quick_check").fetchone()
        finally:
            db.close()
        if result != ("ok",):
            raise ValueError(f"SQLite quick_check failed: {archive}: {result}")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
    return path


def pack(path: Path) -> Path:
    if not path.exists():
        raise FileNotFoundError(path)
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        result = db.execute("PRAGMA quick_check").fetchone()
    finally:
        db.close()
    if result != ("ok",):
        raise ValueError(f"SQLite quick_check failed: {path}: {result}")
    archive = path.with_suffix(".db")
    temp = archive.with_name(archive.name + ".tmp")
    encoder = zlib.compressobj(level=9)
    try:
        with path.open("rb") as source, temp.open("wb") as target:
            for chunk in iter(lambda: source.read(CHUNK), b""):
                target.write(encoder.compress(chunk))
            target.write(encoder.flush())
            target.flush()
            os.fsync(target.fileno())
        if temp.stat().st_size >= GITHUB_LIMIT:
            raise ValueError(f"Compressed file still exceeds GitHub's 100 MiB limit: {temp}")
        os.replace(temp, archive)
        # The local restored copy represents this archive until either changes.
        os.utime(path, ns=(archive.stat().st_mtime_ns, archive.stat().st_mtime_ns))
    finally:
        temp.unlink(missing_ok=True)
    return archive


def journal_paths(directory: Path):
    """List journal paper stores, excluding coverage and Crossref."""
    import json
    root = Path(__file__).resolve().parents[1]
    journals = json.loads((root / "config.json").read_text(encoding="utf-8"))["journals"]
    for journal in journals:
        path = directory / f"{journal}.sqlite"
        if path.exists() or path.with_suffix(".db").exists():
            yield path


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=["restore", "pack"])
    parser.add_argument("paths", nargs="*", type=Path)
    args = parser.parse_args()
    paths = args.paths or list(journal_paths(Path(__file__).resolve().parents[1] / "library"))
    for path in paths:
        result = restore(path) if args.operation == "restore" else pack(path)
        print(result, flush=True)

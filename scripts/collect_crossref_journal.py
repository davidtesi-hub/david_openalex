#!/usr/bin/env python3
"""Capture full Crossref records by journal and publication date, for two outputs."""

from __future__ import annotations

import argparse
import email.utils
import json
import os
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zlib
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config.json"
PAGE_SIZE = 1000
INTERVAL_DAYS = 30
WINDOW_DAYS = 370


def window(today: date | None = None) -> tuple[date, date]:
    end = (today or datetime.now(timezone.utc).date()) - timedelta(days=1)
    return end - timedelta(days=WINDOW_DAYS - 1), end


def intervals(start: date, end: date):
    """Newest first, each interval inclusive and at most 30 days."""
    right = end
    while right >= start:
        left = max(start, right - timedelta(days=INTERVAL_DAYS - 1))
        yield left, right
        right = left - timedelta(days=1)


class Client:
    def __init__(self, issn: str, mailto: str, max_requests: int, deadline: float):
        self.issn, self.mailto = issn, mailto
        self.max_requests, self.deadline = max_requests, deadline
        self.attempts = 0
        self.last_finished = 0.0

    def fetch(self, params: dict) -> dict:
        params = {**params, "mailto": self.mailto}
        for retry in range(4):
            if self.attempts >= self.max_requests or time.time() >= self.deadline:
                raise RuntimeError("Request or runtime limit reached; interval incomplete")
            time.sleep(max(0, 1 - (time.monotonic() - self.last_finished)))
            self.attempts += 1
            request = urllib.request.Request(
                f"https://api.crossref.org/journals/{self.issn}/works?" + urllib.parse.urlencode(params),
                headers={"User-Agent": f"david-openalex-crossref/2.0 (mailto:{self.mailto})",
                         "Accept": "application/json"},
            )
            delay = 2 ** (retry + 1)
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    data = json.load(response)
                if data.get("status") != "ok" or not isinstance(data.get("message"), dict):
                    raise ValueError("Invalid Crossref response")
                return data["message"]
            except urllib.error.HTTPError as error:
                if error.code != 429 and not 500 <= error.code < 600:
                    raise RuntimeError(f"Crossref HTTP {error.code}") from None
                after = error.headers.get("Retry-After")
                if after:
                    try:
                        delay = max(delay, float(after))
                    except ValueError:
                        try:
                            delay = max(delay, (email.utils.parsedate_to_datetime(after) - datetime.now(timezone.utc)).total_seconds())
                        except (ValueError, TypeError):
                            pass
            except (urllib.error.URLError, TimeoutError, ValueError):
                pass
            finally:
                self.last_finished = time.monotonic()
            if retry == 3:
                raise RuntimeError("Crossref retries exhausted")
            if delay > 300:
                raise RuntimeError("Crossref requested a long pause; resume on a later run")
            time.sleep(delay)
        raise RuntimeError("Unreachable retry state")


def open_db(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.executescript("""
    CREATE TABLE IF NOT EXISTS papers (
      doi TEXT PRIMARY KEY, title TEXT, raw_json_zlib BLOB NOT NULL, last_scan_utc TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS runs (
      id TEXT PRIMARY KEY, mode TEXT, issn TEXT, from_date TEXT, to_date TEXT,
      query_filter TEXT, started_utc TEXT, finished_utc TEXT, status TEXT,
      expected INTEGER, retrieved INTEGER DEFAULT 0, requests INTEGER DEFAULT 0, error TEXT);
    CREATE TABLE IF NOT EXISTS interval_coverage (
      from_date TEXT NOT NULL, to_date TEXT NOT NULL, expected INTEGER NOT NULL,
      retrieved INTEGER NOT NULL, last_scan_utc TEXT NOT NULL,
      PRIMARY KEY(from_date,to_date));
    """)
    columns = {row[1] for row in db.execute("PRAGMA table_info(papers)")}
    if "raw_json" in columns and "raw_json_zlib" not in columns:
        # Existing A&A manual databases used uncompressed JSON text.
        db.execute("""CREATE TABLE papers_compressed (
          doi TEXT PRIMARY KEY, title TEXT, raw_json_zlib BLOB NOT NULL, last_scan_utc TEXT NOT NULL)""")
        for doi, title, raw, last_scan in db.execute("SELECT doi,title,raw_json,last_scan_utc FROM papers"):
            db.execute("INSERT INTO papers_compressed VALUES(?,?,?,?)",
                       (doi, title, pack_record(json.loads(raw)), last_scan))
        db.execute("DROP TABLE papers")
        db.execute("ALTER TABLE papers_compressed RENAME TO papers")
        db.commit()
        db.execute("VACUUM")
    return db


def stamp():
    return datetime.now(timezone.utc).isoformat()


def pack_record(item: dict) -> bytes:
    """Preserve every provider field, compressed inside the SQLite BLOB."""
    return zlib.compress(json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), 6)


def unpack_record(blob: bytes) -> dict:
    return json.loads(zlib.decompress(blob).decode("utf-8"))


def capture_interval(db, client: Client, start: date, end: date):
    query = f"type:journal-article,from-pub-date:{start},until-pub-date:{end}"
    seen, cursors = set(), set()
    cursor, expected = "*", None
    while True:
        if cursor in cursors:
            raise RuntimeError(f"Repeated cursor in {start}..{end}")
        cursors.add(cursor)
        message = client.fetch({"filter": query, "rows": PAGE_SIZE, "cursor": cursor})
        items, total = message.get("items"), message.get("total-results")
        if not isinstance(items, list) or not isinstance(total, int):
            raise RuntimeError("Invalid Crossref list page")
        if expected is None:
            expected = total
        if expected != total:
            raise RuntimeError(f"Total changed during {start}..{end}; retry interval later")
        with db:
            for item in items:
                doi = item.get("DOI", "").strip().lower()
                if not doi or item.get("type") != "journal-article" or client.issn not in item.get("ISSN", []):
                    raise RuntimeError("Record contradicts requested ISSN/type")
                if doi in seen:
                    raise RuntimeError(f"Repeated DOI in {start}..{end}")
                seen.add(doi)
                # Both logical collectors currently retain every provider field.
                db.execute("""INSERT INTO papers(doi,title,raw_json_zlib,last_scan_utc)
                    VALUES(?,?,?,?) ON CONFLICT(doi) DO UPDATE SET
                    title=excluded.title,raw_json_zlib=excluded.raw_json_zlib,last_scan_utc=excluded.last_scan_utc""",
                    (doi, " ".join(item.get("title", [])), pack_record(item), stamp()))
        print(json.dumps({"interval": [str(start), str(end)], "retrieved": len(seen), "expected": expected}), flush=True)
        if len(seen) == expected:
            break
        if len(items) < PAGE_SIZE or not message.get("next-cursor") or len(seen) > expected:
            raise RuntimeError(f"Incomplete pagination in {start}..{end}")
        cursor = message["next-cursor"]
    with db:
        db.execute("""INSERT INTO interval_coverage VALUES(?,?,?,?,?)
            ON CONFLICT(from_date,to_date) DO UPDATE SET
            expected=excluded.expected,retrieved=excluded.retrieved,last_scan_utc=excluded.last_scan_utc""",
            (str(start), str(end), expected, len(seen), stamp()))
    return expected, len(seen)


def run(journal: str, mode: str, max_requests: int = 100):
    journals = json.loads(CONFIG.read_text(encoding="utf-8"))["journals"]
    if journal not in journals:
        raise ValueError(f"Unknown journal: {journal}")
    issn = journals[journal]["crossref_issn"]
    mailto = os.environ.get(("CROSSMARK" if mode == "crossmark" else "CROSSREF") + "_MAILTO", "").strip()
    if "@" not in mailto or "\r" in mailto or "\n" in mailto:
        raise ValueError("Missing contact email secret")
    start, end = window()
    db = open_db(ROOT / "library" / mode / f"{journal}.sqlite")
    ident = str(uuid.uuid4())
    db.execute("""INSERT INTO runs(id,mode,issn,from_date,to_date,query_filter,started_utc,status)
        VALUES(?,?,?,?,?,?,?,?)""", (ident, mode, issn, str(start), str(end),
        "type:journal-article,from-pub-date:...,until-pub-date:...", stamp(), "running"))
    db.commit()
    deadline = min(float(os.environ.get("CROSSREF_DEADLINE_EPOCH", time.time() + 24 * 60)), time.time() + 24 * 60)
    client = Client(issn, mailto, max_requests, deadline)
    expected = retrieved = 0
    status, error = "ok", None
    try:
        for left, right in intervals(start, end):
            total, count = capture_interval(db, client, left, right)
            expected += total
            retrieved += count
    except Exception as exc:
        status, error = "error", str(exc)
    finally:
        db.execute("""UPDATE runs SET finished_utc=?,status=?,expected=?,retrieved=?,requests=?,error=?
            WHERE id=?""", (stamp(), status, expected, retrieved, client.attempts, error, ident))
        db.commit()
        db.close()
    print(json.dumps({"journal": journal, "mode": mode, "from_date": str(start), "to_date": str(end),
                      "status": status, "completed_records": retrieved, "requests": client.attempts,
                      "error": error}), flush=True)
    return 0 if status == "ok" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", required=True)
    parser.add_argument("--mode", choices=("crossref", "crossmark"), required=True)
    parser.add_argument("--max-requests", type=int, default=100)
    args = parser.parse_args()
    if args.max_requests < 1:
        parser.error("--max-requests must be positive")
    raise SystemExit(run(args.journal, args.mode, args.max_requests))

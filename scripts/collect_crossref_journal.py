#!/usr/bin/env python3
"""Capture Crossref publication dates and update status by journal."""

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
    try:
        from scripts.library_storage import restore
    except ModuleNotFoundError:
        from library_storage import restore
    path = restore(path)
    db = sqlite3.connect(path)
    db.executescript("""
    CREATE TABLE IF NOT EXISTS papers (
      doi TEXT PRIMARY KEY,
      published_online TEXT, published_print TEXT, issued TEXT, accepted TEXT,
      created TEXT, deposited TEXT, indexed TEXT,
      status TEXT NOT NULL DEFAULT 'unknown', updates_json TEXT NOT NULL DEFAULT '[]',
      last_checked_utc TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS runs (
      id TEXT PRIMARY KEY, mode TEXT, issn TEXT, from_date TEXT, to_date TEXT,
      query_filter TEXT, started_utc TEXT, finished_utc TEXT, status TEXT,
      expected INTEGER, retrieved INTEGER DEFAULT 0, requests INTEGER DEFAULT 0, error TEXT);
    CREATE TABLE IF NOT EXISTS interval_coverage (
      from_date TEXT NOT NULL, to_date TEXT NOT NULL, expected INTEGER NOT NULL,
      retrieved INTEGER NOT NULL, last_scan_utc TEXT NOT NULL,
      PRIMARY KEY(from_date,to_date));
    """)
    return db


def stamp():
    return datetime.now(timezone.utc).isoformat()


def date_value(record: dict, key: str):
    value = record.get(key)
    if not isinstance(value, dict):
        return None
    parts = value.get("date-parts")
    if not parts or not parts[0]:
        return None
    return "-".join(f"{int(part):02d}" if i else f"{int(part):04d}" for i, part in enumerate(parts[0]))


def update_data(record: dict):
    updates = []
    for direction in ("updated-by", "update-to"):
        entries = record.get(direction) or []
        for entry in entries:
            updates.append({
                "direction": direction,
                "doi": (entry.get("DOI") or "").lower() or None,
                "type": entry.get("type"),
                "label": entry.get("label"),
                "date": date_value(entry, "updated"),
            })
    types = {str(update.get("type", "")).lower() for update in updates if update["direction"] == "updated-by"}
    if types & {"retraction", "withdrawal", "removal", "partial_retraction"}:
        status = "retracted"
    elif any(update["direction"] == "updated-by" for update in updates):
        status = "updated"
    elif any(update["direction"] == "update-to" for update in updates):
        status = "update_notice"
    else:
        status = "unknown"
    return status, json.dumps(updates, ensure_ascii=False, separators=(",", ":"))


def normalized_record(record: dict):
    status, updates = update_data(record)
    return (
        date_value(record, "published-online"),
        date_value(record, "published-print"),
        date_value(record, "issued"),
        date_value(record, "accepted"),
        date_value(record, "created"),
        date_value(record, "deposited"),
        date_value(record, "indexed"),
        status, updates, stamp(),
    )


def capture_interval(db, client: Client, start: date, end: date, *, provisional: bool = False):
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
                db.execute("""INSERT INTO papers(
                    doi,published_online,published_print,issued,accepted,created,deposited,indexed,status,updates_json,last_checked_utc)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(doi) DO UPDATE SET
                    published_online=COALESCE(excluded.published_online,published_online),
                    published_print=COALESCE(excluded.published_print,published_print),
                    issued=COALESCE(excluded.issued,issued), accepted=COALESCE(excluded.accepted,accepted),
                    created=COALESCE(excluded.created,created), deposited=COALESCE(excluded.deposited,deposited),
                    indexed=COALESCE(excluded.indexed,indexed),
                    status=CASE WHEN excluded.status='unknown' THEN status ELSE excluded.status END,
                    updates_json=CASE WHEN excluded.updates_json='[]' THEN updates_json ELSE excluded.updates_json END,
                    last_checked_utc=excluded.last_checked_utc""",
                    (doi, *normalized_record(item)))
        print(json.dumps({"interval": [str(start), str(end)], "retrieved": len(seen), "expected": expected}), flush=True)
        if len(seen) == expected:
            break
        if len(items) < PAGE_SIZE or not message.get("next-cursor") or len(seen) > expected:
            raise RuntimeError(f"Incomplete pagination in {start}..{end}")
        cursor = message["next-cursor"]
    if not provisional:
        with db:
            db.execute("""INSERT INTO interval_coverage VALUES(?,?,?,?,?)
                ON CONFLICT(from_date,to_date) DO UPDATE SET
                expected=excluded.expected,retrieved=excluded.retrieved,last_scan_utc=excluded.last_scan_utc""",
                (str(start), str(end), expected, len(seen), stamp()))
    return expected, len(seen)


def run(journal: str, mode: str = "crossref", max_requests: int = 100):
    journals = json.loads(CONFIG.read_text(encoding="utf-8"))["journals"]
    if journal not in journals:
        raise ValueError(f"Unknown journal: {journal}")
    issn = journals[journal]["crossref_issn"]
    if mode != "crossref":
        raise ValueError("Only the Crossref collector is supported")
    mailto = os.environ.get("CROSSREF_MAILTO", "").strip()
    if "@" not in mailto or "\r" in mailto or "\n" in mailto:
        raise ValueError("Missing contact email secret")
    today = datetime.now(timezone.utc).date()
    start, end = window(today)
    db = open_db(ROOT / "library" / mode / f"{journal}.sqlite")
    ident = str(uuid.uuid4())
    db.execute("""INSERT INTO runs(id,mode,issn,from_date,to_date,query_filter,started_utc,status)
        VALUES(?,?,?,?,?,?,?,?)""", (ident, mode, issn, str(start), str(today),
        "type:journal-article,from-pub-date:...,until-pub-date:...", stamp(), "running"))
    db.commit()
    deadline = min(float(os.environ.get("CROSSREF_DEADLINE_EPOCH", time.time() + 24 * 60)), time.time() + 24 * 60)
    client = Client(issn, mailto, max_requests, deadline)
    expected = retrieved = 0
    status, error = "ok", None
    try:
        # Store today's available records without declaring the day complete.
        total, count = capture_interval(db, client, today, today, provisional=True)
        expected += total
        retrieved += count
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
    print(json.dumps({"journal": journal, "mode": mode, "from_date": str(start), "to_date": str(today),
                      "covered_through_utc_day": str(end), "provisional_utc_day": str(today),
                      "status": status, "completed_records": retrieved, "requests": client.attempts,
                      "error": error}), flush=True)
    return 0 if status == "ok" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", required=True)
    parser.add_argument("--mode", choices=("crossref",), default="crossref")
    parser.add_argument("--max-requests", type=int, default=100)
    args = parser.parse_args()
    if args.max_requests < 1:
        parser.error("--max-requests must be positive")
    raise SystemExit(run(args.journal, args.mode, args.max_requests))

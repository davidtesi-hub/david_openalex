#!/usr/bin/env python3
"""Collect a selected journal into a resumable, coverage-aware SQLite library."""
import argparse
from collections import Counter
import email.utils
import json
import os
import random
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from field_selection import selected_work, short_id, short_doi, migrate_papers

API = "https://api.openalex.org/works"
USER_AGENT = "david-openalex/1.0 (https://github.com/davidtesi-hub/david_openalex)"


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class StopCollection(Exception):
    """Expected resource or service stop; the current day remains incomplete."""


def init_db(db):
    db.executescript("""
    PRAGMA journal_mode=DELETE;
    CREATE TABLE IF NOT EXISTS papers (
      id TEXT PRIMARY KEY, doi TEXT, title TEXT, publication_date TEXT,
      type TEXT NOT NULL, source_id TEXT NOT NULL, abstract TEXT,
      raw_json TEXT NOT NULL, provider_updated_date TEXT,
      first_seen_utc TEXT NOT NULL, last_seen_utc TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS papers_date ON papers(publication_date);
    CREATE TABLE IF NOT EXISTS day_coverage (
      day TEXT PRIMARY KEY, query_filter TEXT NOT NULL, status TEXT NOT NULL,
      initial_count INTEGER, final_count INTEGER, rows_received INTEGER DEFAULT 0,
      unique_count INTEGER DEFAULT 0, pages INTEGER DEFAULT 0, cursor TEXT,
      last_completed_utc TEXT, last_attempt_utc TEXT, error TEXT);
    CREATE TABLE IF NOT EXISTS query_runs (
      id TEXT PRIMARY KEY, started_utc TEXT NOT NULL, finished_utc TEXT,
      status TEXT NOT NULL, reason TEXT, attempts INTEGER DEFAULT 0,
      days_completed INTEGER DEFAULT 0, reported_cost_usd REAL DEFAULT 0,
      max_days INTEGER, max_requests INTEGER);
    CREATE TABLE IF NOT EXISTS collector_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS request_log (
      id INTEGER PRIMARY KEY, run_id TEXT, time_utc TEXT, params_json TEXT,
      http_status INTEGER, elapsed_seconds REAL, reported_cost_usd REAL,
      usage_headers_json TEXT, error_code TEXT);
    """)
    db.execute("""CREATE TABLE IF NOT EXISTS interval_coverage (
      from_date TEXT NOT NULL, to_date TEXT NOT NULL, query_filter TEXT NOT NULL,
      status TEXT NOT NULL, initial_count INTEGER, final_count INTEGER,
      rows_received INTEGER DEFAULT 0, unique_count INTEGER DEFAULT 0,
      pages INTEGER DEFAULT 0, cursor TEXT, last_completed_utc TEXT,
      last_attempt_utc TEXT, error TEXT, PRIMARY KEY(from_date,to_date))""")
    columns = {row[1] for row in db.execute("PRAGMA table_info(query_runs)")}
    for column in ("intervals_completed", "max_intervals"):
        if column not in columns:
            db.execute("ALTER TABLE query_runs ADD COLUMN " + column + " INTEGER")
    db.commit()
    migrate_papers(db)
    init_daily_coverage(db)


class Client:
    def __init__(self, db, run_id, key, config, max_requests, deadline):
        self.db, self.run_id, self.key, self.config = db, run_id, key, config
        self.max_requests, self.deadline = max_requests, deadline
        self.attempts = 0
        self.last_finished = 0.0
        self.cost = 0.0

    def sleep(self, seconds):
        if time.monotonic() + seconds >= self.deadline:
            raise StopCollection("time_limit")
        time.sleep(seconds)

    def fetch(self, params):
        for retry in range(self.config["max_retries"] + 1):
            if self.attempts >= self.max_requests:
                raise StopCollection("request_limit")
            if time.monotonic() >= self.deadline:
                raise StopCollection("time_limit")
            self.sleep(max(0, self.config["request_interval_seconds"] -
                           (time.monotonic() - self.last_finished)))
            request = urllib.request.Request(
                API + "?" + urllib.parse.urlencode(params),
                headers={"Authorization": "Bearer " + self.key,
                         "User-Agent": USER_AGENT, "Accept": "application/json"})
            self.attempts += 1
            started = time.monotonic()
            status, headers, payload, code = None, {}, None, None
            network_error = False
            try:
                with urllib.request.urlopen(request, timeout=self.config["request_timeout_seconds"]) as response:
                    status, headers = response.status, response.headers
                    payload = json.loads(response.read())
            except urllib.error.HTTPError as error:
                status, headers = error.code, error.headers
                try:
                    body = json.loads(error.read())
                    code = str(body.get("code") or body.get("error") or "http_error")[:200]
                except (ValueError, AttributeError):
                    code = "http_error"
            except (urllib.error.URLError, TimeoutError, OSError, ValueError):
                network_error, code = True, "network_or_json_error"
            self.last_finished = time.monotonic()
            cost = (payload or {}).get("meta", {}).get("cost_usd")
            cost = float(cost) if isinstance(cost, (int, float)) else None
            self.cost += cost or 0
            usage = {k: v for k, v in headers.items()
                     if "ratelimit" in k.lower() or "rate-limit" in k.lower()
                     or "budget" in k.lower() or k.lower() == "retry-after"}
            self.db.execute("INSERT INTO request_log(run_id,time_utc,params_json,http_status,"
                            "elapsed_seconds,reported_cost_usd,usage_headers_json,error_code) "
                            "VALUES(?,?,?,?,?,?,?,?)",
                            (self.run_id, now(), json.dumps(params), status,
                             self.last_finished - started, cost, json.dumps(usage), code))
            self.db.commit()
            if status == 200 and isinstance(payload, dict):
                return payload
            if status in (401, 403):
                raise RuntimeError("OpenAlex authentication/authorization failed (HTTP %s)" % status)
            if status == 429:
                # An unidentified 429 may be a daily budget stop: never hammer it.
                raise StopCollection("openalex_429_budget_or_rate_limit")
            if not (network_error or (status is not None and status >= 500)):
                raise RuntimeError("OpenAlex request failed (HTTP %s)" % status)
            if retry == self.config["max_retries"]:
                raise RuntimeError("OpenAlex retries exhausted")
            delay = min(60, 2 ** (retry + 1)) + random.random()
            after = headers.get("Retry-After")
            if after:
                try:
                    delay = max(delay, float(after))
                except ValueError:
                    try:
                        until = email.utils.parsedate_to_datetime(after)
                        delay = max(delay, (until - datetime.now(timezone.utc)).total_seconds())
                    except (ValueError, TypeError):
                        pass
            self.sleep(delay)


def abstract_text(index):
    if not isinstance(index, dict):
        return None
    tokens = [(p, word) for word, positions in index.items() for p in positions]
    return " ".join(word for _, word in sorted(tokens))


def journal_filter(journal):
    return "primary_location.source.id:%s,type:article" % journal["source_id"]


def collect_interval(db, client, journal, from_date, to_date, *, provisional=False):
    if from_date > to_date:
        raise ValueError("Reversed interval")
    query = journal_filter(journal) + ",from_publication_date:%s,to_publication_date:%s" % (from_date, to_date)
    db.execute("INSERT INTO interval_coverage(from_date,to_date,query_filter,status,last_attempt_utc) VALUES(?,?,?,'running',?) "
               "ON CONFLICT(from_date,to_date) DO UPDATE SET query_filter=excluded.query_filter,status='running',"
               "rows_received=0,unique_count=0,pages=0,cursor='*',initial_count=NULL,final_count=NULL,"
               "last_attempt_utc=excluded.last_attempt_utc,error=NULL", (from_date, to_date, query, now()))
    db.commit()
    cursor, seen_cursors, ids = "*", set(), set()
    rows, pages, initial = 0, 0, None
    daily_counts = Counter()
    try:
        while True:
            if cursor in seen_cursors:
                raise RuntimeError("Repeated cursor")
            seen_cursors.add(cursor)
            payload = client.fetch({"filter": query, "per_page": client.config["per_page"], "cursor": cursor})
            meta, records = payload.get("meta"), payload.get("results")
            if not isinstance(meta, dict) or not isinstance(records, list) or "next_cursor" not in meta:
                raise RuntimeError("Invalid OpenAlex page envelope")
            total = meta.get("count")
            if not isinstance(total, int) or total < 0:
                raise RuntimeError("Invalid OpenAlex count")
            if initial is None:
                initial = total
            if total != initial:
                raise RuntimeError("Count changed during pagination; reconcile this day")
            next_cursor = meta["next_cursor"]
            if next_cursor is not None and not isinstance(next_cursor, str):
                raise RuntimeError("Invalid cursor")
            timestamp = now()
            with db:
                for work in records:
                    source = ((work.get("primary_location") or {}).get("source") or {}).get("id", "")
                    work_id = work.get("id", "")
                    if (not work_id.startswith("https://openalex.org/W") or work.get("type") != "article"
                            or source.rsplit("/", 1)[-1] != journal["source_id"]
                            or not from_date <= (work.get("publication_date") or "") <= to_date):
                        raise RuntimeError("Work contradicts journal/type/date filter")
                    if work_id in ids:
                        raise RuntimeError("Duplicate Work ID across pages")
                    ids.add(work_id)
                    daily_counts[work["publication_date"]] += 1
                    db.execute("INSERT INTO papers VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                               "doi=excluded.doi,title=excluded.title,publication_date=excluded.publication_date,"
                               "type=excluded.type,source_id=excluded.source_id,abstract=excluded.abstract,"
                               "raw_json=excluded.raw_json,provider_updated_date=excluded.provider_updated_date,"
                               "last_seen_utc=excluded.last_seen_utc",
                               (short_id(work_id), short_doi(work.get("doi")), work.get("title"), work.get("publication_date"), "article", short_id(source),
                                abstract_text(work.get("abstract_inverted_index")),
                                json.dumps(selected_work(work), ensure_ascii=False), work.get("updated_date"), timestamp, timestamp))
                rows += len(records)
                pages += 1
                db.execute("UPDATE interval_coverage SET initial_count=?,final_count=?,rows_received=?,"
                           "unique_count=?,pages=?,cursor=? WHERE from_date=? AND to_date=?",
                           (initial, total, rows, len(ids), pages, next_cursor, from_date, to_date))
            if next_cursor is None:
                if len(ids) != initial:
                    raise RuntimeError("Pagination count mismatch")
                break
            cursor = next_cursor
        if not provisional:
            record_daily_coverage(db, from_date, to_date, daily_counts, now())
        db.execute("UPDATE interval_coverage SET status=?,last_completed_utc=?,error=NULL WHERE from_date=? AND to_date=?",
                   ("partial" if provisional else "ok", now(), from_date, to_date))
        db.commit()
    except Exception as error:
        db.execute("UPDATE interval_coverage SET status='error',error=? WHERE from_date=? AND to_date=?", (str(error), from_date, to_date))
        db.commit()
        raise


def init_daily_coverage(db):
    db.execute("CREATE TABLE IF NOT EXISTS scan_daily_coverage (day TEXT PRIMARY KEY, articles INTEGER NOT NULL, last_scan TEXT NOT NULL)")
    migrated = db.execute("SELECT value FROM collector_state WHERE key='daily_coverage_migrated'").fetchone()
    if migrated:
        return
    # Historical manifests have interval totals, not counts per publication day.
    # Reconstruct daily counts from the currently stored articles; keep genuine completion dates.
    counts = dict(db.execute("SELECT publication_date,count(*) FROM papers GROUP BY publication_date"))
    entries = list(db.execute("SELECT day,day,last_completed_utc FROM day_coverage WHERE status='ok' AND last_completed_utc IS NOT NULL"))
    entries += list(db.execute("SELECT from_date,to_date,last_completed_utc FROM interval_coverage WHERE status='ok' AND last_completed_utc IS NOT NULL"))
    with db:
        for start, end, scanned in sorted(entries, key=lambda row: row[2]):
            day = date.fromisoformat(start)
            while day <= date.fromisoformat(end):
                db.execute("INSERT INTO scan_daily_coverage VALUES(?,?,?) ON CONFLICT(day) DO UPDATE SET articles=excluded.articles,last_scan=excluded.last_scan",
                           (day.isoformat(), counts.get(day.isoformat(), 0), scanned[:10]))
                day += timedelta(days=1)
        db.execute("INSERT INTO collector_state VALUES('daily_coverage_migrated','1')")


def record_daily_coverage(db, first, last, counts, scanned):
    day = date.fromisoformat(first)
    while day <= date.fromisoformat(last):
        db.execute("INSERT INTO scan_daily_coverage VALUES(?,?,?) ON CONFLICT(day) DO UPDATE SET articles=excluded.articles,last_scan=excluded.last_scan",
                   (day.isoformat(), counts.get(day.isoformat(), 0), scanned[:10]))
        day += timedelta(days=1)


def export_coverage(db, journal_key, path):
    # Rebuild from durable scan snapshots; never infer fresh counts from partial runs.
    rows = db.execute("SELECT day,articles,last_scan FROM scan_daily_coverage ORDER BY day").fetchall()
    temp = path.with_suffix('.sqlite.tmp')
    if temp.exists():
        temp.unlink()
    coverage = sqlite3.connect(temp)
    try:
        coverage.execute("CREATE TABLE daily_coverage (day TEXT PRIMARY KEY, articles INTEGER NOT NULL CHECK(articles>=0), last_scan TEXT NOT NULL)")
        coverage.executemany("INSERT INTO daily_coverage VALUES(?,?,?)", rows)
        coverage.commit()
    finally:
        coverage.close()
    temp.replace(path)


def split_range(first, last, size):
    """Newest interval first, without gaps or overlapping endpoints."""
    while last >= first:
        start = max(first, last - timedelta(days=size - 1))
        yield start, last
        last = start - timedelta(days=1)


def plan_intervals(db, archive_start, yesterday, size, first=None, last=None):
    if first:
        lower, upper = date.fromisoformat(first), date.fromisoformat(last or first)
        if lower > upper or upper > yesterday:
            raise ValueError("Range must be ordered and end no later than yesterday UTC")
        return list(split_range(lower, upper, size))
    # Refresh the most recent block to detect late arrivals.
    recent_start = max(archive_start, yesterday - timedelta(days=size - 1))
    candidates = [(recent_start, yesterday)]
    covered = {date.fromisoformat(row[0]) for row in db.execute("SELECT day FROM day_coverage WHERE status='ok'")}
    for start, end in db.execute("SELECT from_date,to_date FROM interval_coverage WHERE status='ok'"):
        d = date.fromisoformat(start)
        while d <= date.fromisoformat(end):
            covered.add(d)
            d += timedelta(days=1)
    # The current refresh block is already scheduled; group contiguous historical holes.
    missing_end = recent_start - timedelta(days=1)
    while missing_end >= archive_start:
        if missing_end in covered:
            missing_end -= timedelta(days=1)
            continue
        start = missing_end
        while (start - timedelta(days=1) >= archive_start
               and start - timedelta(days=1) not in covered
               and (missing_end - start).days + 1 < size):
            start -= timedelta(days=1)
        candidates.append((start, missing_end))
        missing_end = start - timedelta(days=1)
    # Least recently attempted complete historical blocks form the reconciliation queue.
    historical = db.execute("SELECT from_date,to_date FROM interval_coverage WHERE status='ok' "
                            "AND to_date<? ORDER BY last_attempt_utc,from_date", (recent_start.isoformat(),))
    for start, end in historical:
        pair = (date.fromisoformat(start), date.fromisoformat(end))
        if pair not in candidates:
            candidates.append(pair)
    # Legacy daily coverage is preserved; revisit it in blocks after filling holes.
    for pair in split_range(archive_start, recent_start - timedelta(days=1), size):
        if pair not in candidates:
            candidates.append(pair)
    return candidates


def run(args):
    config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    journal = config["journals"][args.journal]
    key = os.environ.get("OPENALEX_API_KEY", "").strip()
    if not key or key.startswith("XXXX"):
        raise ValueError("Set OPENALEX_API_KEY in the environment")
    directory = ROOT / "library"
    directory.mkdir(exist_ok=True)
    db = sqlite3.connect(directory / (args.journal + ".sqlite"))
    init_db(db)
    run_id, started = str(uuid.uuid4()), now()
    db.execute("INSERT INTO query_runs(id,started_utc,status,max_intervals,max_requests) VALUES(?,?,'running',?,?)",
               (run_id, started, args.max_intervals, args.max_requests))
    db.commit()
    client = Client(db, run_id, key, config, args.max_requests, time.monotonic() + args.max_minutes * 60)
    completed, days_completed, status, reason = 0, 0, "ok", None
    try:
        today = datetime.now(timezone.utc).date()
        yesterday = today - timedelta(days=1)
        state = db.execute("SELECT value FROM collector_state WHERE key='archive_start'").fetchone()
        if state:
            archive_start = date.fromisoformat(state[0])
        else:
            payload = client.fetch({"filter": journal_filter(journal), "sort": "publication_date:asc", "per_page": 1})
            records = payload.get("results", [])
            if not records:
                raise RuntimeError("No articles found for configured journal")
            archive_start = date.fromisoformat(records[0]["publication_date"])
            db.execute("INSERT INTO collector_state VALUES('archive_start',?)", (archive_start.isoformat(),))
            db.commit()
        candidates = plan_intervals(db, archive_start, yesterday, args.interval_days,
                                    args.from_date, args.to_date)
        days_completed = 0
        if not args.from_date:
            # Today's publications are useful immediately, but remain outside coverage.
            collect_interval(db, client, journal, today.isoformat(), today.isoformat(), provisional=True)
            print(json.dumps({"from_date": today.isoformat(), "to_date": today.isoformat(),
                              "status": "partial"}), flush=True)
        for first, last in candidates[:args.max_intervals]:
            collect_interval(db, client, journal, first.isoformat(), last.isoformat())
            completed += 1
            days_completed += (last - first).days + 1
            print(json.dumps({"from_date": first.isoformat(), "to_date": last.isoformat(), "status": "ok"}), flush=True)
    except StopCollection as error:
        status, reason = "stopped", str(error)
    except Exception as error:
        status, reason = "error", str(error)
    finally:
        db.execute("UPDATE query_runs SET finished_utc=?,status=?,reason=?,attempts=?,"
                   "days_completed=?,reported_cost_usd=?,intervals_completed=? WHERE id=?",
                   (now(), status, reason, client.attempts, days_completed, client.cost, completed, run_id))
        db.commit()
        export_coverage(db, args.journal, directory / (args.journal + ".coverage.sqlite"))
        db.close()
    print(json.dumps({"run_id": run_id, "status": status, "reason": reason,
                      "intervals_completed": completed, "days_completed": days_completed, "attempts": client.attempts,
                      "reported_cost_usd": client.cost}), flush=True)
    return 1 if status == "error" else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", required=True, choices=list(json.loads((ROOT / "config.json").read_text(encoding="utf-8"))["journals"]))
    parser.add_argument("--max-intervals", type=int, default=2)
    parser.add_argument("--interval-days", type=int, default=120)
    parser.add_argument("--max-requests", type=int, default=100)
    parser.add_argument("--max-minutes", type=float, default=25)
    parser.add_argument("--from-date")
    parser.add_argument("--to-date")
    args = parser.parse_args()
    if min(args.max_intervals, args.interval_days, args.max_requests, args.max_minutes) <= 0:
        parser.error("Resource limits must be positive")
    if args.to_date and not args.from_date:
        parser.error("--to-date requires --from-date")
    raise SystemExit(run(args))

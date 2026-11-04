#!/usr/bin/env python3
"""Manual 30-day A&A metadata capture; Crossmark metadata uses Crossref REST."""
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
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ISSN = '0004-6361'


def window(today=None):
    yesterday = (today or datetime.now(timezone.utc).date()) - timedelta(days=1)
    return yesterday - timedelta(days=29), yesterday


def crossmark_record(item):
    keys = ['DOI', 'title', 'type', 'ISSN', 'published', 'published-online',
            'published-print', 'created', 'deposited', 'indexed', 'URL',
            'update-policy', 'update-to', 'updated-by', 'assertion', 'relation',
            'content-domain', 'clinical-trial-number']
    result = {k: item[k] for k in keys if k in item}
    result['interpretation'] = 'Reported metadata only; absence does not establish an unchanged or unretracted status.'
    return result


class Client:
    def __init__(self, mailto, max_requests=100):
        self.mailto = mailto
        self.limit = max_requests
        self.attempts = 0
        self.last_finished = 0

    def fetch(self, params):
        params = {**params, 'mailto': self.mailto}
        for retry in range(4):
            if self.attempts >= self.limit:
                raise RuntimeError('HTTP attempt limit reached; capture remains incomplete')
            time.sleep(max(0, 1 - (time.monotonic() - self.last_finished)))
            self.attempts += 1
            request = urllib.request.Request(
                'https://api.crossref.org/journals/' + ISSN + '/works?' + urllib.parse.urlencode(params),
                headers={'User-Agent': 'david-openalex-crossref/1.0 (mailto:' + self.mailto + ')',
                         'Accept': 'application/json'})
            delay = 2 ** (retry + 1)
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    data = json.load(response)
                if data.get('status') != 'ok' or not isinstance(data.get('message'), dict):
                    raise ValueError('Invalid Crossref envelope')
                return data['message']
            except urllib.error.HTTPError as error:
                if error.code != 429 and not 500 <= error.code < 600:
                    raise RuntimeError('Crossref HTTP ' + str(error.code)) from None
                after = error.headers.get('Retry-After')
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
                raise RuntimeError('Crossref retries exhausted')
            if delay > 300:
                raise RuntimeError('Server requires extended wait; retry capture later')
            time.sleep(delay)


def run(mode, max_requests=100):
    mailto = os.environ.get(('CROSSMARK' if mode == 'crossmark' else 'CROSSREF') + '_MAILTO', '').strip()
    if '@' not in mailto or '\n' in mailto or '\r' in mailto:
        raise ValueError('Configure the contact email secret for this workflow')
    start, end = window()
    query = f'type:journal-article,from-pub-date:{start},until-pub-date:{end}'
    directory = ROOT / 'library' / mode
    directory.mkdir(exist_ok=True)
    db = sqlite3.connect(directory / 'aanda.sqlite')
    db.executescript('''
    CREATE TABLE IF NOT EXISTS papers (
      doi TEXT PRIMARY KEY, title TEXT, raw_json TEXT NOT NULL, last_scan_utc TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS runs (
      id TEXT PRIMARY KEY, mode TEXT, issn TEXT, from_date TEXT, to_date TEXT,
      query_filter TEXT, started_utc TEXT, finished_utc TEXT, status TEXT,
      expected INTEGER, retrieved INTEGER DEFAULT 0, requests INTEGER DEFAULT 0, error TEXT);
    ''')
    identifier = str(uuid.uuid4())
    stamp = lambda: datetime.now(timezone.utc).isoformat()
    db.execute('INSERT INTO runs(id,mode,issn,from_date,to_date,query_filter,started_utc,status) VALUES(?,?,?,?,?,?,?,?)',
               (identifier, mode, ISSN, str(start), str(end), query, stamp(), 'running'))
    db.commit()
    client, seen, cursors = Client(mailto, max_requests), set(), set()
    cursor, expected, status, error = '*', None, 'ok', None
    try:
        while True:
            if cursor in cursors:
                raise RuntimeError('Repeated cursor; capture incomplete')
            cursors.add(cursor)
            message = client.fetch({'filter': query, 'rows': 1000, 'cursor': cursor})
            items, total = message.get('items'), message.get('total-results')
            if not isinstance(items, list) or not isinstance(total, int):
                raise RuntimeError('Invalid Crossref list response')
            if expected is None:
                expected = total
            if expected != total:
                raise RuntimeError('Total changed during capture; repeat the window')
            with db:
                for item in items:
                    doi = item.get('DOI', '').lower()
                    if not doi or item.get('type') != 'journal-article' or ISSN not in item.get('ISSN', []):
                        raise RuntimeError('Record contradicts requested ISSN/type')
                    if doi in seen:
                        raise RuntimeError('Repeated DOI across pages')
                    seen.add(doi)
                    record = crossmark_record(item) if mode == 'crossmark' else item
                    db.execute('INSERT INTO papers VALUES(?,?,?,?) ON CONFLICT(doi) DO UPDATE SET title=excluded.title,raw_json=excluded.raw_json,last_scan_utc=excluded.last_scan_utc',
                               (doi, ' '.join(item.get('title', [])), json.dumps(record, ensure_ascii=False), stamp()))
            print(json.dumps({'mode': mode, 'retrieved': len(seen), 'total': expected}), flush=True)
            if len(seen) == expected:
                break
            if not items or len(seen) > expected or not message.get('next-cursor'):
                raise RuntimeError('Incomplete or inconsistent pagination')
            cursor = message['next-cursor']
    except Exception as exc:
        status, error = 'error', str(exc)
    finally:
        db.execute('UPDATE runs SET finished_utc=?,status=?,expected=?,retrieved=?,requests=?,error=? WHERE id=?',
                   (stamp(), status, expected, len(seen), client.attempts, error, identifier))
        db.commit()
        db.close()
    print(json.dumps({'mode': mode, 'issn': ISSN, 'from_date': str(start), 'to_date': str(end),
                      'status': status, 'retrieved': len(seen), 'requests': client.attempts, 'error': error}), flush=True)
    return 0 if status == 'ok' else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=['crossref', 'crossmark'], required=True)
    parser.add_argument('--max-requests', type=int, default=100)
    args = parser.parse_args()
    if args.max_requests < 1:
        parser.error('--max-requests must be positive')
    raise SystemExit(run(args.mode, args.max_requests))

import importlib.util
import sqlite3
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location('collector', Path(__file__).resolve().parents[1] / 'scripts/collect_openalex_journal.py')
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)


def work(identifier):
    return {'id': 'https://openalex.org/W' + identifier, 'type': 'article',
            'publication_date': '2026-10-01', 'title': 'Example',
            'primary_location': {'source': {'id': 'https://openalex.org/S205231332'}}}


class FakeClient:
    config = {'per_page': 100}

    def __init__(self, pages):
        self.pages = iter(pages)

    def fetch(self, params):
        result = next(self.pages)
        if isinstance(result, Exception):
            raise result
        return result


def page(records, total, cursor=None):
    return {'meta': {'count': total, 'next_cursor': cursor}, 'results': records}


class CollectorTests(unittest.TestCase):
    def test_daily_coverage_includes_zeroes_and_survives_partial_refresh(self):
        self.collect([page([work('1')], 1)])
        before = self.db.execute('SELECT * FROM scan_daily_coverage ORDER BY day').fetchall()
        self.assertEqual(len(before), 30)
        self.assertEqual(before[0][1], 1)
        self.assertTrue(all(row[1] == 0 for row in before[1:]))
        with self.assertRaises(c.StopCollection):
            self.collect([page([work('2')], 2, 'next'), c.StopCollection('limit')])
        self.assertEqual(before, self.db.execute('SELECT * FROM scan_daily_coverage ORDER BY day').fetchall())

    def test_legacy_conversion_keeps_completed_scan_date(self):
        self.db.execute("DELETE FROM collector_state WHERE key='daily_coverage_migrated'")
        self.db.execute("INSERT INTO day_coverage(day,query_filter,status,last_completed_utc) VALUES('2026-09-01','legacy','ok','2026-10-10T12:00:00Z')")
        c.init_daily_coverage(self.db)
        self.assertEqual(self.db.execute('SELECT * FROM scan_daily_coverage').fetchall(),
                         [('2026-09-01', 0, '2026-10-10')])

    def test_thirty_day_blocks_have_no_gaps(self):
        first, last = c.date(2026, 9, 1), c.date(2026, 10, 31)
        blocks = list(c.split_range(first, last, 30))
        days = [start + c.timedelta(days=i) for start, end in blocks
                for i in range((end - start).days + 1)]
        self.assertEqual(len(days), 61)
        self.assertEqual(len(set(days)), 61)
        self.assertEqual(min(days), first)
        self.assertEqual(max(days), last)

    def test_dates_are_preserved_across_interval_pages(self):
        second = work('2')
        second['publication_date'] = '2026-10-30'
        self.collect([page([work('1')], 2, 'next'), page([second], 2)])
        dates = self.db.execute('SELECT publication_date FROM papers ORDER BY id').fetchall()
        self.assertEqual(dates, [('2026-10-01',), ('2026-10-30',)])

    def test_legacy_coverage_preserved_and_holes_planned(self):
        self.db.execute("INSERT INTO day_coverage(day,query_filter,status) VALUES('2026-09-30','legacy','ok')")
        blocks = c.plan_intervals(self.db, c.date(2026,9,1), c.date(2026,10,31), 30)
        self.assertEqual(blocks[0], (c.date(2026,10,2), c.date(2026,10,31)))
        self.assertEqual(blocks[1], (c.date(2026,10,1), c.date(2026,10,1)))
        self.assertEqual(self.db.execute('SELECT status FROM day_coverage').fetchone()[0], 'ok')

    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        c.init_db(self.db)
        self.journal = {'source_id': 'S205231332'}

    def tearDown(self):
        self.db.close()

    def collect(self, pages):
        c.collect_interval(self.db, FakeClient(pages), self.journal, '2026-10-01', '2026-10-30')

    def status(self):
        return self.db.execute('SELECT status FROM interval_coverage').fetchone()[0]

    def test_multiple_pages_and_idempotent_refresh(self):
        for _ in range(2):
            self.collect([page([work('1')], 2, 'next'), page([work('2')], 2, 'end'), page([], 2)])
        self.assertEqual(self.status(), 'ok')
        self.assertEqual(self.db.execute('SELECT count(*) FROM papers').fetchone()[0], 2)

    def test_empty_day_is_covered(self):
        self.collect([page([], 0)])
        self.assertEqual(self.status(), 'ok')

    def test_current_day_is_stored_without_coverage(self):
        item = work('today')
        item['publication_date'] = '2026-11-05'
        c.collect_interval(self.db, FakeClient([page([item], 1)]), self.journal,
                           '2026-11-05', '2026-11-05', provisional=True)
        self.assertEqual(self.db.execute('SELECT count(*) FROM papers').fetchone()[0], 1)
        self.assertEqual(self.status(), 'partial')
        self.assertEqual(self.db.execute('SELECT count(*) FROM scan_daily_coverage').fetchone()[0], 0)

    def test_interruption_keeps_page_and_restart_recovers(self):
        with self.assertRaises(c.StopCollection):
            self.collect([page([work('1')], 2, 'next'), c.StopCollection('request_limit')])
        self.assertEqual(self.status(), 'error')
        self.collect([page([work('1'), work('2')], 2)])
        self.assertEqual(self.status(), 'ok')
        self.assertEqual(self.db.execute('SELECT count(*) FROM papers').fetchone()[0], 2)

    def test_incomplete_count_is_not_covered(self):
        with self.assertRaises(RuntimeError):
            self.collect([page([work('1')], 2)])
        self.assertEqual(self.status(), 'error')

    def test_duplicate_and_wrong_source_rejected(self):
        with self.assertRaises(RuntimeError):
            self.collect([page([work('1')], 2, 'next'), page([work('1')], 2)])
        wrong = work('2')
        wrong['primary_location']['source']['id'] = 'https://openalex.org/S0'
        with self.assertRaises(RuntimeError):
            self.collect([page([wrong], 1)])


if __name__ == '__main__':
    unittest.main()

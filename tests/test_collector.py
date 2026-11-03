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
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        c.init_db(self.db)
        self.journal = {'source_id': 'S205231332'}

    def tearDown(self):
        self.db.close()

    def collect(self, pages):
        c.collect_day(self.db, FakeClient(pages), self.journal, '2026-10-01')

    def status(self):
        return self.db.execute('SELECT status FROM day_coverage').fetchone()[0]

    def test_multiple_pages_and_idempotent_refresh(self):
        for _ in range(2):
            self.collect([page([work('1')], 2, 'next'), page([work('2')], 2, 'end'), page([], 2)])
        self.assertEqual(self.status(), 'ok')
        self.assertEqual(self.db.execute('SELECT count(*) FROM papers').fetchone()[0], 2)

    def test_empty_day_is_covered(self):
        self.collect([page([], 0)])
        self.assertEqual(self.status(), 'ok')

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

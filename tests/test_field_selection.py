import json
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from field_selection import selected_work, migrate_papers


class SelectionTests(unittest.TestCase):
    def test_nested_projection_and_identifier_rules(self):
        work = {'unapproved': 'drop', 'primary_location': {
            'doi': 'https://doi.org/10.1/x', 'landing_page_url': 'https://doi.org/10.1/x',
            'id': 'doi:10.1/x', 'source': {'id': 'https://openalex.org/S1', 'issn': ['drop']}},
            'authorships': [{'author': {'id': 'https://openalex.org/A1', 'display_name': 'drop'}}],
            'topics': [{'id': 'https://openalex.org/T1', 'display_name': 'Topic',
                        'field': {'id': 'https://openalex.org/fields/31', 'display_name': 'Physics'}}],
            'referenced_works': ['https://openalex.org/W2'], 'best_oa_location': None,
            'content_urls': None}
        result = selected_work(work)
        self.assertNotIn('unapproved', result)
        self.assertEqual(result['primary_location']['doi'], '10.1/x')
        self.assertEqual(result['primary_location']['landing_page_url'], 'https://doi.org/10.1/x')
        self.assertEqual(result['primary_location']['id'], 'doi:10.1/x')
        self.assertEqual(result['primary_location']['source'], {'id': 'S1'})
        self.assertEqual(result['authorships'], [{'author': {'id': 'A1'}}])
        self.assertEqual(result['topics'], [{'display_name': 'Topic', 'field': {'display_name': 'Physics'}}])
        self.assertEqual(result['referenced_works'], ['W2'])
        self.assertIsNone(result['best_oa_location'])
        self.assertEqual(selected_work(result), result)

    def test_migration_is_idempotent(self):
        db = sqlite3.connect(':memory:')
        db.execute('CREATE TABLE papers(id TEXT PRIMARY KEY, doi TEXT, source_id TEXT, raw_json TEXT)')
        db.execute('INSERT INTO papers VALUES(?,?,?,?)', ('https://openalex.org/W1',
                   'https://doi.org/10.1/x', 'https://openalex.org/S1', json.dumps({'type': 'article', 'extra': 1})))
        db.commit()
        migrate_papers(db)
        migrate_papers(db)
        row = db.execute('SELECT * FROM papers').fetchone()
        self.assertEqual(row[:3], ('W1', '10.1/x', 'S1'))
        self.assertEqual(json.loads(row[3]), {'type': 'article'})
        db.close()

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class WorkflowTests(unittest.TestCase):
    def test_all_journals_have_requested_schedule_and_own_storage(self):
        journals = json.loads((ROOT / 'config.json').read_text(encoding='utf-8'))['journals']
        self.assertEqual(len(journals), 20)
        self.assertEqual(len(list((ROOT / '.github/workflows').glob('openalex-*-collector.yml'))), 20)
        for index, (code, journal) in enumerate(journals.items()):
            with self.subTest(journal=code):
                text = (ROOT / f'.github/workflows/openalex-{code}-collector.yml').read_text(encoding='utf-8')
                self.assertEqual(text.splitlines()[0], f"name: 0 {journal['name']} OA")
                cron = re.search(r"cron: '([^']+)'", text).group(1)
                minute, hours, *rest = cron.split()
                offset = 180 + index * 10
                self.assertEqual(int(minute), offset % 60)
                self.assertEqual([int(h) for h in hours.split(',')],
                                 [h for h in range(24) if h % 4 == (offset // 60) % 4])
                self.assertEqual(rest, ['*', '*', '*'])
                self.assertEqual(cron, journal['cron_utc'])
                self.assertIn(f'--journal {code} ', text)
                self.assertIn(f'library/{code}.db', text)
                self.assertIn(f'library/{code}.coverage.db', text)
                self.assertIn("inputs.max_intervals || '2'", text)
                self.assertIn("inputs.max_requests || '100'", text)
                self.assertIn('group: openalex-collectors', text)

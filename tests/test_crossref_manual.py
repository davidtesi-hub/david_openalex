import importlib.util
from pathlib import Path
import unittest
from datetime import date

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('crossref_manual', ROOT / 'scripts/collect_crossref_aanda.py')
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)


class CrossrefManualTests(unittest.TestCase):
    def test_window_is_thirty_inclusive_days_ending_yesterday(self):
        self.assertEqual(c.window(date(2026, 11, 4)), (date(2026, 10, 5), date(2026, 11, 3)))
        start, end = c.window(date(2024, 3, 1))
        self.assertEqual((end - start).days + 1, 30)
        self.assertEqual(end, date(2024, 2, 29))

    def test_crossmark_preserves_update_evidence_without_inventing_status(self):
        item = {'DOI': '10.1/x', 'update-to': [{'DOI': '10.1/y', 'type': 'correction'}],
                'reference': [{'DOI': '10.1/z'}]}
        result = c.crossmark_record(item)
        self.assertEqual(result['update-to'], item['update-to'])
        self.assertNotIn('reference', result)
        self.assertNotIn('is_retracted', result)

    def test_workflows_manual_only(self):
        for mode in ['crossref', 'crossmark']:
            text = (ROOT / f'.github/workflows/{mode}-aanda-manual.yml').read_text(encoding='utf-8')
            self.assertIn('workflow_dispatch:', text)
            self.assertNotIn('schedule:', text)
            self.assertIn('--mode ' + mode, text)
            self.assertIn(f'library/{mode}/aanda.sqlite', text)

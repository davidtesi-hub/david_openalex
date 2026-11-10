import importlib.util
import json
from datetime import date
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("crossref_collector", ROOT / "scripts/collect_crossref_journal.py")
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)
gen_spec = importlib.util.spec_from_file_location("crossref_workflows", ROOT / "scripts/generate_crossref_workflows.py")
generator = importlib.util.module_from_spec(gen_spec)
gen_spec.loader.exec_module(generator)


class CrossrefCollectorTests(unittest.TestCase):
    def test_370_inclusive_days_split_without_gaps(self):
        start, end = collector.window(date(2026, 11, 5))
        parts = list(collector.intervals(start, end))
        self.assertEqual((start.isoformat(), end.isoformat()), ("2025-10-31", "2026-11-04"))
        self.assertEqual(sum((right - left).days + 1 for left, right in parts), 370)
        self.assertEqual(len(parts), 13)
        self.assertEqual(parts[0][1], end)
        self.assertEqual(parts[-1][0], start)
        for (newer_start, _), (_, older_end) in zip(parts, parts[1:]):
            self.assertEqual((newer_start - older_end).days, 1)

    def test_dates_and_update_status_are_stored(self):
        sample = {"DOI": "10.1/X", "title": ["Sample"], "type": "journal-article",
                  "ISSN": ["0004-6361"], "published-online": {"date-parts": [[2026, 11, 1]]},
                  "issued": {"date-parts": [[2026, 11]]},
                  "updated-by": [{"DOI": "10.1/correction", "type": "correction",
                                   "updated": {"date-parts": [[2026, 11, 2]]}}]}

        class FakeClient:
            issn = "0004-6361"

            def fetch(self, params):
                return {"items": [sample], "total-results": 1}

        with tempfile.TemporaryDirectory() as directory:
            db = collector.open_db(Path(directory) / "aanda.sqlite")
            collector.capture_interval(db, FakeClient(), date(2026, 11, 1), date(2026, 11, 1))
            stored = db.execute("SELECT published_online,issued,status,updates_json FROM papers WHERE doi='10.1/x'").fetchone()
            self.assertEqual(stored, ("2026-11-01", "2026-11", "updated",
                '[{"direction":"updated-by","doi":"10.1/correction","type":"correction","label":null,"date":"2026-11-02"}]'))
            self.assertEqual(db.execute("SELECT expected,retrieved FROM interval_coverage").fetchone(), (1, 1))
            db.close()

    def test_current_day_is_stored_without_coverage(self):
        sample = {"DOI": "10.1/today", "type": "journal-article", "ISSN": ["0004-6361"],
                  "published-online": {"date-parts": [[2026, 11, 5]]}}

        class FakeClient:
            issn = "0004-6361"

            def fetch(self, params):
                return {"items": [sample], "total-results": 1}

        with tempfile.TemporaryDirectory() as directory:
            db = collector.open_db(Path(directory) / "aanda.sqlite")
            collector.capture_interval(db, FakeClient(), date(2026, 11, 5),
                                       date(2026, 11, 5), provisional=True)
            self.assertEqual(db.execute('SELECT count(*) FROM papers').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT count(*) FROM interval_coverage').fetchone()[0], 0)
            db.close()

    def test_workflows_match_all_journals_and_shifted_cron(self):
        journals = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))["journals"]
        self.assertEqual(len(journals), 20)
        for code, info in journals.items():
            self.assertTrue(info["crossref_issn"])
            mode = "crossref"
            text = (ROOT / f".github/workflows/{mode}-{code}-collector.yml").read_text(encoding="utf-8")
            self.assertEqual(text.splitlines()[0], f"name: 0 {info['name']} CR")
            self.assertIn(f"cron: '{generator.shifted_cron(info['cron_utc'], 3)}'", text)
            self.assertIn("workflow_dispatch:", text)
            self.assertIn(f"run-{mode}-{code}-collector", text)
            self.assertIn(f"--journal {code} --mode {mode}", text)
            self.assertIn(f"library/{mode}/{code}.sqlite", text)
            self.assertNotIn("upload-artifact", text)


if __name__ == "__main__":
    unittest.main()

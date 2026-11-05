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

    def test_full_provider_payload_is_stored_for_both_modes(self):
        sample = {"DOI": "10.1/X", "title": ["Sample"], "type": "journal-article",
                  "ISSN": ["0004-6361"], "published-online": {"date-parts": [[2026, 11, 1]]},
                  "assertion": [{"name": "acceptance_date"}], "custom-field": {"nested": True}}

        class FakeClient:
            issn = "0004-6361"

            def fetch(self, params):
                return {"items": [sample], "total-results": 1}

        with tempfile.TemporaryDirectory() as directory:
            db = collector.open_db(Path(directory) / "aanda.sqlite")
            collector.capture_interval(db, FakeClient(), date(2026, 11, 1), date(2026, 11, 1))
            stored = collector.unpack_record(db.execute("SELECT raw_json_zlib FROM papers WHERE doi='10.1/x'").fetchone()[0])
            self.assertEqual(stored, sample)
            self.assertLess(len(collector.pack_record(sample)), len(json.dumps(sample).encode()))
            self.assertEqual(db.execute("SELECT expected,retrieved FROM interval_coverage").fetchone(), (1, 1))
            db.close()

    def test_legacy_aanda_rows_are_migrated_without_losing_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "aanda.sqlite"
            import sqlite3
            old = sqlite3.connect(path)
            old.execute("CREATE TABLE papers(doi TEXT PRIMARY KEY,title TEXT,raw_json TEXT,last_scan_utc TEXT)")
            raw = {"DOI": "10.1/x", "extra": {"author": ["A", "B"]}}
            old.execute("INSERT INTO papers VALUES(?,?,?,?)", ("10.1/x", "Sample", json.dumps(raw), "2026-11-04"))
            old.commit(); old.close()
            new = collector.open_db(path)
            stored = collector.unpack_record(new.execute("SELECT raw_json_zlib FROM papers").fetchone()[0])
            self.assertEqual(stored, raw)
            new.close()

    def test_workflows_match_all_journals_and_shifted_cron(self):
        journals = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))["journals"]
        self.assertEqual(len(journals), 20)
        for code, info in journals.items():
            self.assertTrue(info["crossref_issn"])
            for mode, offset in (("crossref", 3), ("crossmark", 7)):
                text = (ROOT / f".github/workflows/{mode}-{code}-collector.yml").read_text(encoding="utf-8")
                self.assertIn(f"cron: '{generator.shifted_cron(info['cron_utc'], offset)}'", text)
                self.assertIn("workflow_dispatch:", text)
                self.assertIn(f"run-{mode}-{code}-collector", text)
                self.assertIn(f"--journal {code} --mode {mode}", text)
                self.assertIn(f"library/{mode}/{code}.sqlite", text)


if __name__ == "__main__":
    unittest.main()

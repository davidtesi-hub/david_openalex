import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.library_storage import pack, restore


class LibraryStorageTests(unittest.TestCase):
    def test_round_trip_and_corrupt_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.sqlite"
            db = sqlite3.connect(path)
            try:
                db.execute("CREATE TABLE papers (id TEXT PRIMARY KEY)")
                db.execute("INSERT INTO papers VALUES ('W1')")
                db.commit()
            finally:
                db.close()
            archive = pack(path)
            self.assertTrue(archive.exists())
            path.unlink()
            restore(path)
            db = sqlite3.connect(path)
            try:
                self.assertEqual(db.execute("SELECT id FROM papers").fetchone(), ("W1",))
            finally:
                db.close()
            path.unlink()
            archive.write_bytes(b"invalid zlib")
            with self.assertRaises(Exception):
                restore(path)
            self.assertFalse(path.exists())

    def test_empty_newer_local_file_is_restored(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.sqlite"
            db = sqlite3.connect(path)
            db.execute("CREATE TABLE papers (id TEXT)")
            db.close()
            pack(path)
            path.write_bytes(b"")
            restore(path)
            self.assertEqual(path.read_bytes()[:16], b"SQLite format 3\x00")

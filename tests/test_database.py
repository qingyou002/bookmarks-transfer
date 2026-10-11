import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from browser_transfer.database import merge_autofill, merge_history
from browser_transfer.models import MigrationError
from .fixtures import addresses, history


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="browser-transfer-test-")
        self.root = Path(self.tmp.name) / "中文目录"
        self.source, self.target = self.root / "source", self.root / "target"

    def tearDown(self):
        self.tmp.cleanup()

    def query(self, path, sql):
        with closing(sqlite3.connect(path)) as db, db:
            return db.execute(sql).fetchall()

    def test_history_repeated_and_reverse_preserve_times_and_relationships(self):
        source, target = self.source / "History", self.target / "History"
        epoch = 13404000000000123
        history(source, [(1, "https://a.example/", "甲", 1, 1, epoch, 0),
                         (2, "https://b.example/", "乙", 1, 0, epoch + 11, 0)],
                [(10, 1, epoch, 0, 1, 90, 50, 0), (11, 2, epoch + 11, 10, 0, 90, 70, 10)])
        history(target, [(77, "https://target.example/", "目标独有", 1, 0, epoch - 20, 0)],
                [(99, 77, epoch - 20, 0, 0, 0, 0, 0)])
        counts = merge_history(source, target)
        self.assertEqual(counts.added, 2)
        rows = self.query(target, "SELECT id,visit_time,from_visit,opener_visit,segment_id,visited_link_id FROM visits ORDER BY id")
        self.assertEqual(rows[1][1], epoch)
        self.assertEqual(rows[2][1], epoch + 11)
        self.assertEqual(rows[2][2:4], (rows[1][0], rows[1][0]))
        self.assertEqual(rows[1][4:], (0, 0))
        snapshot = self.query(target, "SELECT * FROM urls ORDER BY id")
        second = merge_history(source, target)
        self.assertEqual((second.added, second.skipped), (0, 2))
        self.assertEqual(self.query(target, "SELECT * FROM urls ORDER BY id"), snapshot)
        reversed_counts = merge_history(target, source)
        self.assertEqual((reversed_counts.added, reversed_counts.skipped), (1, 2))
        self.assertEqual(self.query(source, "SELECT count(*) FROM visits"), [(3,)])
        self.assertEqual(self.query(target, "PRAGMA integrity_check"), [("ok",)])

    def test_existing_url_only_increments_new_visits(self):
        source, target = self.source / "History", self.target / "History"
        url = (1, "https://same.example/", "同一网址", 2, 0, 102, 0)
        history(source, [url], [(1, 1, 101, 0, 0, 0, 0, 0), (2, 1, 102, 1, 0, 0, 0, 0)])
        history(target, [(8, *url[1:3], 1, 0, 101, 0)], [(7, 8, 101, 0, 0, 0, 0, 0)])
        self.assertEqual(merge_history(source, target).added, 1)
        self.assertEqual(self.query(target, "SELECT visit_count FROM urls"), [(2,)])
        merge_history(source, target)
        self.assertEqual(self.query(target, "SELECT visit_count FROM urls"), [(2,)])

    def test_history_bad_relationship_url_rolls_back(self):
        source, target = self.source / "History", self.target / "History"
        history(source, [(1, "https://ok.example/", "", 1, 0, 1, 0)], [(1, 404, 1, 0, 0, 0, 0, 0)])
        history(target)
        with self.assertRaises(MigrationError):
            merge_history(source, target)
        self.assertEqual(self.query(target, "SELECT count(*) FROM urls"), [(0,)])

    def test_unknown_required_schema_field_prevents_write(self):
        source, target = self.source / "History", self.target / "History"
        history(source, [(1, "https://a.example/", "", 1, 0, 1, 0)], [(1, 1, 1, 0, 0, 0, 0, 0)])
        history(target)
        with closing(sqlite3.connect(target)) as db, db:
            db.executescript("DROP TABLE visits; CREATE TABLE visits(id INTEGER PRIMARY KEY,url INTEGER,"
                            "visit_time INTEGER,from_visit INTEGER,transition INTEGER,segment_id INTEGER,"
                            "visit_duration INTEGER,new_required TEXT NOT NULL);")
        with self.assertRaises(MigrationError):
            merge_history(source, target)
        self.assertEqual(self.query(target, "SELECT count(*) FROM urls"), [(0,)])

    def test_guard_failure_before_commit_rolls_back(self):
        source, target = self.source / "History", self.target / "History"
        history(source, [(1, "https://a.example/", "", 1, 0, 1, 0)], [(1, 1, 1, 0, 0, 0, 0, 0)])
        history(target)
        calls = []
        def guard():
            calls.append(True)
            if len(calls) > 1:
                raise MigrationError("browser-restarted")
        with self.assertRaises(MigrationError):
            merge_history(source, target, guard)
        self.assertEqual(self.query(target, "SELECT count(*) FROM visits"), [(0,)])

    def test_locked_target_is_not_modified(self):
        source, target = self.source / "History", self.target / "History"
        history(source)
        history(target)
        db = sqlite3.connect(target)
        db.execute("BEGIN EXCLUSIVE")
        try:
            with self.assertRaises(sqlite3.OperationalError):
                merge_history(source, target)
        finally:
            db.rollback()
            db.close()

    def test_addresses_repeat_reverse_account_conversion_and_no_financial_changes(self):
        source, target = self.source / "Web Data", self.target / "Web Data"
        first = [(7, "王小明"), (9, "me@example.invalid"), (77, "测试路 1 号"), (36, "CN")]
        different = [(7, "王小明"), (77, "测试路 2 号"), (36, "CN")]
        addresses(source, [("edge-account", 1, first)])
        addresses(target, [("chrome-local", 0, different)])
        self.assertEqual(merge_autofill(source, target).added, 1)
        self.assertEqual(merge_autofill(source, target).skipped, 1)
        self.assertEqual(self.query(target, "SELECT record_type FROM addresses"), [(0,), (0,)])
        self.assertEqual(self.query(target, "SELECT card_number_encrypted FROM credit_cards"), [(bytes([1, 2, 3, 4]),)])
        self.assertEqual(self.query(target, "SELECT storage_key FROM autofill_sync_metadata"), [("untouched",)])
        self.assertEqual(merge_autofill(target, source).added, 1)
        self.assertEqual(merge_autofill(target, source).added, 0)

    def test_unknown_nonempty_address_field_is_reported_and_not_copied(self):
        source, target = self.source / "Web Data", self.target / "Web Data"
        addresses(source, [("unsupported", 0, [(7, "测试"), (169, "passport-not-in-scope")])])
        addresses(target)
        self.assertEqual(merge_autofill(source, target).failed, 1)
        self.assertEqual(self.query(target, "SELECT count(*) FROM addresses"), [(0,)])

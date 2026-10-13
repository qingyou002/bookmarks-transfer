import csv
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from browser_transfer.backup import BOOKMARK_FILES, Backup, snapshot_database
from browser_transfer.models import MigrationError, Profile
from browser_transfer.passwords import cleanup, csv_batches
from .fixtures import history


class BackupPasswordTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_restore_keeps_original_and_saves_replaced_state(self):
        profile = Profile("Chrome", self.root / "chrome", "Default")
        profile.path.mkdir(parents=True)
        file = profile.path / "Bookmarks"
        file.write_text('{"original":"中文"}', encoding="utf-8")
        backup = Backup.create(profile, ["Bookmarks", "Bookmarks.bak"], root=self.root / "backups")
        file.write_text('{"changed":true}', encoding="utf-8")
        (profile.path / "Bookmarks.bak").write_text("new", encoding="utf-8")
        loaded = Backup.load(backup.directory)
        loaded.restore_files()
        self.assertEqual(json.loads(file.read_text(encoding="utf-8")), {"original": "中文"})
        self.assertFalse((profile.path / "Bookmarks.bak").exists())

    def test_tampered_backup_refuses_restore(self):
        profile = Profile("Edge", self.root / "edge", "Default")
        profile.path.mkdir(parents=True)
        file = profile.path / "Bookmarks"
        file.write_text("original", encoding="utf-8")
        backup = Backup.create(profile, ["Bookmarks"], root=self.root / "backups")
        (backup.directory / "Bookmarks").write_text("damaged", encoding="utf-8")
        with self.assertRaises(MigrationError):
            Backup.load(backup.directory)
        self.assertEqual(file.read_text(encoding="utf-8"), "original")

    def test_account_and_encrypted_bookmark_files_restore_as_bytes(self):
        profile = Profile("Chrome", self.root / "chrome", "Default")
        profile.path.mkdir(parents=True)
        original = {"AccountBookmarks": b'{"account":true}', "EncryptedAccountBookmarks2": b"\0encrypted-test\xff"}
        for name, data in original.items():
            (profile.path / name).write_bytes(data)
        backup = Backup.create(profile, list(BOOKMARK_FILES), root=self.root / "backups")
        for name in original:
            (profile.path / name).write_bytes(b"changed")
        (profile.path / "EncryptedBookmarks2").write_bytes(b"new")
        Backup.load(backup.directory).restore_files()
        for name, data in original.items():
            self.assertEqual((profile.path / name).read_bytes(), data)
        self.assertFalse((profile.path / "EncryptedBookmarks2").exists())

    def test_restore_failure_after_replacement_rolls_back_current_file(self):
        profile = Profile("Chrome", self.root / "chrome", "Default")
        profile.path.mkdir(parents=True)
        file = profile.path / "Bookmarks"
        file.write_text("original", encoding="utf-8")
        backup = Backup.create(profile, ["Bookmarks"], root=self.root / "backups")
        file.write_text("current", encoding="utf-8")
        restore = Backup._restore_file

        def fail_after_replace(instance, target, name, info):
            restore(instance, target, name, info)
            if instance is backup:
                raise OSError("simulated failure after replacement")

        with patch.object(Backup, "_restore_file", fail_after_replace):
            with self.assertRaises(OSError):
                backup.restore_files()
        self.assertEqual(file.read_text(encoding="utf-8"), "current")

    def test_wal_snapshot_includes_committed_data(self):
        source, target = self.root / "live.db", self.root / "snapshot.db"
        conn = sqlite3.connect(source)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE entries(value TEXT)")
        conn.commit()
        conn.execute("INSERT INTO entries VALUES('wal-record')")
        conn.commit()
        try:
            snapshot_database(source, target)
            with closing(sqlite3.connect(target)) as dst, dst:
                self.assertEqual(dst.execute("SELECT * FROM entries").fetchall(), [("wal-record",)])
        finally:
            conn.close()

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI")
    def test_cookie_snapshot_is_encrypted_and_recoverable(self):
        profile = Profile("Edge", self.root, "Default")
        profile.path.mkdir()
        backup = Backup.create(profile, [], root=self.root / "backups")
        values = [{"name": "session", "value": "unique-test-secret", "domain": "example.invalid"}]
        backup.save_cookies(values, values)
        raw = (backup.directory / "cookies.dpapi").read_bytes()
        self.assertNotIn(b"unique-test-secret", raw)
        self.assertEqual(backup.read_cookies()["before"], values)
        backup.verify()

    def test_password_csv_quotes_unicode_and_batches(self):
        original = self.root / "outside.csv"
        with original.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["name", "url", "username", "password"])
            for i in range(3001):
                writer.writerow(["测试", f"https://{i}.example.invalid/", "name,with,commas", 'pass"word\nline'])
        directory = self.root / "run-test"
        batches, counts = csv_batches(original, directory)
        self.assertEqual(len(batches), 2)
        self.assertEqual(counts.added, 3001)
        with batches[0].open(encoding="utf-8", newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(rows[0]["password"], 'pass"word\nline')
        self.assertEqual(rows[0]["name"], "测试")
        cleanup(directory)
        self.assertFalse(directory.exists())
        self.assertTrue(original.exists())

    def test_password_missing_headers_generates_no_import(self):
        original = self.root / "bad.csv"
        original.write_text("url,username\nhttps://example.invalid/,user", encoding="utf-8")
        with self.assertRaises(MigrationError):
            csv_batches(original, self.root / "run-bad")
        self.assertFalse(list((self.root / "run-bad").glob("*.csv")))

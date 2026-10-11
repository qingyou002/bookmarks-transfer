import json
import os
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from browser_transfer.backup import Backup
from browser_transfer.bridge import Assembler, Broker, EXTENSION_IDS, _read_exact, chunks, pack_message
from browser_transfer.engine import Engine
from browser_transfer.models import MigrationError, PROJECT, Profile
from .fixtures import addresses, history


class ProtocolTests(unittest.TestCase):
    def test_large_unicode_out_of_order_roundtrip(self):
        payload = {"cookies": [{"value": "测试🙂" * 80000}], "id": "test"}
        frames = chunks(payload)
        self.assertGreater(len(frames), 1)
        assembler = Assembler()
        value = None
        for frame in reversed(frames):
            self.assertLess(len(pack_message(frame)), 1024 * 1024)
            result = assembler.feed(frame)
            if result is not None:
                value = result
        self.assertEqual(value, payload)

    def test_duplicate_and_oversized_frames_rejected(self):
        assembler = Assembler()
        frame = chunks({"value": "x" * 250000})[0]
        self.assertIsNone(assembler.feed(frame))
        with self.assertRaises(MigrationError):
            assembler.feed(frame)
        with self.assertRaises(MigrationError):
            Assembler().feed({"type": "chunk", "key": "x", "index": 0, "total": 10000, "data": ""})

    def test_packaged_extension_ids_match_public_keys(self):
        import base64
        import hashlib
        for browser, expected in EXTENSION_IDS.items():
            manifest = json.loads((PROJECT / "extensions" / browser.lower() / "manifest.json").read_text(encoding="utf-8"))
            digest = hashlib.sha256(base64.b64decode(manifest["key"])).digest()[:16]
            actual = "".join(chr(97 + (byte >> 4)) + chr(97 + (byte & 15)) for byte in digest)
            self.assertEqual(actual, expected)
            self.assertEqual(manifest["incognito"], "not_allowed")

    @unittest.skipUnless(os.name == "nt", "Windows named pipes")
    def test_real_native_host_subprocess_named_pipe_roundtrip(self):
        with tempfile.TemporaryDirectory() as temp:
            profile = Profile("Edge", Path(temp), "Default")
            cancel = threading.Event()
            broker = Broker([profile], cancel)
            process = subprocess.Popen([sys.executable, str(PROJECT / "host_entry.py"),
                                        f"chrome-extension://{EXTENSION_IDS['Edge']}/"],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE)
            errors = []
            def emulate_extension():
                try:
                    assembler = Assembler()
                    while True:
                        size = struct.unpack("<I", _read_exact(process.stdout, 4))[0]
                        request = assembler.feed(json.loads(_read_exact(process.stdout, size)))
                        if request is not None:
                            response = {"id": request["id"], "ok": True, "result": request["payload"]}
                            for frame in chunks(response):
                                raw = pack_message(frame)
                                process.stdin.write(struct.pack("<I", len(raw)) + raw)
                            process.stdin.flush()
                except (EOFError, OSError, ValueError):
                    pass
                except Exception as exc:
                    errors.append(type(exc).__name__)
            try:
                for frame in chunks({"type": "hello", "run": broker.run, "profile": "Default"}):
                    raw = pack_message(frame)
                    process.stdin.write(struct.pack("<I", len(raw)) + raw)
                process.stdin.flush()
                worker = threading.Thread(target=emulate_extension, daemon=True)
                worker.start()
                broker.wait_ready(["Edge"], timeout=10)
                data = {"test": "中文数据🙂" * 70000}
                self.assertEqual(broker.rpc("Edge", "echo", data, timeout=20), data)
                self.assertFalse(errors)
            finally:
                broker.close()
                process.kill()
                process.wait(timeout=10)
                for stream in (process.stdin, process.stdout, process.stderr):
                    stream.close()


class FakeBroker:
    def __init__(self, *_):
        self.run = "test"

    def wait_ready(self, *_):
        pass

    def disconnect(self):
        pass

    def close(self):
        pass

    def rpc(self, browser, operation, payload=None):
        if operation == "bookmarks_export":
            return {"tree": [{"id": "0", "children": []}]}
        if operation == "cookies_export":
            return {"cookies": []}
        if operation == "mappings_export":
            return {"bookmarkMappings": {}}
        if operation in {"bookmarks_import", "cookies_import"}:
            return {"counts": {"added": 0, "updated": 0, "skipped": 0, "failed": 0}}
        return {}


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.edge = Profile("Edge", self.root / "edge", "Default")
        self.chrome = Profile("Chrome", self.root / "chrome", "Default")
        for p in (self.edge, self.chrome):
            p.path.mkdir(parents=True)
            history(p.path / "History")
            addresses(p.path / "Web Data")
        self.patches = [patch("browser_transfer.engine.Broker", FakeBroker),
                        patch("browser_transfer.engine.host_registered", return_value=True),
                        patch("browser_transfer.engine.launch"),
                        patch("browser_transfer.engine.assert_closed"),
                        patch("browser_transfer.engine.RUNTIME", self.root / "runtime"),
                        patch("browser_transfer.bridge.RUNTIME", self.root / "runtime")]
        original_create = Backup.create
        self.patches.append(patch("browser_transfer.engine.Backup.create", side_effect=lambda target, files, **kw:
                                  original_create(target, files, root=self.root / "backups", **kw)))
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def test_both_directions_and_repeat_pipeline(self):
        with closing(sqlite3.connect(self.edge.path / "History")) as db, db:
            db.execute("INSERT INTO urls VALUES(1,'https://edge.example/','edge',1,0,100,0)")
            db.execute("INSERT INTO visits(id,url,visit_time,from_visit,transition,segment_id,visit_duration) "
                       "VALUES(1,1,100,0,0,0,0)")
        with closing(sqlite3.connect(self.chrome.path / "History")) as db, db:
            db.execute("INSERT INTO urls VALUES(10,'https://chrome.example/','chrome',1,0,200,0)")
            db.execute("INSERT INTO visits(id,url,visit_time,from_visit,transition,segment_id,visit_duration) "
                       "VALUES(10,10,200,0,0,0,0)")
        for source, target in ((self.edge, self.chrome), (self.chrome, self.edge)):
            report = Engine(prompt=lambda _: "continue").migrate(source, target, ["history", "autofill", "bookmarks", "cookies"])
            self.assertTrue(all(result["status"] == "success" for result in report["results"].values()), report)
            second = Engine(prompt=lambda _: "continue").migrate(source, target, ["history"])
            self.assertEqual(second["results"]["history"]["counts"]["added"], 0)
            self.assertTrue(Path(report["backup"]).is_dir())
        for p in (self.edge, self.chrome):
            with closing(sqlite3.connect(p.path / "History")) as db, db:
                self.assertEqual(db.execute("SELECT count(*) FROM visits").fetchone()[0], 2)

    def test_cancellation_before_writes_records_unexecuted_projects(self):
        report = Engine(prompt=lambda _: "cancel").migrate(self.edge, self.chrome, ["history", "cookies"])
        self.assertEqual(report["results"]["history"]["status"], "cancelled")
        self.assertIsNone(report["backup"])

    def test_failure_of_one_project_does_not_hide_other_result(self):
        with closing(sqlite3.connect(self.edge.path / "Web Data")) as db, db:
            db.execute("DROP TABLE addresses")
        report = Engine(prompt=lambda _: "continue").migrate(self.edge, self.chrome, ["history", "autofill"])
        self.assertEqual(report["results"]["history"]["status"], "success")
        self.assertEqual(report["results"]["autofill"]["status"], "failed")

    def test_password_skipped_not_reported_as_success(self):
        def prompt(request):
            return "skip" if request["kind"] == "password_export" else "continue"
        report = Engine(prompt=prompt).migrate(self.chrome, self.edge, ["passwords"])
        self.assertEqual(report["results"]["passwords"]["status"], "skipped")
        self.assertFalse(list((self.root / "runtime/tmp").glob("run-*/*.csv")))

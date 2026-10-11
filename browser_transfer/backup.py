from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path

from .database import connect, integrity
from .models import MigrationError, Profile, RUNTIME
from .windows import dpapi, private_dir

ALLOWED_FILES = frozenset({"Bookmarks", "Bookmarks.bak", "History", "Web Data",
                           "Login Data", "Login Data For Account"})
SQLITE_FILES = ALLOWED_FILES - {"Bookmarks", "Bookmarks.bak"}


def write_json(path: Path, data: dict) -> None:
    staged = path.with_name(path.name + ".tmp")
    staged.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(staged, path)


def file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def snapshot_database(source: Path, target: Path) -> None:
    src = connect(source, readonly=True)
    try:
        integrity(src)
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)
            integrity(dst)
        finally:
            dst.close()
    finally:
        src.close()


class Backup:
    def __init__(self, directory: Path, manifest: dict):
        self.directory, self.manifest = directory, manifest

    @classmethod
    def create(cls, target: Profile, files: list[str], *, root: Path = RUNTIME / "backups",
               guard=lambda: None) -> Backup:
        private_dir(root)
        directory = private_dir(root / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]))
        manifest = {"version": 1, "target": target.to_dict(), "files": {},
                    "created": datetime.now().isoformat(timespec="seconds")}
        backup = cls(directory, manifest)
        for name in files:
            if name not in ALLOWED_FILES:
                raise MigrationError("不支持的备份文件。")
            guard()
            original = target.path / name
            info = {"existed": original.is_file()}
            if original.is_file():
                destination = directory / name
                if name in SQLITE_FILES:
                    snapshot_database(original, destination)
                else:
                    shutil.copyfile(original, destination)
                info["sha256"] = file_hash(destination)
            manifest["files"][name] = info
            backup.save()
        backup.save()
        return backup

    @classmethod
    def load(cls, directory: Path) -> Backup:
        data = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        if data.get("version") != 1 or not set(data.get("files", {})) <= ALLOWED_FILES:
            raise MigrationError("备份清单格式不受支持。")
        profile = Profile.from_dict(data["target"])
        if profile.browser not in {"Edge", "Chrome"} or Path(profile.directory).name != profile.directory:
            raise MigrationError("备份配置路径无效。")
        backup = cls(directory, data)
        backup.verify()
        return backup

    def save(self) -> None:
        write_json(self.directory / "manifest.json", self.manifest)

    def save_cookies(self, before: list, incoming: list) -> None:
        # Encrypt snapshots with current-user DPAPI. Never save plaintext cookie JSON.
        blob = json.dumps({"before": before, "incoming": incoming}, ensure_ascii=False).encode()
        file = self.directory / "cookies.dpapi"
        file.write_bytes(dpapi(blob))
        self.manifest["cookies_sha256"] = file_hash(file)
        self.save()

    def read_cookies(self) -> dict | None:
        if "cookies_sha256" not in self.manifest:
            return None
        return json.loads(dpapi((self.directory / "cookies.dpapi").read_bytes(), decrypt=True))

    def verify(self) -> None:
        for name, info in self.manifest["files"].items():
            if info["existed"]:
                file = self.directory / name
                if not file.is_file() or file_hash(file) != info.get("sha256"):
                    raise MigrationError("备份校验失败，未开始恢复。")
                if name in SQLITE_FILES:
                    conn = connect(file, readonly=True)
                    try:
                        integrity(conn)
                    finally:
                        conn.close()
        if "cookies_sha256" in self.manifest:
            file = self.directory / "cookies.dpapi"
            if not file.is_file() or file_hash(file) != self.manifest["cookies_sha256"]:
                raise MigrationError("Cookie 备份校验失败，未开始恢复。")

    def restore_files(self, guard=lambda: None) -> None:
        self.verify()
        profile = Profile.from_dict(self.manifest["target"])
        if not profile.path.is_dir():
            raise MigrationError("备份对应的浏览器配置不存在。")
        # Preserve current state before restoring; undo a partial file replacement on failure.
        rollback = Backup.create(profile, list(self.manifest["files"]), guard=guard)
        replaced = []
        try:
            for name, info in self.manifest["files"].items():
                guard()
                # A replacement can succeed before sidecar cleanup fails; include
                # the current file in rollback even if the operation raises.
                replaced.append(name)
                self._restore_file(profile.path, name, info)
        except BaseException:
            for name in reversed(replaced):
                guard()
                rollback._restore_file(profile.path, name, rollback.manifest["files"][name])
            raise

    def _restore_file(self, target: Path, name: str, info: dict) -> None:
        path = target / name
        if info["existed"]:
            stage = target / (name + ".browser-transfer-restore")
            shutil.copyfile(self.directory / name, stage)
            os.replace(stage, path)
        else:
            path.unlink(missing_ok=True)
        if name in SQLITE_FILES:
            # The snapshot already includes committed WAL contents; stale sidecars must not replay.
            for suffix in ("-wal", "-shm", "-journal"):
                path.with_name(path.name + suffix).unlink(missing_ok=True)

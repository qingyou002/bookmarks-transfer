"""Authenticated Windows named-pipe broker and browser Native Messaging host."""
from __future__ import annotations

import base64
import json
import os
import queue
import struct
import sys
import threading
import time
import uuid
from multiprocessing.connection import Client, Listener
from pathlib import Path

from .backup import write_json
from .models import Cancelled, MigrationError, PROJECT, Profile, RUNTIME
from .windows import private_dir, require_venv

HOST_NAME = "com.local.browser_transfer"
EXTENSION_IDS = {"Edge": "bajkcmfogmcaecohaiconmiokccmfnkp",
                 "Chrome": "gpegieodopedlbcljgjcmjndjciadkla"}
MAX_PAYLOAD = 64 * 1024 * 1024
CHUNK_BYTES = 192 * 1024


def pack_message(message: dict) -> bytes:
    return json.dumps(message, ensure_ascii=True, separators=(",", ":")).encode("utf-8")


def chunks(message: dict) -> list[dict]:
    raw = pack_message(message)
    if len(raw) > MAX_PAYLOAD:
        raise MigrationError("浏览器数据超过单次传输上限，请分项目迁移。")
    parts = [raw[i:i + CHUNK_BYTES] for i in range(0, len(raw), CHUNK_BYTES)]
    key = uuid.uuid4().hex
    return [{"type": "chunk", "key": key, "index": i, "total": len(parts),
             "data": base64.b64encode(part).decode("ascii")} for i, part in enumerate(parts)]


class Assembler:
    def __init__(self):
        self.pending = {}

    def feed(self, frame: dict) -> dict | None:
        if frame.get("type") != "chunk":
            raise MigrationError("浏览器通信协议无效。")
        key, index, total = frame.get("key"), frame.get("index"), frame.get("total")
        if not isinstance(key, str) or not isinstance(index, int) or not isinstance(total, int):
            raise MigrationError("浏览器通信分片无效。")
        if not 1 <= total <= (MAX_PAYLOAD // CHUNK_BYTES + 1) or not 0 <= index < total:
            raise MigrationError("浏览器通信分片超出限制。")
        if key not in self.pending and len(self.pending) >= 4:
            raise MigrationError("浏览器通信并发超出限制。")
        bucket = self.pending.setdefault(key, {"total": total, "parts": {}})
        if total != bucket["total"] or index in bucket["parts"]:
            raise MigrationError("浏览器通信分片重复或不一致。")
        raw = base64.b64decode(frame.get("data", ""), validate=True)
        if len(raw) > CHUNK_BYTES:
            raise MigrationError("浏览器通信分片过大。")
        bucket["parts"][index] = raw
        if len(bucket["parts"]) == total:
            data = b"".join(bucket["parts"][i] for i in range(total))
            del self.pending[key]
            if len(data) > MAX_PAYLOAD:
                raise MigrationError("浏览器通信数据过大。")
            obj = json.loads(data)
            if not isinstance(obj, dict):
                raise MigrationError("浏览器通信数据格式无效。")
            return obj
        return None


class RunLock:
    def __init__(self):
        self.file = None

    def __enter__(self):
        private_dir(RUNTIME)
        self.file = (RUNTIME / "app.lock").open("a+b")
        if self.file.tell() == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            import msvcrt
            msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            self.file.close()
            raise MigrationError("已有迁移或恢复任务正在运行，请先完成它。") from None
        return self

    def __exit__(self, *_):
        import msvcrt
        self.file.seek(0)
        msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
        self.file.close()


class Broker:
    def __init__(self, profiles: list[Profile], cancelled: threading.Event):
        self.run = uuid.uuid4().hex
        self.profiles = {p.browser: p for p in profiles}
        self.cancelled = cancelled
        self.stopped = threading.Event()
        self.clients = {}
        self.responses = {}
        self.lock = threading.RLock()
        self.address = rf"\\.\pipe\browser-transfer-{self.run}"
        self.authkey = os.urandom(32)
        self.listener = Listener(self.address, family="AF_PIPE", authkey=self.authkey)
        private_dir(RUNTIME)
        write_json(RUNTIME / "session.json", {"pipe": self.address, "key": self.authkey.hex(),
                                              "run": self.run, "pid": os.getpid()})
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while not self.stopped.is_set():
            try:
                conn = self.listener.accept()
            except (OSError, EOFError):
                break
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        browser = None
        try:
            if not conn.poll(20):
                return
            hello = json.loads(conn.recv_bytes(MAX_PAYLOAD))
            browser = hello.get("browser")
            if (hello.get("type") != "hello" or hello.get("run") != self.run or
                    browser not in self.profiles or hello.get("profile") != self.profiles[browser].directory):
                return
            with self.lock:
                if browser in self.clients:
                    self.clients[browser][0].close()
                self.clients[browser] = (conn, threading.Lock())
            while not self.stopped.is_set():
                response = json.loads(conn.recv_bytes(MAX_PAYLOAD))
                with self.lock:
                    pending = self.responses.get(response.get("id"))
                if pending and pending[0] == browser:
                    pending[1].put(response)
        except (OSError, EOFError, ValueError):
            pass
        finally:
            with self.lock:
                if browser in self.clients and self.clients[browser][0] is conn:
                    del self.clients[browser]
            conn.close()

    def wait_ready(self, browsers: list[str], timeout: float = 180):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.cancelled.is_set():
                raise Cancelled("任务已取消。")
            with self.lock:
                if all(browser in self.clients for browser in browsers):
                    return
            self.stopped.wait(0.2)
        raise MigrationError("扩展连接超时，请确认两边加载了对应扩展并保持连接页面打开。")

    def rpc(self, browser: str, operation: str, payload: dict | None = None,
            timeout: float = 180) -> dict:
        request_id = uuid.uuid4().hex
        responses = queue.Queue()
        with self.lock:
            client = self.clients.get(browser)
            if not client:
                raise MigrationError(f"{browser} 扩展尚未连接。")
            self.responses[request_id] = (browser, responses)
        try:
            message = {"id": request_id, "operation": operation, "payload": payload or {}}
            with client[1]:
                client[0].send_bytes(pack_message(message))
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                # An issued write is allowed to finish before acknowledging cancellation.
                try:
                    response = responses.get(timeout=0.2)
                except queue.Empty:
                    with self.lock:
                        if self.clients.get(browser, (None,))[0] is not client[0]:
                            raise MigrationError("扩展连接已断开；部分操作可能已完成，请查看备份并重试。")
                    continue
                if not response.get("ok"):
                    raise MigrationError("浏览器扩展拒绝操作或 API 不支持；部分操作可能已完成，请查看备份。")
                return response.get("result", {})
            raise MigrationError("浏览器操作超时；部分操作可能已完成，请查看备份并重试。")
        finally:
            with self.lock:
                self.responses.pop(request_id, None)

    def disconnect(self):
        with self.lock:
            for conn, _ in self.clients.values():
                conn.close()
            self.clients.clear()

    def close(self):
        self.stopped.set()
        self.disconnect()
        self.listener.close()
        file = RUNTIME / "session.json"
        try:
            if json.loads(file.read_text(encoding="utf-8")).get("run") == self.run:
                file.unlink(missing_ok=True)
        except (OSError, ValueError):
            pass


def register_host() -> dict:
    require_venv()
    import winreg
    root = private_dir(RUNTIME / "native")
    python = PROJECT / ".venv/Scripts/python.exe"
    if any('"' in str(p) or '%' in str(p) for p in (PROJECT, python)):
        raise MigrationError("项目路径含不支持的批处理字符，请移至普通路径。")
    launcher = root / "host.cmd"
    launcher.write_text(f'@echo off\n"{python}" "{PROJECT / "host_entry.py"}" %*\n', encoding="utf-8")
    manifest = root / "manifest.json"
    write_json(manifest, {"name": HOST_NAME, "description": "Local Browser Transfer",
                          "path": str(launcher), "type": "stdio",
                          "allowed_origins": [f"chrome-extension://{v}/" for v in EXTENSION_IDS.values()]})
    for browser in EXTENSION_IDS:
        part = "Microsoft\\Edge" if browser == "Edge" else "Google\\Chrome"
        key = rf"Software\{part}\NativeMessagingHosts\{HOST_NAME}"
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_WRITE) as reg:
            winreg.SetValueEx(reg, "", 0, winreg.REG_SZ, str(manifest))
    return {b: str(PROJECT / "extensions" / b.lower()) for b in EXTENSION_IDS}


def host_registered() -> bool:
    if os.name != "nt":
        return False
    import winreg
    expected = str(RUNTIME / "native/manifest.json")
    for part in ("Microsoft\\Edge", "Google\\Chrome"):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, rf"Software\{part}\NativeMessagingHosts\{HOST_NAME}") as key:
                if winreg.QueryValueEx(key, "")[0] != expected:
                    return False
        except OSError:
            return False
    return Path(expected).is_file()


def unregister_host() -> None:
    import winreg
    for part in ("Microsoft\\Edge", "Google\\Chrome"):
        name = rf"Software\{part}\NativeMessagingHosts\{HOST_NAME}"
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, name) as key:
                value = winreg.QueryValueEx(key, "")[0]
            if value == str(RUNTIME / "native/manifest.json"):
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, name)
        except FileNotFoundError:
            pass


def _read_exact(stream, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        part = stream.read(size - len(data))
        if not part:
            raise EOFError
        data.extend(part)
    return bytes(data)


def native_main() -> None:
    # argv[1] is supplied by the browser and must be one of the packaged origins.
    origins = {f"chrome-extension://{v}/": b for b, v in EXTENSION_IDS.items()}
    if len(sys.argv) < 2 or sys.argv[1] not in origins:
        return
    import msvcrt
    msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
    msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
    browser = origins[sys.argv[1]]
    conn = None
    try:
        session = json.loads((RUNTIME / "session.json").read_text(encoding="utf-8"))
        conn = Client(session["pipe"], family="AF_PIPE", authkey=bytes.fromhex(session["key"]))
        assembler = Assembler()
        # Read and validate the extension's session/profile pairing before forwarding.
        def read_message():
            while True:
                length = struct.unpack("<I", _read_exact(sys.stdin.buffer, 4))[0]
                if not 0 < length <= 1024 * 1024:
                    raise MigrationError("通信帧超限。")
                message = assembler.feed(json.loads(_read_exact(sys.stdin.buffer, length)))
                if message is not None:
                    return message

        hello = read_message()
        if hello.get("type") != "hello" or hello.get("run") != session["run"]:
            return
        hello["browser"] = browser
        conn.send_bytes(pack_message(hello))

        def to_browser():
            try:
                while True:
                    message = json.loads(conn.recv_bytes(MAX_PAYLOAD))
                    for frame in chunks(message):
                        data = pack_message(frame)
                        sys.stdout.buffer.write(struct.pack("<I", len(data)) + data)
                        sys.stdout.buffer.flush()
            except (OSError, EOFError, ValueError):
                pass

        threading.Thread(target=to_browser, daemon=True).start()
        while True:
            conn.send_bytes(pack_message(read_message()))
    except (OSError, EOFError, ValueError, MigrationError):
        # stdout is protocol-only. Do not print browser payloads or tracebacks.
        pass
    finally:
        if conn:
            conn.close()

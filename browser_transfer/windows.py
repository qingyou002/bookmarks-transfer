from __future__ import annotations

import csv
import ctypes
import io
import json
import os
import subprocess
import sys
from pathlib import Path

from .models import MigrationError, Profile

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def discover() -> dict[str, list[Profile]]:
    base = Path(os.environ.get("LOCALAPPDATA", ""))
    roots = {"Edge": base / "Microsoft/Edge/User Data",
             "Chrome": base / "Google/Chrome/User Data"}
    pf = Path(os.environ.get("PROGRAMFILES", "C:/Program Files"))
    pf86 = Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)"))
    candidates = {
        "Edge": [pf86 / "Microsoft/Edge/Application/msedge.exe",
                 pf / "Microsoft/Edge/Application/msedge.exe",
                 base / "Microsoft/Edge/Application/msedge.exe"],
        "Chrome": [pf / "Google/Chrome/Application/chrome.exe",
                   pf86 / "Google/Chrome/Application/chrome.exe",
                   base / "Google/Chrome/Application/chrome.exe"],
    }
    result = {"Edge": [], "Chrome": []}
    for kind, root in roots.items():
        if not root.is_dir():
            continue
        exe = next((p for p in candidates[kind] if p.is_file()), None)
        names = {}
        try:
            state = json.loads((root / "Local State").read_text(encoding="utf-8"))
            names = state.get("profile", {}).get("info_cache", {})
        except (OSError, ValueError):
            pass
        for path in sorted(root.iterdir(), key=lambda p: (p.name != "Default", p.name)):
            if (path.name == "Default" or path.name.startswith("Profile ")) and path.is_dir():
                info = names.get(path.name, {})
                result[kind].append(Profile(kind, root, path.name, exe,
                                           info.get("name", path.name)))
    return result


def browser_running(browser: str) -> bool:
    if os.name != "nt":
        raise MigrationError("本工具目前只支持 Windows。")
    image = {"Edge": "msedge.exe", "Chrome": "chrome.exe"}[browser]
    proc = subprocess.run(["tasklist.exe", "/FI", f"IMAGENAME eq {image}",
                           "/FO", "CSV", "/NH"], capture_output=True,
                          creationflags=CREATE_NO_WINDOW, timeout=15)
    if proc.returncode:
        raise MigrationError("无法检查浏览器后台进程，已停止数据库写入。")
    # Process image names are ASCII; localized tasklist messages are irrelevant.
    return any(row and row[0].casefold() == image for row in
               csv.reader(io.StringIO(proc.stdout.decode("utf-8", errors="replace"))))


def assert_closed(*profiles: Profile) -> None:
    if any(browser_running(p.browser) for p in profiles):
        raise MigrationError("浏览器仍有后台进程；请正常退出后重试。没有强制结束进程。")


def launch(profile: Profile, url: str | None = None) -> None:
    if not profile.executable or not profile.executable.is_file():
        raise MigrationError(f"找不到 {profile.browser} 可执行文件。")
    args = [str(profile.executable), f"--user-data-dir={profile.user_data}",
            f"--profile-directory={profile.directory}"]
    if url:
        args.append(url)
    subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=CREATE_NO_WINDOW)


_SID: str | None = None


def user_sid() -> str:
    global _SID
    if _SID is None:
        proc = subprocess.run([
            "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
            "[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value"
        ], capture_output=True, creationflags=CREATE_NO_WINDOW, timeout=15)
        sid = proc.stdout.decode("ascii", errors="ignore").strip()
        if proc.returncode or not sid.startswith("S-1-"):
            raise MigrationError("无法获取当前用户 SID，未创建敏感数据目录。")
        _SID = sid
    return _SID


def private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        # Restrict before writing secrets. Children inherit only this user's ACL.
        proc = subprocess.run(["icacls.exe", str(path), "/inheritance:r", "/grant:r",
                               f"*{user_sid()}:(OI)(CI)F"], capture_output=True,
                              creationflags=CREATE_NO_WINDOW, timeout=15)
        if proc.returncode:
            raise MigrationError("无法限制临时目录权限，未写入敏感数据。")
    else:
        path.chmod(0o700)
    return path


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def dpapi(data: bytes, *, decrypt: bool = False) -> bytes:
    if os.name != "nt":
        raise MigrationError("Cookie 备份加密需要 Windows DPAPI。")
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    buf = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    src = DATA_BLOB(len(data), buf)
    dst = DATA_BLOB()
    if decrypt:
        fn = crypt.CryptUnprotectData
        fn.argtypes = [ctypes.POINTER(DATA_BLOB), ctypes.c_void_p, ctypes.c_void_p,
                       ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                       ctypes.POINTER(DATA_BLOB)]
        args = (ctypes.byref(src), None, None, None, None, 1, ctypes.byref(dst))
    else:
        fn = crypt.CryptProtectData
        fn.argtypes = [ctypes.POINTER(DATA_BLOB), ctypes.c_wchar_p, ctypes.c_void_p,
                       ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                       ctypes.POINTER(DATA_BLOB)]
        args = (ctypes.byref(src), "Browser Transfer", None, None, None, 1,
                ctypes.byref(dst))
    fn.restype = ctypes.c_int
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if not fn(*args):
        raise MigrationError("Windows DPAPI 加密/解密失败。备份须由同一 Windows 用户恢复。")
    try:
        return ctypes.string_at(dst.pbData, dst.cbData)
    finally:
        kernel.LocalFree(dst.pbData)


def require_venv() -> None:
    from .models import PROJECT
    expected = (PROJECT / ".venv/Scripts/python.exe").resolve()
    actual = Path(sys.executable).resolve()
    if actual not in (expected, expected.with_name("pythonw.exe")):
        raise MigrationError("请使用项目 .venv 的 Python，或双击 setup.cmd / start.cmd。")

from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
import subprocess
import sys
import time
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


class _PROCESSENTRY32(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]


def _kernel():
    if os.name != "nt":
        raise MigrationError("本工具目前只支持 Windows。")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "CreateToolhelp32Snapshot": ([wintypes.DWORD, wintypes.DWORD], wintypes.HANDLE),
        "Process32FirstW": ([wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32)], wintypes.BOOL),
        "Process32NextW": ([wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32)], wintypes.BOOL),
        "ProcessIdToSessionId": ([wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
        "GetCurrentProcessId": ([], wintypes.DWORD),
        "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
        "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
        "QueryFullProcessImageNameW": ([wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                        ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
        "TerminateProcess": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
    }
    for name, (args, result) in signatures.items():
        fn = getattr(kernel, name)
        fn.argtypes, fn.restype = args, result
    return kernel


def _session(kernel, pid: int) -> int:
    session = wintypes.DWORD()
    if not kernel.ProcessIdToSessionId(pid, ctypes.byref(session)):
        raise MigrationError("无法检查浏览器所属会话，已停止操作。")
    return session.value


def browser_processes(browsers=("Edge", "Chrome")) -> dict[str, list[int]]:
    """Enumerate exact browser images in this logon session, including background processes."""
    kernel = _kernel()
    current = _session(kernel, kernel.GetCurrentProcessId())
    images = {"msedge.exe": "Edge", "chrome.exe": "Chrome"}
    result = {browser: [] for browser in dict.fromkeys(browsers)}
    handle = kernel.CreateToolhelp32Snapshot(2, 0)
    if handle == ctypes.c_void_p(-1).value:
        raise MigrationError("无法检查浏览器后台进程，已停止操作。")
    try:
        entry = _PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(entry)
        ok = kernel.Process32FirstW(handle, ctypes.byref(entry))
        if not ok and ctypes.get_last_error() != 18:  # ERROR_NO_MORE_FILES
            raise MigrationError("无法读取进程列表，已停止操作。")
        while ok:
            browser = images.get(entry.szExeFile.casefold())
            if browser in result:
                try:
                    if _session(kernel, entry.th32ProcessID) == current:
                        result[browser].append(entry.th32ProcessID)
                except MigrationError:
                    # A process may disappear between the snapshot and session query.
                    if ctypes.get_last_error() != 87:
                        raise
            ok = kernel.Process32NextW(handle, ctypes.byref(entry))
        if ctypes.get_last_error() != 18:
            raise MigrationError("进程列表读取不完整，已停止操作。")
    finally:
        kernel.CloseHandle(handle)
    return result


def browser_running(browser: str) -> bool:
    return bool(browser_processes([browser])[browser])


def _request_window_close(pids: set[int]) -> None:
    user = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user.EnumWindows.restype = wintypes.BOOL
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user.PostMessageW.restype = wintypes.BOOL
    failed = []
    @callback_type
    def visit(hwnd, _):
        pid = wintypes.DWORD()
        user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids and not user.PostMessageW(hwnd, 0x0010, 0, 0):  # WM_CLOSE
            failed.append(pid.value)
        return True
    if not user.EnumWindows(visit, 0) or failed:
        raise MigrationError("部分浏览器窗口无法正常关闭，请手动退出或重试。")


def _terminate_browser(pid: int, browser: str) -> None:
    kernel = _kernel()
    handle = kernel.OpenProcess(0x1001, False, pid)  # QUERY_LIMITED_INFORMATION | TERMINATE
    if not handle:
        if ctypes.get_last_error() == 87:  # Already exited.
            return
        raise MigrationError("无法结束部分浏览器进程，请手动退出后重试。")
    try:
        # Validate the opened handle, avoiding killing a reused PID or WebView2.
        name, size = ctypes.create_unicode_buffer(32768), wintypes.DWORD(32768)
        if not kernel.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(size)):
            raise MigrationError("无法验证浏览器进程，未结束该进程。")
        expected = {"Edge": "msedge.exe", "Chrome": "chrome.exe"}[browser]
        if Path(name.value).name.casefold() != expected:
            raise MigrationError("进程身份发生变化，未结束该进程。")
        if _session(kernel, pid) != _session(kernel, kernel.GetCurrentProcessId()):
            raise MigrationError("进程不属于当前会话，未结束该进程。")
        if not kernel.TerminateProcess(handle, 0):
            raise MigrationError("浏览器进程结束失败，请手动退出后重试。")
    finally:
        kernel.CloseHandle(handle)


def close_browser_processes(browsers=("Edge", "Chrome"), *, force=False,
                            cancelled=None, timeout=10) -> dict[str, int]:
    """Caller must obtain a separate explicit action before passing force=True."""
    processes = browser_processes(browsers)
    if force:
        for browser, pids in processes.items():
            for pid in pids:
                if cancelled is not None and cancelled.is_set():
                    return {b: len(p) for b, p in browser_processes(browsers).items()}
                _terminate_browser(pid, browser)
    else:
        _request_window_close({pid for pids in processes.values() for pid in pids})
    deadline = time.monotonic() + timeout
    while True:
        remaining = {b: len(p) for b, p in browser_processes(browsers).items()}
        if not any(remaining.values()) or time.monotonic() >= deadline:
            return remaining
        if cancelled is not None:
            if cancelled.wait(0.2):
                return remaining
        else:
            time.sleep(0.2)


def assert_closed(*profiles: Profile) -> None:
    if any(browser_running(p.browser) for p in profiles):
        raise MigrationError("浏览器仍有后台进程；请关闭后重试。")


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

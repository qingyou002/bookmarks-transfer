"""Launch the real Tk window and check layout/lifecycle without browser changes."""
import json
import threading
import sys
import tkinter as tk
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from browser_transfer.gui import App
from browser_transfer.bookmarks import Comparison
from browser_transfer.bookmark_dialog import BookmarkCompareDialog, BarSelectionDialog

root = tk.Tk()
app = App(root)
root.update_idletasks()
root.update()
source, target = app._profiles()
assert (source.browser, target.browser) == ("Edge", "Chrome")
app.direction.set("Chrome")
app._direction_changed()
source, target = app._profiles()
assert (source.browser, target.browser) == ("Chrome", "Edge")
assert len(app.table.get_children()) == 5
assert all(variable.get() for variable in app.selected.values())
assert root.winfo_width() >= 800
assert app.start_button.winfo_ismapped()
assert app.table.winfo_ismapped()
assert app.close_button.winfo_ismapped()
assert app.mode_combo.winfo_ismapped()
app._row_result("bookmarks", {"status": "success", "message": "旧版报告兼容测试",
                             "counts": {"added": 1, "updated": 0, "skipped": 0, "failed": 0}})
assert app.table.item("bookmarks", "values")[4] == "0"

def url(node_id, title, address):
    return {"id": node_id, "title": title, "url": address}

def bar(node_id, children):
    return {"id": node_id, "title": "书签栏", "children": children}

source = bar("1", [{"id": "10", "title": "资料", "children": [url("11", "文档", "https://docs.example.invalid/")]},
                   url("12", "搜索", "https://search.example.invalid/")])
target = bar("1", [{"id": "20", "title": "资料", "children": [url("21", "文档", "https://docs.example.invalid/"),
                                                                            url("22", "仅目标保留", "https://keep.example.invalid/")]},
                   url("23", "搜索", "https://search.example.invalid/")])
prior = Comparison(source, target, "edge", "chrome")
state = prior.state_after(target, {k: p["target"] for k, p in prior.pairs.items() if k != "root"})
source["children"][0]["title"] = "资料与学习"
source["children"].reverse()
comparison = Comparison(source, target, "edge", "chrome", state)
answers = []
dialog = BookmarkCompareDialog(root, {"message": "选择整栏基准和逐项例外，查看目标结果后应用。", "comparison": comparison,
                                      "source_browser": "Edge", "target_browser": "Chrome"}, answers.append)
root.update()
assert str(dialog.apply_button.cget("state")) == "normal", dialog.summary.get()
child_key = comparison.by_id["target"]["22"]
folder_key = comparison.by_id["target"]["20"]
dialog.differences.selection_set(child_key)
dialog.choose("target")
assert comparison.resolve(dialog.decision())["preview"]["deleted"] == 0
dialog.differences.selection_set(folder_key)
dialog.choose("source")
assert child_key not in dialog.choices
assert comparison.resolve(dialog.decision())["preview"]["deleted"] == 1
dialog.base.set("target")
dialog.reset()
assert comparison.resolve(dialog.decision())["preview"]["updated"] == 0
dialog.base.set("source")
dialog.refresh()
dialog.differences.selection_set(child_key)
dialog.choose("target")
dialog.selected()
dialog.apply()
assert answers[-1]["choices"][child_key] == "target"
root.update()
assert dialog.differences.winfo_ismapped()
assert dialog.apply_button.winfo_y() + dialog.apply_button.winfo_height() <= dialog.apply_button.master.winfo_height()

# Capture only this synthetic Tk test window for visual QA, using standard-library Win32 GDI.
import ctypes
from ctypes import wintypes as w
import struct
import zlib

user, gdi = ctypes.WinDLL("user32"), ctypes.WinDLL("gdi32")
user.GetAncestor.argtypes, user.GetAncestor.restype = [w.HWND, w.UINT], w.HWND
user.GetWindowRect.argtypes = [w.HWND, ctypes.POINTER(w.RECT)]
user.GetDC.argtypes, user.GetDC.restype = [w.HWND], w.HDC
user.ReleaseDC.argtypes = [w.HWND, w.HDC]
user.PrintWindow.argtypes, user.PrintWindow.restype = [w.HWND, w.HDC, w.UINT], w.BOOL
gdi.CreateCompatibleDC.argtypes, gdi.CreateCompatibleDC.restype = [w.HDC], w.HDC
gdi.CreateCompatibleBitmap.argtypes, gdi.CreateCompatibleBitmap.restype = [w.HDC, ctypes.c_int, ctypes.c_int], w.HBITMAP
gdi.SelectObject.argtypes, gdi.SelectObject.restype = [w.HDC, w.HGDIOBJ], w.HGDIOBJ
gdi.DeleteObject.argtypes = [w.HGDIOBJ]
gdi.DeleteDC.argtypes = [w.HDC]
gdi.GetDIBits.argtypes = [w.HDC, w.HBITMAP, w.UINT, w.UINT, ctypes.c_void_p, ctypes.c_void_p, w.UINT]
hwnd = user.GetAncestor(dialog.winfo_id(), 2)
rect = w.RECT()
user.GetWindowRect(hwnd, ctypes.byref(rect))
width, height = rect.right - rect.left, rect.bottom - rect.top
dc = user.GetDC(hwnd)
memory = gdi.CreateCompatibleDC(dc)
bitmap = gdi.CreateCompatibleBitmap(dc, width, height)
previous = gdi.SelectObject(memory, bitmap)
try:
    assert user.PrintWindow(hwnd, memory, 0)
    gdi.SelectObject(memory, previous)
    header = struct.pack("<IiiHHIIiiII", 40, width, -height, 1, 32, 0, width * height * 4, 0, 0, 0, 0)
    info = ctypes.create_string_buffer(header + b"\0" * 16)
    pixels = ctypes.create_string_buffer(width * height * 4)
    assert gdi.GetDIBits(memory, bitmap, 0, height, pixels, info, 0) == height
    raw = pixels.raw
    rows = bytearray()
    for y in range(height):
        rows.append(0)
        for x in range(width):
            i = (y * width + x) * 4
            rows.extend((raw[i + 2], raw[i + 1], raw[i]))
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")
    screenshot = Path(__file__).resolve().parent.parent / "runtime" / "gui-v2-smoke.png"
    screenshot.parent.mkdir(exist_ok=True)
    screenshot.write_bytes(png)
finally:
    gdi.DeleteObject(bitmap)
    gdi.DeleteDC(memory)
    user.ReleaseDC(hwnd, dc)
dialog.destroy()

selection = []
picker = BarSelectionDialog(root, {"message": "选择书签栏", "source_browser": "Edge", "target_browser": "Chrome",
                                  "source_bars": [source], "target_bars": [target, bar("9", [])]}, selection.append)
root.update()
picker.combos["target"].current(1)
def descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from descendants(child)
next(b for b in descendants(picker) if b.winfo_class() == "TButton" and b.cget("text") == "比较所选书签栏").invoke()
assert selection[-1] == {"source": "1", "target": "9"}
picker.destroy()

for can_force in (False, True):
    event = {"request": {"kind": "close", "message": "保存内容后关闭测试浏览器", "can_force": can_force,
                         "remaining": {"Edge": 1}}, "ready": threading.Event(), "response": {}}
    app._show_prompt(event)
    root.update()
    labels = [b.cget("text") for b in descendants(app.prompt_window) if b.winfo_class() == "TButton"]
    assert ("强制结束剩余进程" in labels) == can_force
    app.prompt_reply("cancel")
print(json.dumps({"gui": "passed", "direction_selection": "both", "rows": 5,
                  "size": [root.winfo_width(), root.winfo_height()], "bookmark_dialog": "passed",
                  "bar_selection": "passed", "close_dialog": "passed", "screenshot": str(screenshot)}, ensure_ascii=False))
root.destroy()

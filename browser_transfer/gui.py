from __future__ import annotations

import json
import os
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .backup import write_json
from .bridge import register_host
from .cli import environment
from .engine import Engine, safe_error
from .models import ITEMS, LABELS, PROJECT, RUNTIME
from .windows import discover, launch, private_dir

STATUS = {"pending": "等待", "success": "完成", "partial": "部分完成", "failed": "失败",
          "cancelled": "已取消", "skipped": "已跳过", "confirmed": "用户已确认"}


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("浏览器迁移 · Edge ↔ Chrome")
        self.root.geometry("960x730")
        self.root.minsize(800, 630)
        self.root.configure(bg="#f4f6f8")
        self.profiles = discover()
        self.messages = queue.Queue()
        self.cancel = threading.Event()
        self.busy = False
        self.close_requested = False
        self.report = None
        self.prompt_window = None
        self.prompt_reply = None
        self.direction = tk.StringVar(value="Edge")
        self.source_choice = tk.StringVar()
        self.target_choice = tk.StringVar()
        self.selected = {name: tk.BooleanVar(value=True) for name in ITEMS}
        self.stage = tk.StringVar(value="就绪 · 请先检查环境并设置扩展")
        self._build()
        self._load_settings()
        self._direction_changed()
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self.root.after(100, self._pump)

    def _build(self):
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("TFrame", background="#f4f6f8")
        style.configure("TLabel", background="#f4f6f8", font=("Microsoft YaHei UI", 10))
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 22, "bold"), foreground="#183b35")
        style.configure("Note.TLabel", foreground="#536676")
        style.configure("Treeview", rowheight=30, font=("Microsoft YaHei UI", 10))
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 10))
        outer = ttk.Frame(self.root, padding=24)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="浏览器迁移", style="Title.TLabel").pack(anchor="w")
        ttk.Label(outer, text="Edge ↔ Chrome  ·  每次选择一个方向，保留目标独有数据", style="Note.TLabel").pack(anchor="w", pady=(4, 18))
        direction = ttk.Frame(outer)
        direction.pack(fill="x")
        self.radios = []
        for label, value in (("Edge → Chrome", "Edge"), ("Chrome → Edge", "Chrome")):
            radio = ttk.Radiobutton(direction, text=label, value=value, variable=self.direction,
                                    command=self._direction_changed)
            radio.pack(side="left", padx=(0, 28))
            self.radios.append(radio)
        choices = ttk.Frame(outer)
        choices.pack(fill="x", pady=12)
        self.source_label = ttk.Label(choices, text="来源配置")
        self.source_label.grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.source_combo = ttk.Combobox(choices, textvariable=self.source_choice, state="readonly", width=30)
        self.source_combo.grid(row=0, column=1, sticky="ew", padx=(0, 24))
        self.target_label = ttk.Label(choices, text="目标配置")
        self.target_label.grid(row=0, column=2, sticky="w", padx=(0, 8))
        self.target_combo = ttk.Combobox(choices, textvariable=self.target_choice, state="readonly", width=30)
        self.target_combo.grid(row=0, column=3, sticky="ew")
        choices.columnconfigure(1, weight=1)
        choices.columnconfigure(3, weight=1)
        options = ttk.Frame(outer)
        options.pack(fill="x", pady=(0, 10))
        self.checkboxes = []
        for name in ITEMS:
            widget = ttk.Checkbutton(options, text=LABELS[name], variable=self.selected[name])
            widget.pack(side="left", padx=(0, 14))
            self.checkboxes.append(widget)
        ttk.Label(outer, text="冲突使用本次来源；密码需官方导入确认，Cookie 不保证所有网站保持登录。", style="Note.TLabel").pack(anchor="w", pady=(0, 14))
        buttons = ttk.Frame(outer)
        buttons.pack(fill="x")
        self.check_button = ttk.Button(buttons, text="环境检查", command=self._check)
        self.setup_button = ttk.Button(buttons, text="扩展设置", command=self._setup)
        self.start_button = ttk.Button(buttons, text="开始迁移", command=self._start)
        self.cancel_button = ttk.Button(buttons, text="停止后续步骤", command=self._cancel, state="disabled")
        self.restore_button = ttk.Button(buttons, text="恢复备份", command=self._restore)
        self.result_button = ttk.Button(buttons, text="查看结果", command=self._view_result)
        for button in (self.check_button, self.setup_button, self.start_button, self.cancel_button,
                       self.restore_button, self.result_button):
            button.pack(side="left", padx=(0, 8))
        ttk.Label(outer, textvariable=self.stage, style="Note.TLabel").pack(anchor="w", pady=(18, 6))
        self.progress = ttk.Progressbar(outer, mode="indeterminate")
        self.progress.pack(fill="x", pady=(0, 12))
        self.table = ttk.Treeview(outer, columns=("item", "status", "added", "updated", "skipped", "failed"),
                                  show="headings", height=5, selectmode="browse")
        for name, label, width in (("item", "数据", 180), ("status", "状态", 140),
                                   ("added", "新增", 80), ("updated", "更新", 80),
                                   ("skipped", "跳过", 80), ("failed", "失败", 80)):
            self.table.heading(name, text=label)
            self.table.column(name, width=width, anchor="w" if name == "item" else "center")
        for name in ITEMS:
            self.table.insert("", "end", iid=name, values=(LABELS[name], "未开始", "—", "—", "—", "—"))
        self.table.pack(fill="x")
        self.table.bind("<<TreeviewSelect>>", self._selected_result)
        log_frame = ttk.Frame(outer)
        log_frame.pack(fill="both", expand=True, pady=(14, 0))
        self.log = tk.Text(log_frame, height=8, wrap="word", font=("Microsoft YaHei UI", 10),
                           bg="white", fg="#263a48", relief="solid", bd=1, padx=10, pady=8, state="disabled")
        scroll = ttk.Scrollbar(log_frame, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        self.log.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

    def _load_settings(self):
        self.saved = {}
        try:
            self.saved = json.loads((RUNTIME / "settings.json").read_text(encoding="utf-8"))
            if self.saved.get("source") in {"Edge", "Chrome"}:
                self.direction.set(self.saved["source"])
            for name, variable in self.selected.items():
                variable.set(name in self.saved.get("items", ITEMS))
        except (OSError, ValueError):
            pass

    def _direction_changed(self):
        source = self.direction.get()
        target = "Chrome" if source == "Edge" else "Edge"
        self.source_label.configure(text=f"来源 {source}")
        self.target_label.configure(text=f"目标 {target}")
        for browser, widget, variable in ((source, self.source_combo, self.source_choice),
                                           (target, self.target_combo, self.target_choice)):
            values = [f"{p.directory} · {p.display_name}" for p in self.profiles[browser]]
            widget["values"] = values
            saved = self.saved.get("profiles", {}).get(browser, "Default")
            pick = next((i for i, p in enumerate(self.profiles[browser]) if p.directory == saved), 0)
            variable.set(values[pick] if values else "未找到配置")

    def _profiles(self):
        source = self.direction.get()
        target = "Chrome" if source == "Edge" else "Edge"
        def selected(browser, combo):
            index = combo.current()
            if index < 0 or not self.profiles[browser]:
                raise ValueError("profile-not-found")
            return self.profiles[browser][index]
        return selected(source, self.source_combo), selected(target, self.target_combo)

    def _append(self, message):
        self.log.configure(state="normal")
        self.log.insert("end", message + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _set_busy(self, value):
        self.busy = value
        for widget in (self.check_button, self.setup_button, self.start_button, self.restore_button,
                       *self.radios, *self.checkboxes):
            widget.configure(state="disabled" if value else "normal")
        for combo in (self.source_combo, self.target_combo):
            combo.configure(state="disabled" if value else "readonly")
        self.cancel_button.configure(state="normal" if value else "disabled")
        if value:
            self.progress.start(12)
        else:
            self.progress.stop()

    def _worker(self, job):
        if self.busy:
            return
        self.cancel.clear()
        self._set_busy(True)
        def work():
            try:
                job()
            except Exception as exc:
                self.messages.put({"type": "error", "message": safe_error(exc)})
            finally:
                self.messages.put({"type": "worker_done"})
        threading.Thread(target=work, daemon=True).start()

    def _prompt(self, request):
        ready = threading.Event()
        response = {"answer": "cancel"}
        self.messages.put({"type": "prompt", "request": request, "ready": ready, "response": response})
        while not ready.wait(0.2):
            if self.cancel.is_set():
                return "cancel"
        return response["answer"]

    def _engine(self):
        return Engine(self.messages.put, self._prompt, self.cancel)

    def _start(self):
        try:
            source, target = self._profiles()
            items = [name for name in ITEMS if self.selected[name].get()]
            if not items:
                messagebox.showinfo("请选择数据", "至少勾选一个迁移项目。", parent=self.root)
                return
            private_dir(RUNTIME)
            self.saved = {"source": source.browser, "profiles": {source.browser: source.directory,
                                                                  target.browser: target.directory}, "items": items}
            write_json(RUNTIME / "settings.json", self.saved)
            for name in ITEMS:
                self.table.item(name, values=(LABELS[name], "等待" if name in items else "未选择", 0, 0, 0, 0))
            self.report = None
            self._worker(lambda: self._engine().migrate(source, target, items))
        except Exception as exc:
            messagebox.showerror("无法开始", safe_error(exc), parent=self.root)

    def _cancel(self):
        self.cancel.set()
        self.stage.set("正在停止后续步骤；当前数据库事务或已发出的浏览器操作会先结束。")
        self._append("已请求停止后续步骤。")
        if self.prompt_reply:
            self.prompt_reply("cancel")

    def _check(self):
        def job():
            info = environment()
            text = f"Python 环境：{info['python_environment']}\nTk：{info['tk']}\n"
            for browser, profiles in info["profiles"].items():
                text += f"{browser}：找到 {len(profiles)} 个配置\n"
            text += "本机通信：" + ("已注册" if info["native_host_registered"] else "尚未注册，请点击扩展设置")
            if info["stale_password_directories"]:
                text += "\n发现上次运行的临时目录，开始迁移时会清理工具生成的 CSV。"
            self.messages.put({"type": "info", "message": text})
        self._worker(job)

    def _setup(self):
        def job():
            paths = register_host()
            text = "已为当前 Windows 用户注册本机通信。\n在 Edge 和 Chrome 的扩展页面开启开发人员模式，分别加载：\n"
            text += "\n".join(f"{b}：{p}" for b, p in paths.items())
            text += "\n\n不要交叉加载；注册通信不等于已经安装浏览器扩展。"
            self.messages.put({"type": "info", "message": text})
            for browser in paths:
                if self.profiles[browser]:
                    launch(self.profiles[browser][0], "edge://extensions" if browser == "Edge" else "chrome://extensions")
        self._worker(job)

    def _restore(self):
        directory = filedialog.askdirectory(title="选择包含 manifest.json 的备份目录",
                                             initialdir=str(RUNTIME / "backups"), parent=self.root)
        if directory:
            self._worker(lambda: self._engine().restore(Path(directory)))

    def _view_result(self):
        try:
            report = self.report or json.loads((RUNTIME / "last-result.json").read_text(encoding="utf-8"))
            window = tk.Toplevel(self.root)
            window.title("迁移结果")
            window.geometry("740x520")
            text = tk.Text(window, wrap="word", font=("Microsoft YaHei UI", 10), padx=14, pady=14)
            text.pack(fill="both", expand=True)
            text.insert("end", json.dumps(report, ensure_ascii=False, indent=2))
            text.configure(state="disabled")
        except (OSError, ValueError):
            messagebox.showinfo("迁移结果", "还没有迁移结果。", parent=self.root)

    def _selected_result(self, _):
        selected = self.table.selection()
        if self.report and selected:
            result = self.report.get("results", {}).get(selected[0])
            if result and result.get("message"):
                self._append(f"{LABELS[selected[0]]}：{result['message']}")

    def _show_prompt(self, event):
        request = event["request"]
        dialog = tk.Toplevel(self.root)
        self.prompt_window = dialog
        dialog.title("需要你完成一个步骤")
        dialog.transient(self.root)
        dialog.resizable(False, False)
        box = ttk.Frame(dialog, padding=22)
        box.pack(fill="both", expand=True)
        ttk.Label(box, text=request["message"], wraplength=620, justify="left").pack(anchor="w", pady=(0, 12))
        file_var = tk.StringVar(value=request.get("file", ""))
        kind = request["kind"]
        if "file" in request:
            ttk.Label(box, text="CSV 文件路径：").pack(anchor="w")
            ttk.Entry(box, textvariable=file_var, width=76, state="normal" if kind == "password_export" else "readonly").pack(fill="x", pady=6)
            row = ttk.Frame(box)
            row.pack(fill="x")
            def copy_path():
                self.root.clipboard_clear()
                self.root.clipboard_append(file_var.get())
            ttk.Button(row, text="复制路径", command=copy_path).pack(side="left")
            if kind == "password_export":
                def browse():
                    selected = filedialog.askopenfilename(parent=dialog, initialdir=request["directory"],
                                                          filetypes=[("密码 CSV", "*.csv")])
                    if selected:
                        file_var.set(selected)
                ttk.Button(row, text="选择已导出的 CSV", command=browse).pack(side="left", padx=8)
            ttk.Label(box, text="CSV 是明文。工具会清理自己目录中的文件；外部原文件需要你自行删除。",
                      style="Note.TLabel", wraplength=620).pack(anchor="w", pady=10)
        buttons = ttk.Frame(box)
        buttons.pack(fill="x", pady=(12, 0))
        def answer(value):
            if event["ready"].is_set():
                return
            event["response"]["answer"] = value
            event["ready"].set()
            self.prompt_reply = None
            self.prompt_window = None
            dialog.destroy()
        self.prompt_reply = answer
        def proceed():
            if kind == "password_export":
                if not Path(file_var.get()).is_file():
                    messagebox.showerror("找不到 CSV", "请先完成官方导出，再选择文件。", parent=dialog)
                    return
                answer(file_var.get())
            else:
                answer("done" if kind == "password_confirm" else "continue")
        ttk.Button(buttons, text="已完成导入" if kind == "password_confirm" else "继续", command=proceed).pack(side="left")
        if kind.startswith("password"):
            ttk.Button(buttons, text="跳过密码", command=lambda: answer("skip")).pack(side="left", padx=8)
        ttk.Button(buttons, text="取消后续步骤", command=lambda: answer("cancel")).pack(side="right")
        dialog.protocol("WM_DELETE_WINDOW", lambda: answer("cancel"))
        dialog.grab_set()

    def _pump(self):
        if self.cancel.is_set() and self.prompt_reply:
            self.prompt_reply("cancel")
        while True:
            try:
                event = self.messages.get_nowait()
            except queue.Empty:
                break
            kind = event["type"]
            if kind == "prompt":
                if self.cancel.is_set():
                    event["response"]["answer"] = "cancel"
                    event["ready"].set()
                else:
                    self._show_prompt(event)
            elif kind in {"progress", "error", "info"}:
                self._append(event["message"])
                self.stage.set(event["message"].splitlines()[0])
                if kind == "info":
                    messagebox.showinfo("环境与扩展", event["message"], parent=self.root)
            elif kind == "result":
                self._row_result(event["item"], event["result"])
            elif kind == "finished":
                self.report = event["report"]
                for name, result in self.report["results"].items():
                    self._row_result(name, result)
                if self.report.get("backup"):
                    self._append("备份：" + self.report["backup"])
            elif kind == "restored":
                self.report = event["report"]
                self._append("恢复结果：" + self.report["status"])
            elif kind == "worker_done":
                self._set_busy(False)
                if self.close_requested:
                    self.root.destroy()
                    return
        self.root.after(100, self._pump)

    def _row_result(self, name, result):
        counts = result["counts"]
        values = (LABELS[name], STATUS.get(result["status"], result["status"]))
        values += ("—", "—", "—", "—") if name == "passwords" else tuple(counts[k] for k in ("added", "updated", "skipped", "failed"))
        self.table.item(name, values=values)
        self._append(f"{LABELS[name]}：{result['message']}")

    def _close(self):
        if self.busy:
            if messagebox.askyesno("停止并退出", "是否停止后续步骤并在当前操作结束后退出？", parent=self.root):
                self.close_requested = True
                self._cancel()
        else:
            self.root.destroy()


def run():
    root = tk.Tk()
    App(root)
    root.mainloop()

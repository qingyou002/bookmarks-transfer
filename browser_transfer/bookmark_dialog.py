from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from .models import MigrationError


def bar_label(bar):
    storage = "账号书签栏" if bar.get("syncing") else "本地书签栏"
    return f"{bar.get('title', '书签栏')} · {storage} · ID {bar['id']}" + ("（受管理）" if bar.get("unmodifiable") else "")


def fill_tree(widget, nodes, parent="", *, desired=False):
    for node in nodes:
        node_id = node["key"] if desired else node["id"]
        widget.insert(parent, "end", iid=node_id, text=node.get("title", ""),
                      values=(node.get("url", ""),), open=True)
        fill_tree(widget, node.get("children", []), node_id, desired=desired)


def tree_box(parent, title):
    box = ttk.LabelFrame(parent, text=title, padding=8)
    tree = ttk.Treeview(box, columns=("url",), selectmode="browse", height=7)
    tree.heading("#0", text="名称 / 文件夹")
    tree.heading("url", text="网址")
    tree.column("#0", width=220)
    tree.column("url", width=240)
    scroll = ttk.Scrollbar(box, orient="vertical", command=tree.yview)
    horizontal = ttk.Scrollbar(box, orient="horizontal", command=tree.xview)
    tree.configure(yscrollcommand=scroll.set, xscrollcommand=horizontal.set)
    tree.grid(row=0, column=0, sticky="nsew")
    scroll.grid(row=0, column=1, sticky="ns")
    horizontal.grid(row=1, column=0, sticky="ew")
    box.rowconfigure(0, weight=1)
    box.columnconfigure(0, weight=1)
    return box, tree


class BarSelectionDialog(tk.Toplevel):
    def __init__(self, parent, request, reply):
        super().__init__(parent)
        self.title("选择参与比较的书签栏")
        self.transient(parent)
        self.resizable(False, False)
        box = ttk.Frame(self, padding=22)
        box.pack(fill="both", expand=True)
        ttk.Label(box, text=request["message"]).pack(anchor="w", pady=(0, 12))
        self.combos = {}
        for side in ("source", "target"):
            ttk.Label(box, text=request[side + "_browser"]).pack(anchor="w")
            combo = ttk.Combobox(box, state="readonly", width=65,
                                 values=[bar_label(b) for b in request[side + "_bars"]])
            combo.pack(fill="x", pady=(4, 12))
            writable = next((i for i, b in enumerate(request[side + "_bars"]) if not b.get("unmodifiable")), 0)
            combo.current(writable)
            self.combos[side] = combo
        def choose():
            selection = {s: request[s + "_bars"][c.current()]["id"] for s, c in self.combos.items()}
            reply(selection)
        ttk.Button(box, text="比较所选书签栏", command=choose).pack(side="left")
        ttk.Button(box, text="取消", command=lambda: reply("cancel")).pack(side="right")


class BookmarkCompareDialog(tk.Toplevel):
    def __init__(self, parent, request, reply):
        super().__init__(parent)
        self.comparison = request["comparison"]
        self.reply = reply
        self.choices = {}
        self.base = tk.StringVar(value="source")
        self.summary = tk.StringVar()
        self.detail = tk.StringVar(value="选择下方差异，可采用某一边的版本；没有历史记录时只标注单边存在。")
        self.title("比较主书签栏")
        self.transient(parent)
        self.geometry(f"{min(1160, self.winfo_screenwidth() - 80)}x{min(820, self.winfo_screenheight() - 100)}")
        self.minsize(800, 620)
        box = ttk.Frame(self, padding=16)
        box.pack(fill="both", expand=True)
        ttk.Label(box, text=request["message"], wraplength=1000).pack(anchor="w")
        base_row = ttk.Frame(box)
        base_row.pack(fill="x", pady=10)
        ttk.Label(base_row, text="整栏基准：").pack(side="left")
        for side in ("source", "target"):
            ttk.Radiobutton(base_row, text=f"采用 {request[side + '_browser']}", variable=self.base,
                            value=side, command=self.refresh).pack(side="left", padx=12)
        ttk.Button(base_row, text="清除全部例外", command=self.reset).pack(side="right")
        trees = ttk.Panedwindow(box, orient="horizontal")
        trees.pack(fill="both", expand=True)
        self.trees = {}
        for side in ("source", "target"):
            frame, tree = tree_box(trees, request[side + "_browser"] + ("（来源）" if side == "source" else "（目标）"))
            trees.add(frame, weight=1)
            fill_tree(tree, getattr(self.comparison, side).get("children", []))
            self.trees[side] = tree
        table_box = ttk.Frame(box)
        table_box.pack(fill="both", expand=True, pady=(12, 0))
        self.differences = ttk.Treeview(table_box, columns=("kind", "source", "target", "choice"),
                                       show="headings", selectmode="browse", height=6)
        for name, label, width in (("kind", "变化", 130), ("source", "来源", 330),
                                   ("target", "目标", 330), ("choice", "选择", 130)):
            self.differences.heading(name, text=label)
            self.differences.column(name, width=width, minwidth=80)
        scroll = ttk.Scrollbar(table_box, command=self.differences.yview)
        horizontal = ttk.Scrollbar(table_box, orient="horizontal", command=self.differences.xview)
        self.differences.configure(yscrollcommand=scroll.set, xscrollcommand=horizontal.set)
        self.differences.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        table_box.columnconfigure(0, weight=1)
        table_box.rowconfigure(0, weight=1)
        for row in self.comparison.differences:
            self.differences.insert("", "end", iid=row["key"], values=(row["label"], row["source"], row["target"], "跟随基准 / 文件夹"))
        self.differences.bind("<<TreeviewSelect>>", self.selected)
        actions = ttk.Frame(box)
        actions.pack(fill="x", pady=8)
        for label, side in (("此项采用来源", "source"), ("此项采用目标", "target"), ("此项跟随基准", None)):
            ttk.Button(actions, text=label, command=lambda s=side: self.choose(s)).pack(side="left", padx=(0, 8))
        ttk.Label(box, textvariable=self.detail, wraplength=1020).pack(anchor="w", pady=(0, 8))
        ttk.Label(box, textvariable=self.summary, wraplength=1020).pack(anchor="w")
        buttons = ttk.Frame(box)
        buttons.pack(fill="x", pady=(12, 0))
        ttk.Button(buttons, text="预览目标结果", command=self.preview).pack(side="left")
        self.apply_button = ttk.Button(buttons, text="应用所选变化", command=self.apply)
        self.apply_button.pack(side="left", padx=10)
        ttk.Button(buttons, text="取消迁移", command=lambda: reply("cancel")).pack(side="right")
        self.refresh()

    def decision(self):
        return {"base": self.base.get(), "choices": dict(self.choices)}

    def reset(self):
        self.choices.clear()
        self.refresh()

    def selected(self, _=None):
        selected = self.differences.selection()
        if not selected:
            return
        row = next(r for r in self.comparison.differences if r["key"] == selected[0])
        self.detail.set(f"{row['label']}\n来源：{row['source']}\n目标：{row['target']}")
        key = selected[0].removeprefix("order:")
        for side, tree in self.trees.items():
            node = self.comparison.node(key, side)
            if node and tree.exists(node["id"]):
                tree.selection_set(node["id"])
                tree.see(node["id"])

    def choose(self, side):
        selected = self.differences.selection()
        if not selected:
            return
        key = selected[0]
        if not key.startswith("order:"):
            for child in self.comparison.descendant_keys(key):
                self.choices.pop(child, None)
                self.choices.pop("order:" + child, None)
            self.choices.pop("order:" + key, None)
        if side is None:
            self.choices.pop(key, None)
        else:
            self.choices[key] = side
        self.refresh()

    def refresh(self):
        for row in self.comparison.differences:
            choice = {"source": "采用来源", "target": "采用目标"}.get(self.choices.get(row["key"]), "跟随基准 / 文件夹")
            self.differences.item(row["key"], values=(row["label"], row["source"], row["target"], choice))
        try:
            plan = self.comparison.resolve(self.decision())
            c = plan["preview"]
            self.summary.set(f"目标将新增 {c['added']} 项，修改 {c['updated']} 项（含移动 {c['moved']} 项），删除 {c['deleted']} 项。目录和网址均计数。")
            self.apply_button.configure(state="normal")
        except MigrationError as exc:
            self.summary.set(str(exc))
            self.apply_button.configure(state="disabled")

    def preview(self):
        try:
            plan = self.comparison.resolve(self.decision())
        except MigrationError:
            return
        window = tk.Toplevel(self)
        window.title("应用后的目标主书签栏")
        window.geometry("800x500")
        frame, tree = tree_box(window, "目标书签栏预览")
        frame.pack(fill="both", expand=True, padx=12, pady=12)
        fill_tree(tree, plan["tree"], desired=True)

    def apply(self):
        try:
            self.comparison.resolve(self.decision())
        except MigrationError:
            self.refresh()
            return
        self.reply(self.decision())

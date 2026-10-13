from __future__ import annotations

import argparse
import json
import threading
import sys
from pathlib import Path

from .bridge import host_registered, register_host, unregister_host
from .engine import Engine, safe_error
from .models import ITEMS, PROJECT, Profile, RUNTIME
from .windows import discover, require_venv


def environment() -> dict:
    import tkinter
    profiles = discover()
    return {"python_environment": str(PROJECT / ".venv"), "tk": tkinter.TkVersion,
            "native_host_registered": host_registered(),
            "profiles": {b: [p.to_dict() for p in values] for b, values in profiles.items()},
            "extensions": {b: str(PROJECT / "extensions" / b.lower()) for b in profiles},
            "stale_password_directories": len(list((RUNTIME / "tmp").glob("run-*")))}


def console_prompt(request: dict) -> str | dict:
    print("\n" + request["message"])
    if request["kind"] == "close":
        if request.get("remaining") is not None:
            print("剩余进程：", "；".join(f"{b} {n}" for b, n in request["remaining"].items()))
        options = "c 正常关闭；y 已手动退出，检查并继续；q 取消"
        if request.get("can_force"):
            options += "；f 强制结束剩余进程"
        answer = input(options + "：").strip().lower()
        return "close_normal" if answer == "c" else "close_force" if answer == "f" and request.get("can_force") else "continue" if answer == "y" else "cancel"
    if request["kind"] == "bookmark_bars":
        answer = {}
        for side in ("source", "target"):
            print(request[side + "_browser"] + " 书签栏：")
            bars = request[side + "_bars"]
            for i, bar in enumerate(bars, 1):
                print(f"  {i}. {bar.get('title', '')} · {'账号' if bar.get('syncing') else '本地'}" +
                      ("（受管理）" if bar.get("unmodifiable") else ""))
            while True:
                value = input("输入编号；q 取消：").strip().lower()
                if value == "q":
                    return "cancel"
                if value.isdigit() and 1 <= int(value) <= len(bars) and not bars[int(value) - 1].get("unmodifiable"):
                    answer[side] = bars[int(value) - 1]["id"]
                    break
        return answer
    if request["kind"] == "bookmark_compare":
        comparison = request["comparison"]
        def show(nodes, depth=0):
            for node in nodes:
                print("  " * depth + node.get("title", "") + (" · " + node["url"] if "url" in node else ""))
                show(node.get("children", []), depth + 1)
        for side in ("source", "target"):
            print(request[side + "_browser"] + "：")
            show(getattr(comparison, side).get("children", []))
        decision = {"base": "source", "choices": {}}
        rows = comparison.differences
        while True:
            print("\n整栏基准：" + request[decision["base"] + "_browser"])
            for i, row in enumerate(rows, 1):
                choice = decision["choices"].get(row["key"], "基准 / 文件夹")
                print(f"{i}. {row['label']} · {choice}\n   来源：{row['source']}\n   目标：{row['target']}")
            valid = True
            try:
                counts = comparison.resolve(decision)["preview"]
                print(f"新增 {counts['added']}，修改 {counts['updated']}（含移动 {counts['moved']}），删除 {counts['deleted']}")
            except Exception as exc:
                valid = False
                print(safe_error(exc))
            value = input("s/t 整栏采用来源/目标；编号+s/t/b 设置例外/跟随基准，如 1t；y 应用；q 取消：").strip().lower()
            if value == "q":
                return "cancel"
            if value == "y" and valid:
                return decision
            if value in {"s", "t"}:
                decision["base"] = "source" if value == "s" else "target"
            elif len(value) > 1 and value[:-1].isdigit() and value[-1] in {"s", "t", "b"} and 1 <= int(value[:-1]) <= len(rows):
                key = rows[int(value[:-1]) - 1]["key"]
                if not key.startswith("order:"):
                    for child in comparison.descendant_keys(key):
                        decision["choices"].pop(child, None)
                        decision["choices"].pop("order:" + child, None)
                    decision["choices"].pop("order:" + key, None)
                if value[-1] == "b":
                    decision["choices"].pop(key, None)
                else:
                    decision["choices"][key] = "source" if value[-1] == "s" else "target"
    if request["kind"] == "password_export":
        print("建议导出路径：", request["file"])
        answer = input("输入 CSV 路径；s 跳过；q 取消：").strip().strip('"')
        return "skip" if answer.lower() == "s" else "cancel" if answer.lower() == "q" else answer
    if request["kind"] == "password_confirm":
        print(f"第 {request['batch']}/{request['total']} 批：{request['file']}")
        answer = input("确认官方导入完成请输入 done；s 跳过；q 取消：").strip().lower()
        return "done" if answer == "done" else "skip" if answer == "s" else "cancel"
    answer = input("完成提示中的操作后输入 y 继续；其他输入取消：").strip().lower()
    return "continue" if answer == "y" else "cancel"


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Windows Edge ↔ Chrome 本机迁移工具")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("gui", "check", "setup", "uninstall"):
        sub.add_parser(name)
    migrate = sub.add_parser("migrate")
    migrate.add_argument("--from", dest="source", choices=("Edge", "Chrome"), default="Edge")
    migrate.add_argument("--to", dest="target", choices=("Edge", "Chrome"), default=None)
    migrate.add_argument("--source-profile", default="Default")
    migrate.add_argument("--target-profile", default="Default")
    migrate.add_argument("--items", nargs="+", choices=ITEMS, default=list(ITEMS))
    migrate.add_argument("--bookmarks-mode", choices=("bar", "archive"), default="bar")
    restore = sub.add_parser("restore")
    restore.add_argument("backup", type=Path)
    args = parser.parse_args(argv)
    try:
        require_venv()
        if args.command == "gui":
            from .gui import run
            run()
        elif args.command == "check":
            print(json.dumps(environment(), ensure_ascii=False, indent=2))
        elif args.command == "setup":
            print(json.dumps(register_host(), ensure_ascii=False, indent=2))
            print("本机通信已注册。请在两边扩展页面启用开发人员模式，加载各自目录。")
        elif args.command == "uninstall":
            unregister_host()
            print("本工具的 Native Messaging 注册已移除；备份保留。请手动卸载浏览器扩展。")
        else:
            cancel = threading.Event()
            def emit(event):
                if event.get("message"):
                    print(event["message"])
            engine = Engine(emit, console_prompt, cancel)
            if args.command == "restore":
                report = engine.restore(args.backup)
            else:
                kinds = discover()
                target = args.target or ("Chrome" if args.source == "Edge" else "Edge")
                def find(browser: str, directory: str) -> Profile:
                    match = next((p for p in kinds[browser] if p.directory == directory), None)
                    if match is None:
                        raise ValueError("profile-not-found")
                    return match
                report = engine.migrate(find(args.source, args.source_profile),
                                        find(target, args.target_profile), args.items,
                                        bookmarks_mode=args.bookmarks_mode)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            if any(r["status"] in {"failed", "partial", "cancelled", "skipped"}
                   for r in report.get("results", {}).values()) or report.get("status") == "partial":
                return 2
        return 0
    except KeyboardInterrupt:
        print("\n已中断。请查看最近的备份和结果；下次启动会检查残留密码 CSV。")
        return 130
    except Exception as exc:
        print(safe_error(exc))
        return 1

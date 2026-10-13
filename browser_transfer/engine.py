from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path
from urllib.parse import urlencode

from .backup import BOOKMARK_FILES, Backup, write_json
from .bookmarks import Comparison, StateStore, fingerprint, select_bar
from .bridge import Broker, EXTENSION_IDS, RunLock, host_registered
from .database import merge_autofill, merge_history
from .models import Cancelled, Counts, ITEMS, LABELS, MigrationError, Profile, RUNTIME, Result
from .passwords import cleanup, csv_batches
from .windows import assert_closed, browser_processes, close_browser_processes, launch, private_dir


def safe_error(exc: BaseException) -> str:
    if isinstance(exc, MigrationError):
        return str(exc)
    return f"操作失败（{type(exc).__name__}）。未输出敏感内容，请检查数据库锁、配置和备份。"


class Engine:
    def __init__(self, emit=lambda event: None, prompt=lambda request: "cancel",
                 cancelled: threading.Event | None = None):
        self.emit, self.prompt = emit, prompt
        self.cancelled = cancelled or threading.Event()
        self.broker = None
        self.bridge_info = {}

    def event(self, message: str, **fields):
        self.emit({"type": "progress", "message": message, **fields})

    def check_cancel(self):
        if self.cancelled.is_set():
            raise Cancelled("已停止后续步骤；已完成的迁移仍然保留。")

    def ask(self, kind: str, message: str, **fields):
        self.check_cancel()
        answer = self.prompt({"kind": kind, "message": message, **fields})
        if answer == "cancel":
            self.cancelled.set()
            self.check_cancel()
        return answer

    def close_browsers(self, profiles: list[Profile], *, initial_action=None):
        browsers = list(dict.fromkeys(p.browser for p in profiles))
        remaining = None
        can_force = False
        while True:
            self.check_cancel()
            if initial_action == "close_normal":
                answer, initial_action = initial_action, None
            else:
                answer = self.ask("close", "请先保存正在编辑的内容。关闭操作会处理当前 Windows 会话中 " +
                         "、".join(browsers) + " 的所有配置窗口及后台进程。先正常关闭；仍有进程时可另点“强制结束剩余进程”。",
                         profiles=[p.to_dict() for p in profiles], remaining=remaining, can_force=can_force)
            try:
                if answer == "close_normal" or (answer == "close_force" and can_force):
                    self.event("正在" + ("强制结束剩余浏览器进程。" if answer == "close_force" else "请求正常关闭浏览器，最多等待 10 秒。"))
                    remaining = close_browser_processes(browsers, force=answer == "close_force",
                                                        cancelled=self.cancelled)
                    self.check_cancel()
                    can_force = bool(any(remaining.values()))
                    self.event("剩余进程：" + "；".join(f"{b} {n}" for b, n in remaining.items()))
                assert_closed(*profiles)
                return
            except MigrationError as exc:
                if isinstance(exc, Cancelled):
                    raise
                self.event(str(exc))
                try:
                    remaining = {b: len(pids) for b, pids in browser_processes(browsers).items()}
                    if answer == "close_normal":
                        can_force = bool(any(remaining.values()))
                except MigrationError:
                    remaining, can_force = None, False

    def prepare_bookmark_bar(self, source: Profile, target: Profile) -> dict:
        for profile in (source, target):
            if self.bridge_info.get(profile.browser, {}).get("bookmarkBarVersion") != 1:
                raise MigrationError(f"请在 {profile.browser} 的扩展页面重新加载本工具 0.2.0 扩展，然后重试主书签栏比较。")
        left = self.broker.rpc(source.browser, "bookmarks_bar_export")
        right = self.broker.rpc(target.browser, "bookmarks_bar_export")
        if not left.get("bars") or not right.get("bars"):
            raise MigrationError("没有找到双方可识别的主书签栏，未开始迁移。")
        if len(left["bars"]) > 1 or len(right["bars"]) > 1:
            answer = self.ask("bookmark_bars", "请选择本次比较的来源和目标书签栏。",
                              source_bars=left["bars"], target_bars=right["bars"],
                              source_browser=source.browser, target_browser=target.browser)
            if not isinstance(answer, dict):
                raise MigrationError("未选择参与比较的书签栏。")
        else:
            answer = {"source": left["bars"][0]["id"], "target": right["bars"][0]["id"]}
        source_bar, target_bar = select_bar(left, answer["source"]), select_bar(right, answer["target"])
        store = StateStore(RUNTIME / "bookmark-state")
        key = store.key(source, target, source_bar["id"], target_bar["id"])
        state = store.read(key)
        comparison = Comparison(source_bar, target_bar, source.key, target.key, state)
        decision = self.ask("bookmark_compare", "选择整栏基准，再设置需要保留的逐项例外。应用只修改目标主书签栏。",
                            comparison=comparison, source_browser=source.browser, target_browser=target.browser)
        if not isinstance(decision, dict):
            raise MigrationError("尚未确认书签栏差异，未开始迁移。")
        return {"comparison": comparison, "plan": comparison.resolve(decision),
                "store": store, "key": key, "before": state}

    def apply_bookmark_bar(self, source: Profile, target: Profile, prepared: dict) -> Result:
        comparison, plan = prepared["comparison"], prepared["plan"]
        for profile, before in ((source, comparison.source), (target, comparison.target)):
            current = select_bar(self.broker.rpc(profile.browser, "bookmarks_bar_export"), before["id"])
            if fingerprint(current) != fingerprint(before):
                return Result("failed", Counts(failed=1), "书签栏在确认后发生变化，未写入；请重新比较后迁移。")
        response = self.broker.rpc(target.browser, "bookmarks_bar_apply", plan)
        counts = Counts(**response["counts"])
        if not response.get("complete") or counts.failed:
            message = ("书签栏在执行前发生变化，未写入；请重新比较。" if response.get("reason") == "stale-snapshot"
                       else "书签栏部分操作未完成，比较基线未更新；请重新比较或恢复备份。")
            return Result("partial" if counts.added or counts.updated or counts.deleted else "failed", counts, message)
        try:
            prepared["store"].write(prepared["key"], comparison.state_after(response["bar"], response["nodeIds"]))
        except Exception as exc:
            counts.failed += 1
            return Result("partial", counts, "目标书签栏已更新，但比较状态保存失败；请恢复备份或重新比较。" + safe_error(exc))
        return Result("success", counts, "已按确认的选择更新目标主书签栏。")

    def open_bridges(self, profiles: list[Profile]):
        if not host_registered():
            raise MigrationError("尚未注册本机通信，请先点击“扩展设置”或执行 setup 命令。")
        if self.broker is None:
            self.broker = Broker(profiles, self.cancelled)
        for profile in profiles:
            query = urlencode({"session": self.broker.run, "profile": profile.directory})
            launch(profile, f"chrome-extension://{EXTENSION_IDS[profile.browser]}/bridge.html?{query}")
        self.event("等待扩展连接，请保持浏览器连接页面打开。")
        self.broker.wait_ready([p.browser for p in profiles])
        for profile in profiles:
            self.bridge_info[profile.browser] = self.broker.rpc(profile.browser, "ping")

    def _cleanup_stale(self):
        tmp = private_dir(RUNTIME / "tmp")
        for directory in tmp.glob("run-*"):
            if directory.is_dir():
                cleanup(directory)

    def migrate(self, source: Profile, target: Profile, items: list[str], *, bookmarks_mode="bar") -> dict:
        if source.browser == target.browser:
            raise MigrationError("请选择不同的源浏览器和目标浏览器。")
        if not items or not set(items) <= set(ITEMS):
            raise MigrationError("至少选择一个有效迁移项目。")
        if bookmarks_mode not in {"bar", "archive"}:
            raise MigrationError("收藏夹模式不受支持。")
        report = {"version": 2, "direction": f"{source.browser} → {target.browser}", "bookmarks_mode": bookmarks_mode,
                  "results": {name: Result().to_dict() for name in items}, "backup": None}
        results = {name: Result() for name in items}
        backup = None
        snapshots = {}
        with RunLock():
            self._cleanup_stale()
            try:
                self.check_cancel()
                self.event(f"准备 {source.browser} → {target.browser}。")
                online = {"bookmarks", "cookies"} & set(items)
                if online:
                    self.open_bridges([source, target])
                    if "bookmarks" in items:
                        if bookmarks_mode == "bar":
                            snapshots["bar"] = self.prepare_bookmark_bar(source, target)
                        else:
                            snapshots["bookmarks"] = self.broker.rpc(source.browser, "bookmarks_export")
                        snapshots["mappings"] = self.broker.rpc(target.browser, "mappings_export")
                    if "cookies" in items:
                        snapshots["source_cookies"] = self.broker.rpc(source.browser, "cookies_export")["cookies"]
                        snapshots["target_cookies"] = self.broker.rpc(target.browser, "cookies_export")["cookies"]
                    self.broker.disconnect()
                self.close_browsers([source, target])
                self.check_cancel()
                files = []
                for name in items:
                    files += {"history": ["History"], "autofill": ["Web Data"],
                              "bookmarks": list(BOOKMARK_FILES),
                              "passwords": ["Login Data", "Login Data For Account"],
                              "cookies": []}[name]
                self.event("正在备份目标数据。")
                backup = Backup.create(target, files, guard=lambda: assert_closed(source, target))
                report["backup"] = str(backup.directory)
                backup.manifest["direction"] = report["direction"]
                if "mappings" in snapshots:
                    backup.manifest["bookmark_mappings"] = snapshots["mappings"].get("bookmarkMappings", {})
                if "bookmarks" in items:
                    backup.manifest["bookmark_bar_states"] = StateStore(RUNTIME / "bookmark-state").for_profile(target.key)
                backup.save()
                if "cookies" in items:
                    backup.save_cookies(snapshots["target_cookies"], snapshots["source_cookies"])
                for name, merger, file in (("history", merge_history, "History"),
                                            ("autofill", merge_autofill, "Web Data")):
                    if name not in items:
                        continue
                    self.check_cancel()
                    self.event(f"正在合并{LABELS[name]}。", item=name)
                    try:
                        results[name].counts = merger(source.path / file, target.path / file,
                                                     guard=lambda: assert_closed(source, target))
                        results[name].status = "partial" if results[name].counts.failed else "success"
                        results[name].message = ("部分地址包含不支持的字段，已跳过这些地址。" if
                                                 results[name].counts.failed else "已合并；重复运行不会重复导入。")
                    except Exception as exc:
                        results[name] = Result("failed", Counts(failed=1), safe_error(exc))
                    self.emit({"type": "result", "item": name, "result": results[name].to_dict()})
                if online:
                    self.check_cancel()
                    self.open_bridges([source, target])
                    for name, operation, payload in (
                        ("bookmarks", "bookmarks_import", {**snapshots.get("bookmarks", {}),
                          "namespace": source.key, "sourceBrowser": source.browser}),
                        ("cookies", "cookies_import", {"cookies": snapshots.get("source_cookies", [])}),
                    ):
                        if name not in items:
                            continue
                        self.check_cancel()
                        self.event(f"正在合并{LABELS[name]}。", item=name)
                        try:
                            if name == "bookmarks" and bookmarks_mode == "bar":
                                results[name] = self.apply_bookmark_bar(source, target, snapshots["bar"])
                            else:
                                response = self.broker.rpc(target.browser, operation, payload)
                                counts = Counts(**response["counts"])
                                results[name] = Result("partial" if counts.failed else "success", counts,
                                                       "已处理；网站是否保持登录请在浏览器中验证。" if
                                                       name == "cookies" else "已合并到来源浏览器专用目录。")
                        except Exception as exc:
                            results[name] = Result("failed", Counts(failed=1), safe_error(exc))
                        self.emit({"type": "result", "item": name, "result": results[name].to_dict()})
                else:
                    launch(source)
                    launch(target)
                if "passwords" in items:
                    self.check_cancel()
                    results["passwords"] = self.password_stage(source, target)
                    self.emit({"type": "result", "item": "passwords", "result": results["passwords"].to_dict()})
                self.event("迁移流程结束，请查看各项目结果。")
            except Cancelled as exc:
                report["message"] = str(exc)
                for name in items:
                    if results[name].status == "pending":
                        results[name] = Result("cancelled", message="未执行。")
                self.event(str(exc))
            except Exception as exc:
                report["message"] = safe_error(exc)
                for name in items:
                    if results[name].status == "pending":
                        results[name] = Result("failed", Counts(failed=1), safe_error(exc))
                self.event(report["message"])
            finally:
                if self.broker:
                    self.broker.close()
                    self.broker = None
                report["results"] = {name: results[name].to_dict() for name in items}
                report_file = RUNTIME / "last-result.json"
                write_json(report_file, report)
                if backup:
                    write_json(backup.directory / "result.json", report)
                self.emit({"type": "finished", "report": report})
        return report

    def password_stage(self, source: Profile, target: Profile) -> Result:
        directory = private_dir(RUNTIME / "tmp" / ("run-" + uuid.uuid4().hex))
        export_file = directory / "export.csv"
        pages = {"Edge": "edge://settings/passwords", "Chrome": "chrome://password-manager/settings"}
        try:
            launch(source, pages[source.browser])
            answer = self.ask("password_export",
                f"请在 {source.browser} 官方密码管理器导出 CSV，并完成身份验证。建议保存到下方受限目录。",
                directory=str(directory), file=str(export_file), source=source.browser)
            if answer == "skip":
                return Result("skipped", message="已跳过密码；可以稍后仅选择密码重试。")
            input_file = Path(answer)
            batches, candidates = csv_batches(input_file, directory)
            if not batches:
                return Result("skipped", message="CSV 没有可导入的密码条目。")
            launch(target, pages[target.browser])
            confirmed = 0
            for index, file in enumerate(batches, 1):
                answer = self.ask("password_confirm",
                    f"请在 {target.browser} 官方密码管理器导入此 CSV。相同网站、相同用户名的冲突请选择来源 "
                    f"{source.browser} 的密码。只有完成该批导入后才点击“已完成导入”。",
                    file=str(file), batch=index, total=len(batches), target=target.browser)
                if answer == "skip":
                    return Result("partial" if confirmed else "skipped", message=
                                  f"已确认 {confirmed}/{len(batches)} 批；实际导入数量以浏览器结果为准。")
                confirmed += 1
            message = f"用户已确认完成 {confirmed} 批导入；候选 {candidates.added} 条，实际数量以浏览器结果为准。"
            if input_file.parent.resolve() != directory.resolve():
                message += "原导出文件在工具目录外，请自行删除；工具仅清理自己的临时文件。"
            return Result("confirmed", message=message)
        except Cancelled:
            raise
        except Exception as exc:
            return Result("failed", Counts(failed=1), safe_error(exc))
        finally:
            cleanup(directory)

    def restore(self, directory: Path) -> dict:
        with RunLock():
            backup = Backup.load(directory)
            target = Profile.from_dict(backup.manifest["target"])
            self.ask("restore", "将恢复此备份中的目标本地数据；备份之后的相关修改会被撤回。当前数据会先另存备份。"
                     " 已同步到云端的修改不能保证撤销。", target=target.to_dict(), directory=str(directory))
            cookies = backup.read_cookies()  # Check DPAPI before any changes.
            restore_bookmarks = bool(set(BOOKMARK_FILES) & set(backup.manifest["files"]))
            bar_states = backup.manifest.get("bookmark_bar_states", {})
            store = StateStore(RUNTIME / "bookmark-state")
            if restore_bookmarks:
                store.validate_profile_states(target.key, bar_states)
                current_states = store.for_profile(target.key)
            if (cookies is not None or "bookmark_mappings" in backup.manifest) and not host_registered():
                raise MigrationError("恢复此备份需要扩展通信，请先完成扩展设置。尚未恢复文件。")
            self.close_browsers([target])
            self.check_cancel()
            self.event("正在恢复目标本地文件。")
            rollback = backup.restore_files(guard=lambda: assert_closed(target),
                                            extra_manifest={"bookmark_bar_states": current_states} if restore_bookmarks else None)
            report = {"restored": str(directory), "target": target.browser, "status": "success"}
            try:
                if restore_bookmarks:
                    store.restore_profile(target.key, bar_states)
                if cookies is not None or "bookmark_mappings" in backup.manifest:
                    self.open_bridges([target])
                    if "bookmark_mappings" in backup.manifest:
                        self.broker.rpc(target.browser, "mappings_restore",
                                        {"bookmarkMappings": backup.manifest["bookmark_mappings"]})
                    if cookies is not None:
                        response = self.broker.rpc(target.browser, "cookies_restore", cookies)
                        report["cookies"] = response["counts"]
                        if response["counts"]["failed"]:
                            report["status"] = "partial"
                else:
                    launch(target)
                self.event("恢复流程结束。")
            except Exception as exc:
                report["status"] = "partial"
                report["message"] = "本地文件已恢复，但比较状态或扩展阶段未全部完成：" + safe_error(exc)
            finally:
                if self.broker:
                    self.broker.close()
                    self.broker = None
            write_json(RUNTIME / "last-result.json", report)
            self.emit({"type": "restored", "report": report})
            return report

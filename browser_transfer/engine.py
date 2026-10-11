from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path
from urllib.parse import urlencode

from .backup import Backup, write_json
from .bridge import Broker, EXTENSION_IDS, RunLock, host_registered
from .database import merge_autofill, merge_history
from .models import Cancelled, Counts, ITEMS, LABELS, MigrationError, Profile, RUNTIME, Result
from .passwords import cleanup, csv_batches
from .windows import assert_closed, launch, private_dir


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

    def event(self, message: str, **fields):
        self.emit({"type": "progress", "message": message, **fields})

    def check_cancel(self):
        if self.cancelled.is_set():
            raise Cancelled("已停止后续步骤；已完成的迁移仍然保留。")

    def ask(self, kind: str, message: str, **fields) -> str:
        self.check_cancel()
        answer = self.prompt({"kind": kind, "message": message, **fields})
        if answer == "cancel":
            self.cancelled.set()
            self.check_cancel()
        return answer

    def close_browsers(self, profiles: list[Profile]):
        while True:
            self.ask("close", "请保存正在编辑的内容，然后正常退出 " +
                     "、".join(p.browser for p in profiles) +
                     "（包括后台进程）。本工具不会强制关闭。", profiles=[p.to_dict() for p in profiles])
            try:
                assert_closed(*profiles)
                return
            except MigrationError as exc:
                self.event(str(exc))

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
            self.broker.rpc(profile.browser, "ping")

    def _cleanup_stale(self):
        tmp = private_dir(RUNTIME / "tmp")
        for directory in tmp.glob("run-*"):
            if directory.is_dir():
                cleanup(directory)

    def migrate(self, source: Profile, target: Profile, items: list[str]) -> dict:
        if source.browser == target.browser:
            raise MigrationError("请选择不同的源浏览器和目标浏览器。")
        if not items or not set(items) <= set(ITEMS):
            raise MigrationError("至少选择一个有效迁移项目。")
        report = {"version": 1, "direction": f"{source.browser} → {target.browser}",
                  "results": {name: Result().to_dict() for name in items}, "backup": None}
        results = {name: Result() for name in items}
        backup = None
        snapshots = {}
        with RunLock():
            self._cleanup_stale()
            try:
                self.check_cancel()
                self.event(f"准备 {source.browser} → {target.browser}；目标独有数据保留。")
                online = {"bookmarks", "cookies"} & set(items)
                if online:
                    self.open_bridges([source, target])
                    if "bookmarks" in items:
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
                              "bookmarks": ["Bookmarks", "Bookmarks.bak"],
                              "passwords": ["Login Data", "Login Data For Account"],
                              "cookies": []}[name]
                self.event("正在备份目标数据。")
                backup = Backup.create(target, files, guard=lambda: assert_closed(source, target))
                report["backup"] = str(backup.directory)
                backup.manifest["direction"] = report["direction"]
                if "mappings" in snapshots:
                    backup.manifest["bookmark_mappings"] = snapshots["mappings"].get("bookmarkMappings", {})
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
            if (cookies is not None or "bookmark_mappings" in backup.manifest) and not host_registered():
                raise MigrationError("恢复此备份需要扩展通信，请先完成扩展设置。尚未恢复文件。")
            self.close_browsers([target])
            self.check_cancel()
            self.event("正在恢复目标本地文件。")
            backup.restore_files(guard=lambda: assert_closed(target))
            report = {"restored": str(directory), "target": target.browser, "status": "success"}
            try:
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
                report["message"] = "本地文件已恢复，但扩展阶段未全部完成：" + safe_error(exc)
            finally:
                if self.broker:
                    self.broker.close()
                    self.broker = None
            write_json(RUNTIME / "last-result.json", report)
            self.emit({"type": "restored", "report": report})
            return report

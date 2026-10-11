# 实现说明

## 组成

- `browser_transfer.gui`：Tkinter 主线程只更新窗口，耗时操作在工作线程运行；通过队列发送进度和人工确认请求。
- `browser_transfer.engine`：同一套流程接受源与目标 Profile，支持两个方向；一次只运行一个任务，取消在步骤边界执行，已发出的浏览器写入和 SQLite 事务允许完成。
- `browser_transfer.database`：按列映射进行 SQLite 事务合并，保留目标 schema/meta，不复制来源同步和派生 ID。新访问的 from_visit/opener_visit 两遍重新映射；重复身份不重新插入。
- `browser_transfer.backup`：目标文件快照、哈希校验、当前用户 DPAPI Cookie 快照、本地文件恢复前的再次备份。
- `browser_transfer.bridge`：HKCU Native Messaging 注册，浏览器启动项目 `.venv` 中的 Python；两个固定扩展 ID 连接本机随机命名管道，使用每轮随机 256 位认证密钥。不开放 HTTP 端口，不使用 pickle 接收消息。
- `extensions/common`：官方 bookmarks/cookies/storage API 的共用实现；`tools/build_extensions.py` 将共用文件复制到可加载的 Edge/Chrome 目录。修改共用代码后要重新运行构建脚本。

## 通信

Python 主程序打开所选用户配置的扩展连接页，URL 仅包含本轮标识和配置目录名。连接页没有后台自动采集行为；没有本轮参数时只显示使用提示。

Native host 校验由浏览器传入的扩展 origin，再校验连接页的本轮标识。Broker 将该浏览器连接与所选配置名配对。实际配置由 `--profile-directory` 启动参数选择，用户必须在对应配置加载扩展。

每条业务消息最多 64 MiB。Native Messaging 用 UTF-8 JSON 和 4 字节小端长度头，分成 192 KiB 字节块后 Base64 传输，单帧小于浏览器的 1 MiB host → extension 限制。限制块数量、重复块及并发重组数量。原始数据在内存中传递；Cookie 持久备份由 DPAPI 加密。

会话文件和备份目录限制为当前 Windows 用户访问。此工具不对同一用户下已经取得本机执行权限的恶意程序提供独立隔离。

## 地址格式

目标支持现代 `addresses` + `address_type_tokens`。来源也能读取较旧的 `local_addresses` / `contact_info` token 格式。只接受官方稳定的姓名、地址、电话和邮箱 FieldType；内容未知且非空时跳过整个地址，避免静默丢失。账户 record_type 0/1/2/3 转为目标本地 record_type 0，重新生成 GUID，不复制账号标识或同步元数据。

备份 History/Web Data 使用 SQLite Backup API，因此不遗漏 committed WAL。运行任务要求两边正常退出并在每个数据库事务前和提交前再次检查浏览器进程。数据库操作可能遇到用户在检查后立即重新启动浏览器的竞争，SQLite 锁和事务作为最后一道保护；运行过程中请不要自行启动浏览器，等待工具重新打开。

## 官方依据与参考项目

- [Chrome cookies API](https://developer.chrome.com/docs/extensions/reference/api/cookies)
- [Chrome bookmarks API](https://developer.chrome.com/docs/extensions/reference/api/bookmarks)
- [Chrome Native Messaging](https://developer.chrome.com/docs/extensions/develop/concepts/native-messaging)
- [Edge Native Messaging](https://learn.microsoft.com/en-us/microsoft-edge/extensions/developer-guide/native-messaging)
- [Edge 应用绑定加密](https://learn.microsoft.com/en-us/deployedge/microsoft-edge-policies/applicationboundencryptionenabled)
- [Chrome 官方密码导入](https://support.google.com/chrome/answer/13068232?hl=en)
- [Edge 官方密码导出](https://support.microsoft.com/en-us/edge/export-passwords-in-microsoft-edge)
- [Chromium FieldType 定义](https://github.com/chromium/chromium/blob/main/components/autofill/core/browser/field_types.h)
- [Chromium AutofillProfile RecordType](https://github.com/chromium/chromium/blob/main/components/autofill/core/browser/data_model/addresses/autofill_profile.h)

GitHub 调研涉及 [Chromigrate](https://github.com/Brubbish/Chromigrate)、[Browser Bookmark Tool](https://github.com/mickpletcher/browser-bookmark-tool) 和 [Cookie-Editor](https://github.com/Moustachauve/cookie-editor)。本项目没有复制这些项目的源代码；双向合并、GUI、备份和扩展代码为本项目独立实现。

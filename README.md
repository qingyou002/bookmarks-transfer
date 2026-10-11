# 浏览器迁移 · Edge ↔ Chrome

Windows 上自用的 Python 3.11 + Tkinter 桌面工具。每次选择一个方向，把来源浏览器的数据合并到目标浏览器；保留目标独有数据。支持 Edge → Chrome 和 Chrome → Edge。

## 快速开始

1. 双击 `setup.cmd`。它会在项目根目录创建 `.venv`，按 `requirements.txt` 初始化环境并检查浏览器配置。首次版本只依赖标准库，Tkinter 随 Windows 版 Python 安装，不通过 pip 安装。
2. 双击 `start.cmd` 打开窗口。
3. 点击 **扩展设置**。它会为当前 Windows 用户注册 Native Messaging 通信（不需要管理员），并打开两边扩展页面。
4. 在扩展页面开启开发人员模式，点击“加载解压缩的扩展”，分别加载：
   - Edge：项目下的 `extensions\edge`
   - Chrome：项目下的 `extensions\chrome`
   请在要迁移的用户配置中加载对应扩展，**不要交叉加载两个目录**。如果选用其他 `Profile *`，也需要在该配置中加载扩展。
5. 在窗口选择方向、来源和目标配置，勾选需要的数据，点击 **开始迁移**。
6. 按提示正常退出两边浏览器，后台进程也必须结束。工具不会强制关闭。完成离线阶段后会重新打开所选配置。
7. 密码需要你在官方界面导出、验证身份和导入。其他步骤由工具处理，最后逐项显示结果。

“扩展设置”只注册通信并提供加载目录，不会自动安装扩展。若 Chrome 的开发人员模式受组织策略限制，需要先解决该限制，Cookie 和收藏夹迁移才能工作。只选择历史、自动填充或密码时不需要扩展。

## 双向迁移的规则

这是一轮一轮的单向合并，不是实时双向同步。方向切换后，冲突规则也跟随本次来源切换。

| 数据 | 行为 |
| --- | --- |
| 收藏夹 | 目标中创建 `Edge 收藏夹` 或 `Chrome 收藏夹`，保留来源层级，重复运行更新已映射的条目。保留目标原有收藏夹，不传播删除。通过本工具导入的副本不会在反向迁移时再次传回，避免导入目录循环嵌套；如果要编辑主要收藏夹，建议在其原始来源中修改。 |
| 历史记录 | 从本机数据库读取，包括已经同步到本机的记录，不调用云端接口。保留原始访问时间、访问类型和可映射的来源/打开关系。按 URL、微秒时间和访问类型去重。目标独有访问保留。 |
| Cookie | 普通和分区 Cookie 通过浏览器 API 迁移，保留会话/过期时间、HostOnly、Secure、HttpOnly、SameSite 和分区键。相同域、路径、名称和分区的 Cookie 以本次来源为准，不复制来源 `storeId`。 |
| 密码 | 通过官方 CSV 导出和导入。冲突由你在官方界面选择使用来源密码。工具不会解密密码数据库或代填 Windows 验证。结果标记为“用户已确认”，候选条数不是官方实际导入条数。 |
| 自动填充 | 合并姓名、地址、电话、邮箱及地址结构字段，内容相同去重，内容不同分别保留。已缓存的账户地址转换为目标本地地址。包含不支持的非空字段的地址会整条跳过并计入失败；银行卡、增强自动填充的证件、同步元数据和普通输入框历史不迁移。 |

Cookie 写入成功不保证网站继续登录，网站可能还依赖设备凭据、localStorage、IndexedDB、sessionStorage 或再次验证。切换浏览器也可能触发网站的登录检查。浏览器原有的“关闭时清除数据”等设置仍会生效；会话 Cookie 的后续寿命由目标浏览器决定。

收藏夹和历史的统计单位不同：收藏夹包含目录和网址节点，历史“新增”按访问记录计数，“更新”按已有网址的统计更新计数。

## 密码步骤

- 工具打开来源官方密码管理器，给出受限临时目录中的建议导出路径。请把官方导出的 CSV 保存到该路径，再在工具中选择文件。
- 工具校验 `url,username,password` 列，正确处理中文、逗号、引号和换行，并按最多 3,000 条拆分导入批次。
- 每一批都需要你在目标官方密码管理器导入，处理冲突，查看导入结果，然后点击“已完成导入”。可以跳过或取消剩余批次。
- 本工具自己的临时 CSV 在成功、跳过、取消或异常后会清理。异常退出后的残留目录在下次启动迁移时清理。删除文件不等于 SSD 上的安全擦除。
- 如果选择了工具目录外的 CSV，工具保留原文件，只清理自己的副本。请自行删除外部原导出文件。

## 备份与恢复

每轮迁移在写入前备份所选项目的目标本地文件，位于 `runtime\backups\日期-标识\`。数据库使用 SQLite Backup API 获取一致快照，包含已提交的 WAL 数据，不简单复制数据库主文件。

- 备份及临时目录限制为当前 Windows 用户访问。Cookie 快照另外使用当前用户 DPAPI 加密，必须由同一 Windows 用户恢复。
- 点击 **恢复备份**，选择包含 `manifest.json` 的目录。工具验证文件哈希和数据库完整性；先保存当前相关文件，再恢复备份。浏览器必须正常退出。
- 恢复收藏夹会一起恢复该轮之前的映射。Cookie 恢复只撤回该轮涉及的 Cookie，尽量保留独立 Cookie；已过期的旧 Cookie 不会复活。
- 本地文件恢复与浏览器 API 阶段不是一个整体事务。若扩展阶段失败，结果明确标记“部分完成”，可以重新恢复该备份。
- 恢复可能撤回备份之后对相关本地文件的修改。已同步到微软/Google 账号或服务器的变化不保证撤销。

`runtime\last-result.json` 保存最近一轮结果。文件和窗口日志不包含密码、Cookie 值、地址内容或网址列表；结果会包含配置路径和计数。原始历史与地址备份仍属于个人数据，请妥善保管。

## 命令行

始终使用项目虚拟环境：

```powershell
.\.venv\Scripts\python.exe -m browser_transfer check
.\.venv\Scripts\python.exe -m browser_transfer setup
.\.venv\Scripts\python.exe -m browser_transfer gui

# Edge → Chrome
.\.venv\Scripts\python.exe -m browser_transfer migrate --from Edge --to Chrome

# Chrome → Edge，只迁移收藏夹和历史
.\.venv\Scripts\python.exe -m browser_transfer migrate --from Chrome --to Edge --items bookmarks history

# 指定双方配置
.\.venv\Scripts\python.exe -m browser_transfer migrate --from Edge --to Chrome --source-profile Default --target-profile "Profile 1"

.\.venv\Scripts\python.exe -m browser_transfer restore "E:\路径\runtime\backups\备份目录"
.\.venv\Scripts\python.exe -m browser_transfer uninstall
```

`setup` 注册当前用户的本机通信；`uninstall` 只移除本工具注册的通道并保留备份，浏览器扩展需要手动卸载。命令行迁移仍有关闭浏览器和密码提示，不能当作无人值守任务。退出码 0 表示本次选中项目完成；2 表示部分完成、跳过或取消；1 表示启动/操作错误。

## 测试与验证范围

```powershell
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -v
node tests\test_extensions.js
```

Python 测试仅使用临时模拟配置，覆盖双向重复迁移、精确时间和 ID 关系、数据库锁和回滚、地址合并、DPAPI 加密、备份恢复、CSV 拆分，以及真实 Native Messaging Python 子进程与 Windows 命名管道的通信。JavaScript 测试用内存浏览器 API 验证书签反向循环处理、Cookie 属性、冲突、恢复和大消息。Node 仅用于开发测试，不是运行依赖。

本机真实配置尚未进行写入验收。第一次使用时建议先在两边各创建临时用户配置，准备少量测试数据，加载扩展，验证两个方向，再选择日常配置。最终需要在浏览器中检查收藏夹、历史时间、自动填充，以及选定网站是否保持登录和密码是否可填充。

历史与地址存储属于浏览器内部格式。运行时检查必需表/列、目标必填字段和完整性；不支持的结构会停止对应项目，不整体覆盖数据库。不承诺未来浏览器版本全部兼容。当前设计以本机 Edge 155 / Chrome 154 的现代 Chromium 格式为目标，Edge 真实结构仍需在退出后进行运行时校验。

不迁移银行卡、通行密钥、网站本地存储、扩展自身配置或标签页。同步设置和浏览器安全策略保持不变，源数据不被直接写入；浏览器正常启动和官方导出本身可能更新其运行状态。

更多实现说明见 [架构说明](docs/architecture.md)。

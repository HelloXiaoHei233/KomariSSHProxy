# MCP 交互终端与协助 SSH

MCP 是 Komari 网页终端与 Agent 之间的交互桥。它直接管理 WebSocket PTY，不依赖 SSH `exec`，也不将命令包装为执行脚本。原 SSH 代理仍提供交互 shell；SFTP 由本地 SSH 代理端口与协助端口单独提供，MCP 本身不提供可靠的命令退出码。

## 启动与鉴权

从服务器选择页面的 **MCP** 进入设置。服务默认关闭；启动后默认只监听 `127.0.0.1:12297`。

- MCP 端点：`POST /mcp`，Streamable HTTP，官方 Python SDK 负责协议协商。
- 每个 MCP 请求都需要 `Authorization: Bearer <设置中的 MCP 密码>`。
- 请求正文为 UTF-8 JSON；设置 `Content-Type: application/json` 和 `Accept: application/json, text/event-stream`。
- 独立随机密码首次运行生成，可修改为 16–128 位无空白的可打印 ASCII 字符。
- “复制 Agent 连接指令”包含地址、鉴权请求头、通用配置示例及操作规则。剪贴板内容含密码，仅交给需要控制终端的 Agent。
- 客户端须支持静态 Bearer 或自定义 HTTP 请求头。本服务没有 OAuth 登录与授权服务器发现流程。
- `GET /` 公开提供脱敏的完整帮助，不公开服务器列表、连接状态或凭据。
- `GET /logs` 使用同样的密码鉴权，可下载保留的 JSONL 操作日志。

服务无 HTTP 会话 ID；真正的终端会话由 `connection_id` 标识。2025 协议客户端先执行 `initialize`，再发送 `notifications/initialized`，后续携带协商的 `MCP-Protocol-Version`。协议与参数错误由 SDK 或工具返回可读原因。

## 工具

| 工具 | 用途 |
| --- | --- |
| `get_help` | 完整帮助、参数格式与示例 |
| `get_service_info` | MCP、协助 SSH 状态与资源限制 |
| `list_terminals` | 只列 UUID、名称和在线状态；`query` 搜索，`refresh=true` 刷新 |
| `get_terminal` | 按唯一 `server_uuid` 获取单台服务器详细配置、性能快照和已开窗口，`refresh=true` 刷新 |
| `list_connections` | 活动 MCP 窗口；`include_closed=true` 包含保留的历史 |
| `get_connection` | 指定窗口的完整详情 |
| `connect_terminal` | 新建独立窗口，返回 `connection_id` |
| `disconnect_terminal` | 断开窗口，保留已有输出；重复调用安全 |
| `send_and_read` | 可选输入、等待、读取输出的唯一 MCP 终端 I/O 工具 |
| `resize_terminal` | 改变共享终端尺寸 |
| `file_operation` | 面板原生文件管理，与 SSH SFTP 共用文件接口 |
| `read_operation_logs` | 按独立日志游标读取操作事件 |

所有参数使用严格 JSON Schema，不接受未声明字段。旧 `send_input`、`send_key`、`read_output` 工具已删除，客户端需要重新发现工具。
服务器 UUID 用于连接和文件管理；终端 I/O 必须使用 `connection_id`。

`list_terminals.query` 不区分大小写，匹配名称、UUID、IPv4/IPv6、系统、分组、地区。
多个空格分隔的关键词取并集，可一次查单台或多台。空查询返回全部；每项仅含 UUID、名称和在线状态。
需要区分同名节点时，使用列表中的唯一 UUID 调用 `get_terminal`，按需查看 IPv4/IPv6、系统、分组、地区、CPU、内存、磁盘、流量等详情。批量列表不返回这些字段。

## 统一输入、等待和读取

以下示例是 `tools/call` 的参数：

```json
{"name":"send_and_read","arguments":{"connection_id":"<ID>","text":"pwd\"Enter\"","wait":{"mode":"idle","idle_ms":300,"max_wait_ms":10000},"read":{"mode":"new"}}}
```

`text` 可省略或为空。默认解析双引号包裹的已知按键，例如 `pwd"Enter"`、`"Ctrl+C"`、`"Tab"`、`"Up"`。
不自动追加回车；需要原样输入引号中的键名时设 `parse_keys=false`，再直接使用 `\r` 发送回车。
未知键名、未闭合引号和其他字符保持原样。支持 Enter、Ctrl+A 至 Ctrl+Z、Tab、Escape、Backspace、Delete、方向键、Home/End、PageUp/PageDown、Shift+Tab。
Shell 引号和反斜杠不会关闭按键解析，脚本、JSON 或含字面键名的命令应关闭解析。

`wait` 省略时等同于 `{"mode":"time","wait_ms":0,"max_wait_ms":0}`。显式传入时，各模式的字段及 `max_wait_ms` 均须提供：

| `wait.mode` | 条件参数 | 返回时机 |
| --- | --- | --- |
| `time` | `wait_ms`、`max_wait_ms` | 指定时间到达，或达到独立最大等待 |
| `idle` | `idle_ms`、`max_wait_ms` | 收到本次观察的新输出后，连续指定毫秒没有更多字节 |
| `match` | `pattern`、`after_ms`、`max_wait_ms`；可选 `regex` | 新输出首次出现关键词或正则后，继续等指定毫秒 |

所有时间使用毫秒，允许 0，最大 600000。`max_wait_ms=0` 对任一模式都立即刷新；静默或匹配延迟为 0 时，条件一旦满足立即返回。
静默模式尚未收到新字节时会等到新输出或最大时限。关键词只扫描本次调用开始之后的新输出，支持跨字节分片和 UTF-8 分片；历史关键词不会误触发。
正则匹配有独立执行超时，复杂模式超时返回 `wait_reason=pattern_timeout` 和已有输出，不会无限阻塞。
条件扫描受每窗口最近 4 MiB 保留范围限制；过快输出被淘汰时会通过 `dropped_bytes` 报告缺失。

输入发送成功之后开始计算等待时限。实际输入前就记录输出位置，因此即时回显不会漏掉。
连接完全关闭时提前返回；`reconnecting` 不视为关闭，静默条件会暂停并在恢复后重新计时，最大等待仍生效。
发送失败的输入不会自动重放，恢复后先刷新检查输出，避免重复执行。

`read` 默认 `{"mode":"all"}`：

| `read.mode` | 额外参数 | 返回范围 |
| --- | --- | --- |
| `all` | 无 | 当前全部保留内容，不施加旧的 64 KiB/200 行限制 |
| `new` | 无 | 本次输入前记录的位置之后新增内容；未输入时从本次调用开始观察 |
| `tail_bytes` | `count` | 最后 count 个原始字节 |
| `tail_lines` | `count` | 最后 count 行，长行不额外按字节截断 |

行以 LF 结束，末尾 LF 不产生虚假的空行；CR 和 ANSI 控制码保持原样。
返回 `text`、`bytes_sent`、`bytes_returned`、`input_output_start`、`buffer_start`、`output_end`、`cursor`、`next_cursor`、`dropped_bytes`、状态与 `wait_reason`。
位置均为绝对字节位置；这些位置用于说明范围，不是旧 `cursor` 输入参数。
`include_base64=true` 额外返回 `data_base64`，默认省略。截取末尾字节可能切断 UTF-8 字符，精确字节应使用 Base64。
原始 PTY 的 stdout/stderr 合并，没有可靠的退出码或命令结束边界。

快速刷新末尾 100 行：

```json
{"name":"send_and_read","arguments":{"connection_id":"<ID>","read":{"mode":"tail_lines","count":100}}}
```

正则匹配后延迟：

```json
{"name":"send_and_read","arguments":{"connection_id":"<ID>","text":"run-job\"Enter\"","wait":{"mode":"match","pattern":"(?m)^DONE [0-9]+$","regex":true,"after_ms":500,"max_wait_ms":60000},"read":{"mode":"new"}}}
```

## 取消等待与并发

长等待可以提供唯一的 `wait_id`，如 `job-1`。同一客户端在另一次 `send_and_read` 中传 `cancel_wait_id=job-1`，原调用会提前返回最后输出，`wait_reason=cancelled`。
取消等待不会中断远端前台程序；需要中断时发送 `text='"Ctrl+C"'`。等待期间不持有输入锁，允许共同输入。
`cancel_wait_id` 只匹配同一客户端、同一 `connection_id` 的活动等待；不存在或重复 ID 会明确报错。

原生 `notifications/cancelled` 也会停止后台等待。无状态 HTTP 客户端应提供稳定且唯一的 `Mcp-Client-Id` 请求头，并在取消通知中保持相同值，避免同机或同代理客户端使用相同 requestId 时相互混淆。
同一客户端不能同时复用 requestId。没有自定义头时按来源 IP 和 User-Agent 分组。
协议取消后，SDK 或客户端可能已关闭响应流、或只接收协议取消错误，因此最后输出不保证能随原请求送达；再次零毫秒刷新仍可读取保留内容。
需要可靠接收最后输出时使用 `wait_id/cancel_wait_id`，保持原 HTTP 请求打开。

最多同时进行 16 个有时限的等待，为刷新、中断和其他操作保留处理能力。
每次输入最多 64 KiB；每窗口保留最近 4 MiB；最多 32 个活动窗口及最近 32 个关闭窗口。
读取不消费数据，不会影响其他读取者，也不重置终端空闲时间。

桌面协助终端支持直接键盘输入、中文输入法、Ctrl+V 粘贴和 Ctrl+Shift+C 复制。
“新连接自动显示窗口”默认关闭，切换后立即保存。终端为独立、非置顶的无边框窗口，与主界面共用标题栏，四边四角均可缩放。
关闭窗口只隐藏，不会断开连接；结束连接请使用 MCP 页面中的“断开选中终端”。

## 用户与 Agent 的连接详情

管理页的“连接详情”和 MCP `get_connection` 使用相同数据：服务器 UUID、名称、IP、系统，窗口 ID/备注，连接状态、脱敏的远端地址，创建/连接/断开时间，最近输入/输出/心跳时间，输入来源，终端尺寸，收发字节数，缓冲范围、粘性重连次数与关闭原因。

协助者详情还包括 SSH 客户端地址、用户名、客户端版本、加密算法、终端类型、尺寸、连接时间与各自的数据量。服务详情包含 SSH Host Key 的 SHA-256 指纹，可用于客户端核对。

MCP 使用无状态 HTTP；“最近 HTTP 客户端”记录来源 IP、User-Agent、请求次数和最近活动，不代表当前仍保持着连接。面板在线状态是最近一次快照，可显式刷新。任何详情都不提供 Cookie、密码、2FA 或 Host Key 私钥。

## 协助模式

1. 启动 MCP 并建立终端窗口。
2. 进入“协助 SSH”，选择一个**已连接窗口**。
3. 设置本机端口（默认 `12298`）、用户名（默认 `assist`）和独立密码。
4. 点击“启用协助模式”，复制 SSH 命令，使用 SSH 客户端连接，并输入协助密码。

```text
ssh -tt -p 12298 assist@127.0.0.1
```

协助端口始终只监听 `127.0.0.1`，不受 MCP 公网开关影响。每个 SSH TCP 连接支持一个交互 shell，最多 8 个协助客户端；同一连接上还可使用 SFTP 子系统；不支持 exec 与端口转发。

所有协作者与 AI 使用同一个 PTY，看到同一份原始输出，输入也进入同一个前台程序。新协助者先收到最近 64 KiB 的输出，随后持续跟随。输入回显由远端终端决定；远端关闭 ECHO 时不会额外广播输入，以免显示密码。

用户和 AI 的输入按发送次序写入；同一个窗口没有各自独立的命令行或互斥编辑权，应根据当前前台程序协调输入。不同任务应分别新建窗口。

关闭 SSH 连接或关闭协助模式只退出协助，不会断开 AI 终端。输入 `exit`、Ctrl+D 或执行结束 shell 的操作仍会影响共享会话。目标终端断开会自动停止其协助端口；停止 MCP 或退出应用会清理全部 MCP 与协助连接。

## 粘性会话

远端节点或面板网络短暂中断时，窗口不会立刻结束，而是进入“重连中”：程序在粘性重连时限
（默认 5 分钟，可在设置中调整，0 表示关闭）内退避重试，并用面板的会话标识重挂同一个终端。
重挂不需要新的 2FA 验证码。

- 界面与 `list_connections` / `get_connection` 中的状态为 `reconnecting`，列表里标黄，终端窗口状态栏显示“重连中”。
- `list_connections`、`get_connection` 和 `send_and_read` 返回 `status_message="连接断开，正在自动重连..."`。
  重连期间发送文本、按键或调整尺寸也会明确提示等待原连接恢复；应保留 `connection_id`，不要新建窗口。
- 输入或心跳发送先发现断线时，同样启动粘性重连。失败的输入不会自动重放；连接恢复后先检查输出，避免重复执行。
- 重挂期间过滤面板发出的完整 TEXT 提示帧 `等待被控端连接 waiting for agent...\n`，不写入 MCP 输出或转发到 SSH。
  首次连接提示仍保留；正常 PTY 的 BINARY 输出、包含相似文字的命令输出均保留。
- 重连成功后状态回到 `connected`，`reconnect_attempts` 记录累计尝试次数；输出游标与已保留的输出不变。
- 超过时限、或面板已回收会话（例如远端 shell 已退出）时，窗口转为 `error` 并给出原因。
- 主动“断开终端”会通知面板结束会话，不会进入重连。

本地 SSH 反代端口使用同一套粘性机制，时限由 `[ssh]` 节的 `ssh_sticky_minutes` 控制
（默认 5 分钟，0 表示关闭）。重连过程会写入该服务器的日志流：掉线时记录
“会话断开，正在重建连接”，重挂成功后记录“当前会话已重建”。

## MCP 原生文件管理

`file_operation` 直接调用面板文件接口，无需新建终端或模拟 Shell。

| operation | 必填字段（另需 server_uuid） | 可选字段 |
| --- | --- | --- |
| roots | 无 | 无 |
| list | path | offset（默认 0）、limit（默认 100，最大 1000） |
| stat | path | 无 |
| read | path | offset、length（默认 65536，最大 262144）、include_base64 |
| upload | path、local_path | 无 |
| download | path、local_path | 无 |
| mkdir | path | mode（八进制字符串） |
| rename | path、destination | 无 |
| chmod | path、mode | 无 |
| chown | path、uid/gid 至少一个 | 另一个属主字段 |
| delete | path | recursive（默认 false） |

远端路径为绝对虚拟路径：Linux 使用 `/tmp/file`，Windows 使用 `/C/path/file`。
`local_path` 必须是运行 MCP 服务的电脑上的绝对文件路径，不是远程 MCP 客户端的路径。
上传直接流式读取本地文件，最大 8 GiB；下载独占创建本地文件，拒绝覆盖同名本地文件。
下载失败可能保留部分文件，请检查后自行处理。读取默认返回文本，可显式获取 Base64。

```json
{"name":"file_operation","arguments":{"server_uuid":"<UUID>","operation":"upload","local_path":"C:/files/abc.txt","path":"/tmp/abc.txt"}}
```

普通上传、创建目录和重命名先检查目标：已存在时失败并返回“存在同名文件或目录”。
本服务会串行化同节点文件操作；面板没有原子无覆盖选项，因此与外部程序同时写同一路径仍存在竞争窗口。
只支持面板原生能力，不提供“上传 abc 替换 def”、备份后替换、编辑后覆盖等组合流程。
删除非空目录必须显式设 `recursive=true`；拒绝删除文件系统根目录。
文件修改权限、属主及重命名均走原生 RPC；节点离线、权限不足或不支持时返回可读错误。
日志记录操作类别和字节数，不写入文件内容、路径、凭据或远端异常正文。

## SFTP 文件传输

本地 SSH 代理端口和协助 SSH 端口都提供 SFTP 子系统，用法与普通 SSH 相同：

```text
sftp -P 12296 root@127.0.0.1
scp -P 12296 ./local.bin root@127.0.0.1:/tmp/remote.bin
```

- 认证沿用对应端口的用户名与密码（代理端口用 `ssh_username`/`ssh_password`，协助端口用协助用户名与密码）。
- 路径采用面板的虚拟路径形式：Linux 节点是普通绝对路径，Windows 节点把盘符写成 `/C/...`。
- 列目录、`stat`、`mkdir`、删除、改名、`chmod`/`chown` 走面板的 `admin:file*` 接口；读写内容走面板的二进制传输通道，读取支持 `Range` 分段。
- 文件能力依赖节点在线且未禁用网页控制；节点离线或旧版 Agent 会返回明确错误。
- 目录删除沿用 SFTP 语义：`rmdir` 只删除空目录，`remove` 可删除文件或整棵目录树。
- 符号链接只读不建：可以列出并跟随，但 `symlink` 返回不支持。
- 文件操作会写入日志流：本地 SSH 代理端口写入该服务器的实时指令流，协助端口写入 MCP 操作日志流。打开、上传、下载、重命名、建目录、删除与改权限都会记录，失败时附带原因。

## 公网访问

“允许公网访问”默认关闭，每次打开应用都会恢复关闭。开启后 MCP 监听 `0.0.0.0`，仍要求密码。应用不会自动修改防火墙、路由器或端口映射。

公网 IP 可直接连接；使用域名或 HTTPS 反向代理时，把外部完整 URL 填入“对外 URL”，例如 `https://mcp.example.com/mcp`，这样复制出的连接说明与 Host/Origin 校验都使用该地址。反向代理应保留合法 Host 和 Authorization 请求头，并将对应路由转发到本机 MCP 端口。

跨公网应使用 HTTPS 反向代理或加密隧道，保护 Bearer 密码和终端内容。程序自身提供 HTTP 监听，不配置 TLS 证书。只有正确鉴权的客户端能读取终端、操作日志或发送输入；鉴权失败限流不会封锁同一代理来源下持有正确密码的客户端。

## 操作日志

界面持续显示连接、发送、按键、中断、尺寸变化、输出读取、协助加入/退出、鉴权失败等事件。日志只记录操作元数据和字节数，不记录输入正文、密码、Cookie、2FA、私钥或完整网络异常正文。

`read_operation_logs` 使用整数游标；继续读取时传上次的 `next_cursor`。实时缓存保留最近 8000 条，`dropped_entries` 表示淘汰量；导出最多保留 4 个 4 MiB 日志段。界面清空显示不会清除导出文件。

日志位于应用运行时临时目录，退出时清理。需要保存时提前导出。“暂停滚动”仅暂停日志显示，记录仍继续。终端原始输出另行读取或从窗口导出。

## 开发验证

```powershell
$ErrorActionPreference = 'Stop'
python -m pip install -r requirements.txt
python main.py --check-mcp
if ($LASTEXITCODE -ne 0) { throw 'Application installation check failed' }
```

CI 执行源码编译、程序安装自检、Windows EXE 构建及打包后自检。仓库不包含独立测试脚本；实际面板的认证、2FA、文件操作和网络连通性仍需用本机配置连接验证。

源码或 EXE 均支持 `--check-mcp`：验证 Qt 加载、隐藏窗口渲染，再在本机临时端口验证 MCP 依赖、鉴权和工具发现，不连接面板，不生成配置文件，成功退出码为 0。可加 `--check-mcp-report <文件路径>` 将结果写入 UTF-8 文件；Qt DLL 等依赖导入失败也会写入报告。

Windows 打包使用 `python -m PyInstaller --noconfirm KomariSSHProxy.spec`。该配置将构建进程的 DLL 搜索 PATH 限制为 Python 与 Windows 系统目录，避免其他软件的同名 ICU/CRT DLL 被误打包。Qt 自身的库和插件仍由 PyInstaller 的 Qt hook 收集。变更依赖或构建环境后，应使用新的构建目录或清理旧的构建缓存，再运行 EXE 自检。

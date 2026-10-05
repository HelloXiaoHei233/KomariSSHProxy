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
| `get_service_info` | MCP 监听、最近 HTTP 请求活动、协助 SSH 客户端与资源限制 |
| `list_terminals` | 所有面板服务器、最近状态、每台服务器的窗口数；`refresh=true` 重新获取面板数据 |
| `list_connections` | 所有活动 MCP 窗口；`include_closed=true` 同时显示保留的历史 |
| `get_connection` | 指定窗口的服务器、端点、尺寸、时间、流量、输出范围和协作者详情 |
| `connect_terminal` | 新建独立窗口，返回窗口详情、连接前与连接后的窗口数 |
| `disconnect_terminal` | 断开指定窗口；重复调用安全 |
| `send_input` | 按原样输入文本，**不追加回车** |
| `send_key` | 发送 Enter、Ctrl+A 至 Ctrl+Z、Tab、Escape、Backspace、Delete、方向键、Home/End、PageUp/PageDown、Shift+Tab |
| `resize_terminal` | 改变共享终端尺寸 |
| `read_output` | 按行、字节与独立游标读取原始输出 |
| `read_operation_logs` | 按独立日志游标读取操作事件 |

所有参数使用严格 JSON Schema；不接受拼错的额外字段。面板 UUID 仅用于新建连接；后续必须使用返回的 `connection_id`。

## 输入与手动回车

示例为 `tools/call` 的参数，`<ID>` 替换为实际窗口 ID：

```json
{"name":"send_input","arguments":{"connection_id":"<ID>","text":"pwd"}}
```

这一步只把 `pwd` 输入终端，不会执行。随后显式回车：

```json
{"name":"send_key","arguments":{"connection_id":"<ID>","key":"Enter"}}
```

中断前台程序：

```json
{"name":"send_key","arguments":{"connection_id":"<ID>","key":"Ctrl+C"}}
```

`send_input` 内已有的换行或控制字符也会按原样发送。接口不会自动删除或追加字符。
它没有“命令已完成”“标准错误”或“退出码”的可靠边界，Agent 应根据实际终端输出判断下一步。错误重试前先检查输出，避免重复执行已收到的输入。

桌面终端窗口可直接输入按键，Enter 发送回车，Ctrl+C 中断，Ctrl+Shift+C 复制选中文字。窗口底部的文本框用于先编辑后发送，发送文本后仍需点击“Enter 回车”。中文输入法提交文字也可以发送。窗口显示的是文本预览；完整 ANSI 终端体验使用协助 SSH。

## 输出流与保留范围

```json
{"name":"read_output","arguments":{"connection_id":"<ID>","max_lines":100,"max_bytes":65536}}
```

默认从保留输出的最前端读取，最多 200 行、65536 字节，以先到的限制为准。`max_lines=null` 可只限制字节。行以 LF 为结束符，CR 是原始终端流的一部分。

后续读取把 `next_cursor` 传入 `cursor`，可加 `wait_ms`（最大 30000）等待新输出；每个读取者维护自己的游标，相互不消费数据。使用 `tail=true` 查看末尾时，不要同时传 `cursor`。

返回的 `text` 是 UTF-8 预览；`data_base64` 保留精确原始字节。字节限制可能切在 UTF-8 字符中间，处理二进制、ANSI 或严格拼接时应解码 `data_base64` 并使用增量解码器。

- 每次最多读取 256 KiB；最多指定 10000 行。
- 每次最多输入 64 KiB。
- 每个窗口保留最近 4 MiB。`buffer_start` / `output_end` 是绝对字节位置；`dropped_bytes` 表示较早内容已淘汰。
- 最多 32 个活动窗口，保留最近 32 个关闭窗口的记录；关闭窗口仍可读取其保留的输出。
- “导出保留的输出”保存该窗口当前保留的原始字节，不是无限期归档。
- 可设置无输入/输出时自动断开的分钟数，默认不自动断开。心跳和读取输出不会重置空闲时间，仍在输出的任务会继续保留。

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

- 界面与 `list_connections` / `get_connection` 中的状态为 `reconnecting`，列表里标黄，终端窗口标题显示“重连中”。
- 重连成功后状态回到 `connected`，`reconnect_attempts` 记录累计尝试次数；输出游标与已保留的输出不变。
- 超过时限、或面板已回收会话（例如远端 shell 已退出）时，窗口转为 `error` 并给出原因。
- 主动“断开终端”会通知面板结束会话，不会进入重连。

本地 SSH 反代端口使用同一套粘性机制，时限由 `[ssh]` 节的 `ssh_sticky_minutes` 控制
（默认 5 分钟，0 表示关闭）。重连过程会写入该服务器的日志流：掉线时记录
“会话断开，正在重建连接”，重挂成功后记录“当前会话已重建”。

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
python -m unittest discover -s tests -v
python main.py --check-mcp
```

测试覆盖独立多窗口、显式回车与快捷键、字节游标、缓冲淘汰、粘性重连、SFTP 子系统、鉴权和参数错误、官方 MCP 客户端、真实本机 Paramiko 协助连接，以及 PyQt 窗口交互。远端 Komari 在测试中被模拟；实际面板的认证、2FA 和网络连通性仍需用本机配置连接验证。

源码或 EXE 均支持 `--check-mcp`：验证 Qt 加载与隐藏窗口渲染，再在本机临时端口验证 MCP 依赖、鉴权和工具发现，不连接面板，不生成配置文件，成功退出码为 0。可加 `--check-mcp-report <文件路径>` 将结果写入 UTF-8 文件；Qt DLL 等依赖导入失败也会写入报告。

Windows 打包使用 `python -m PyInstaller --noconfirm KomariSSHProxy.spec`。该配置将构建进程的 DLL 搜索 PATH 限制为 Python 与 Windows 系统目录，避免其他软件的同名 ICU/CRT DLL 被误打包。Qt 自身的库和插件仍由 PyInstaller 的 Qt hook 收集。变更依赖或构建环境后，应使用新的构建目录或清理旧的构建缓存，再运行 EXE 自检。

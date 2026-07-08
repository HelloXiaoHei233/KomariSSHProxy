# Komari SSH Proxy

Komari SSH Proxy 是一个 SSH 代理工具，用于把 Komari 面板的网页终端 WebSocket 桥接成标准 SSH 端口，而不必依赖网页终端。

> 仅用于你拥有或已获授权管理的 Komari 面板和节点。

![登录凭据](docs/images/login.png)

## 功能

- 支持从浏览器开发者控制台复制出的请求中智能所需凭据。
- 显示在线状态、CPU、内存、磁盘、流量、IP 和代理状态。
- 支持多台服务器同时开启本地 SSH 代理
- 实时指令流支持清空和导出 TXT。
- 可选允许公网访问
- 由于 Komari 网页终端接口只提供交互式终端流，不提供文件协议能力，因此无法处理 SFTP 请求。

## 界面

![服务器列表](docs/images/server-list.png)

![SSH 代理页面](docs/images/proxy-page.png)

截图中的服务器名、IP 和日志均为示例数据。

## 从源码运行

推荐 Python 3.12 或更新版本。

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python main.py
```

## 配置文件

程序首次打开时，会在脚本或 exe 同目录生成 `KomariSSHProxy.ini`。

字段说明：

- `base_url`：Komari 面板地址，例如 `https://komari.example.com`
- `user_agent`：请求面板接口时使用的浏览器 User-Agent
- `session_token`：面板登录会话 Cookie
- `cf_clearance`：Cloudflare 放行 Cookie，如面板站点需要则填写
- `sensorsdata2015jssdkcross`：面板浏览器环境中的可选 Cookie
- `language`：语言 Cookie，默认 `zh-CN`
- `extra_cookies`：额外 Cookie，格式为 `a=b; c=d`
- `ssh_password`：默认本地 SSH 登录密码，首次运行生成，用户修改后更新
- `ssh_host_key_pem`：本地 SSH 服务端 Host Key，首次运行生成
- `app_theme`：`system`、`dark` 或 `light`

端口、用户名、公网访问、终端尺寸、2FA 验证码仍是运行时配置，不会写入 ini。


## 智能识别

登录页支持直接粘贴浏览器开发者工具复制出来的请求：

- PowerShell `Invoke-WebRequest`
- curl 命令

点击识别后，程序会尝试提取面板地址、User-Agent 和已知 Cookie 字段。

## 本地 SSH 代理

默认运行时设置：

- 端口：`12296`
- 用户名：`root`
- 密码：首次启动生成并持久化到 `KomariSSHProxy.ini`
- 监听地址：`127.0.0.1`
- 允许公网访问：关闭
- 终端尺寸：`120` 列、`36` 行

开启“允许公网访问”后，本地 SSH 服务会监听 `0.0.0.0`。开启前请确认密码强度和防火墙规则。


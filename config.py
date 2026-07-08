from __future__ import annotations

import configparser
from dataclasses import dataclass, field
from pathlib import Path
import secrets
import sys
from urllib.parse import urlparse


APP_DISPLAY_NAME = "Komari SSH Proxy"
APP_CONFIG_NAME = "KomariSSHProxy"


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resource_path(relative_path: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / relative_path


APP_DIR = app_dir()
CONFIG_PATH = APP_DIR / f"{APP_CONFIG_NAME}.ini"

DEFAULT_USER_AGENT = "Mozilla/5.0 (compatible; KomariSSHProxy/1.0)"


def generate_ssh_password() -> str:
    return "kp-" + secrets.token_urlsafe(12)


INI_FIELDS = [
    (
        "auth",
        [
            (
                "base_url",
                "base_url为面板基础地址，例如 https://komari.example.com，用于拼接 HTTP API 和 WebSocket 地址，此项必填。",
                lambda cfg: normalize_base_url(cfg.base_url),
            ),
            (
                "user_agent",
                "user_agent为浏览器 User-Agent，用于让面板 API 请求尽量匹配浏览器环境，此项必填。",
                lambda cfg: cfg.user_agent,
            ),
            (
                "session_token",
                "session_token为面板登录会话 Cookie，用于管理员接口鉴权，此项必填。",
                lambda cfg: cfg.session_token,
            ),
            (
                "cf_clearance",
                "cf_clearance为 Cloudflare 放行 Cookie，用于通过面板站点的人机/安全校验，此项通常必填。",
                lambda cfg: cfg.cf_clearance,
            ),
            (
                "sensorsdata2015jssdkcross",
                "sensorsdata2015jssdkcross为浏览器中的 sensorsdata Cookie，用于还原面板请求 Cookie 环境，此项建议保留。",
                lambda cfg: cfg.sensorsdata2015jssdkcross,
            ),
            (
                "language",
                "language为面板语言 Cookie，例如 zh-CN，用于指定面板返回语言，此项选填，默认 zh-CN。",
                lambda cfg: cfg.language or "zh-CN",
            ),
            (
                "extra_cookies",
                "extra_cookies为额外 Cookie，格式为 a=b; c=d，用于保存未来新增或无法归类的 Cookie，此项选填，可留空。",
                lambda cfg: cfg.extra_cookies,
            ),
        ],
    ),
    (
        "ssh",
        [
            (
                "ssh_password",
                "ssh_password为默认本地 SSH 登录密码，首次运行会自动生成随机值并保存；用户在界面修改后会更新此项。",
                lambda cfg: cfg.ssh_password,
            ),
            (
                "ssh_host_key_pem",
                "ssh_host_key_pem为本地 SSH 服务端 Host Key 私钥，首次运行会自动生成并保存；请不要泄露或提交此项。",
                lambda cfg: cfg.ssh_host_key_pem,
            ),
        ],
    ),
    (
        "app",
        [
            (
                "app_theme",
                "app_theme为界面主题，可选 system、dark、light，用于保存右上角主题按钮状态，此项选填，默认 system。",
                lambda cfg: _theme(cfg.app_theme),
            ),
        ],
    ),
]


def normalize_base_url(value: str) -> str:
    value = (value or "").strip().rstrip("/")
    if not value:
        return ""
    if "://" not in value:
        value = "https://" + value
    return value.rstrip("/")


def render_ini(config: "AgentConfig") -> str:
    lines = [
        f"# {APP_DISPLAY_NAME} 配置文件。",
        "# 允许添加以 # 或 ; 开头的独立注释行。",
        "# 不建议把注释放在字段同一行尾部，因为 Cookie 里可能包含分号或特殊字符。",
        "# SSH 密码和 Host Key 会作为默认值保存；端口、用户名、公网访问、终端尺寸、2FA 验证码仍为运行时配置。",
        "",
    ]
    for section, fields in INI_FIELDS:
        lines.append(f"[{section}]")
        for key, comment, getter in fields:
            lines.append(f"# {comment}")
            lines.append(f"{key} = {_ini_value(getter(config))}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _ini_value(value: object) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    parts = text.split("\n")
    if len(parts) == 1:
        return parts[0]
    return parts[0] + "".join(f"\n    {part}" for part in parts[1:])


@dataclass
class AgentConfig:
    base_url: str = ""
    user_agent: str = DEFAULT_USER_AGENT
    session_token: str = ""
    cf_clearance: str = ""
    sensorsdata2015jssdkcross: str = ""
    language: str = "zh-CN"
    extra_cookies: str = ""
    terminal_2fa_code: str = ""
    ssh_port: int = 12296
    ssh_username: str = "root"
    ssh_password: str = field(default_factory=generate_ssh_password)
    ssh_host_key_pem: str = ""
    public_access: bool = False
    terminal_cols: int = 120
    terminal_rows: int = 36
    app_theme: str = "system"

    @classmethod
    def load(cls, path: Path = CONFIG_PATH) -> "AgentConfig":
        parser = configparser.ConfigParser(interpolation=None)
        parser.read(path, encoding="utf-8")
        cfg = cls()
        auth = parser["auth"] if parser.has_section("auth") else {}
        ssh = parser["ssh"] if parser.has_section("ssh") else {}
        app = parser["app"] if parser.has_section("app") else {}

        cfg.base_url = normalize_base_url(auth.get("base_url", cfg.base_url))
        cfg.user_agent = auth.get("user_agent", cfg.user_agent)
        cfg.session_token = auth.get("session_token", cfg.session_token)
        cfg.cf_clearance = auth.get("cf_clearance", cfg.cf_clearance)
        cfg.sensorsdata2015jssdkcross = auth.get(
            "sensorsdata2015jssdkcross", cfg.sensorsdata2015jssdkcross
        )
        cfg.language = auth.get("language", cfg.language)
        cfg.extra_cookies = auth.get("extra_cookies", cfg.extra_cookies)
        saved_password = ssh.get("ssh_password", "").strip()
        if saved_password:
            cfg.ssh_password = saved_password
        cfg.ssh_host_key_pem = ssh.get("ssh_host_key_pem", cfg.ssh_host_key_pem).strip()
        cfg.app_theme = _theme(app.get("app_theme", auth.get("app_theme", cfg.app_theme)))
        if path.exists() and (not saved_password or not cfg.ssh_host_key_pem):
            cfg.save(path)
        return cfg

    def save(self, path: Path = CONFIG_PATH) -> None:
        with path.open("w", encoding="utf-8") as f:
            f.write(render_ini(self))

    @property
    def host(self) -> str:
        return urlparse(normalize_base_url(self.base_url)).netloc

    @property
    def origin(self) -> str:
        parsed = urlparse(normalize_base_url(self.base_url))
        return f"{parsed.scheme}://{parsed.netloc}"

    def cookie_pairs(self) -> list[tuple[str, str]]:
        pairs: list[tuple[str, str]] = []
        if self.sensorsdata2015jssdkcross:
            pairs.append(("sensorsdata2015jssdkcross", self.sensorsdata2015jssdkcross))
        if self.language:
            pairs.append(("language", self.language))
        if self.session_token:
            pairs.append(("session_token", self.session_token))
        if self.cf_clearance:
            pairs.append(("cf_clearance", self.cf_clearance))

        known = {name for name, _ in pairs}
        raw = self.extra_cookies.replace("\n", ";")
        for chunk in raw.split(";"):
            chunk = chunk.strip()
            if not chunk or "=" not in chunk:
                continue
            name, value = chunk.split("=", 1)
            name = name.strip()
            if not name or name in known:
                continue
            pairs.append((name, value.strip()))
            known.add(name)
        return pairs

    def cookie_header(self) -> str:
        return "; ".join(f"{name}={value}" for name, value in self.cookie_pairs())

    def has_login_credentials(self) -> bool:
        return bool(normalize_base_url(self.base_url) and self.session_token)


def _theme(value: str | None) -> str:
    value = (value or "system").strip().lower()
    return value if value in {"system", "dark", "light"} else "system"

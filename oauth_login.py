"""External-browser OAuth with a loopback page and explicit callback handoff."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import hmac
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import secrets
import threading
import time
from typing import Callable
from urllib.parse import parse_qs, urljoin, urlsplit

from config import AgentConfig, normalize_base_url
from komari_client import KomariClient
from panel_auth import (PanelLoginError, checked_config, login_error_message,
                        request_origin, response_cookie, without_session)


MAX_CALLBACK_LENGTH = 16384
FLOW_TIMEOUT_SECONDS = 300


@dataclass(repr=False)
class OAuthFlow:
    config: AgentConfig
    authorization_url: str
    state: str
    state_required: bool
    provider: str = ""
    created_at: float = field(default_factory=time.monotonic)

    @classmethod
    def begin(cls, config: AgentConfig) -> "OAuthFlow":
        config = without_session(checked_config(config))
        client = KomariClient(config)
        try:
            with client.session.get(f"{client.base_url}/api/public", headers=client._http_headers(),
                                    timeout=20, allow_redirects=False) as response:
                payload = response.json()
                settings = payload.get("data", {}) if isinstance(payload, dict) else {}
                if response.status_code != 200 or not isinstance(settings, dict):
                    raise PanelLoginError("无法读取面板 OAuth 配置，请检查面板地址和站点访问限制。")
                provider = settings.get("oauth_provider", "")
                if not isinstance(provider, str):
                    raise PanelLoginError("面板返回的 OAuth 提供商配置格式不正确。")
                state_required = provider != "qq"
            with client.session.get(f"{client.base_url}/api/oauth", headers=client._http_headers(),
                                    timeout=20, allow_redirects=False) as response:
                state = response_cookie(response, "oauth_state", config)
                redirect = response.headers.get("Location", "")
                location = urljoin(client.base_url + "/", redirect)
                parsed = urlsplit(location)
                if (response.status_code not in {302, 303} or not redirect or not state
                        or len(state) > 4096 or any(not (char.isascii() and (char.isalnum() or char in "-_.~")) for char in state)
                        or parsed.scheme not in {"http", "https"} or not parsed.hostname
                        or parsed.username or parsed.password or parsed.fragment
                        or any(ord(char) < 32 or ord(char) == 127 for char in redirect)):
                    raise PanelLoginError("无法开始 OAuth，请确认面板已启用 OAuth，并检查地址和站点访问限制。")
                parsed.port
                values = parse_qs(parsed.query, keep_blank_values=True).get("state", [])
                if len(values) > 1 or (state_required and (len(values) != 1 or not hmac.compare_digest(values[0].encode("utf-8"), state.encode("ascii")))):
                    raise PanelLoginError("面板返回的 OAuth state 不一致，请重新开始授权。")
                return cls(config, location, state, state_required, provider=provider)
        finally:
            client.session.close()

    def callback_parameters(self, callback_url: str) -> dict[str, str]:
        if time.monotonic() - self.created_at >= FLOW_TIMEOUT_SECONDS:
            raise PanelLoginError("本次授权已过期，请重新开始授权。")
        if not isinstance(callback_url, str) or len(callback_url) > MAX_CALLBACK_LENGTH:
            raise PanelLoginError("回调链接过长或格式不正确。")
        try:
            parsed = urlsplit(callback_url.strip())
            expected = urlsplit(self.config.base_url + "/api/oauth_callback")
            valid = (request_origin(callback_url.strip()) == request_origin(self.config.base_url)
                     and parsed.path == expected.path and not parsed.username
                     and not parsed.password and not parsed.fragment)
            values = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=20)
        except ValueError:
            valid, values = False, {}
        if not valid or any(len(items) != 1 for items in values.values()):
            raise PanelLoginError("请粘贴当前面板 /api/oauth_callback 的完整回调链接。")
        state = values.get("state", [""])[0]
        if self.state_required and (not state or not hmac.compare_digest(state.encode("utf-8"), self.state.encode("ascii"))):
            raise PanelLoginError("回调不属于本次授权，请使用当前链接重新授权。")
        if "error" in values:
            raise PanelLoginError("授权已被取消或拒绝，请重新开始授权。")
        code = values.get("code", [""])[0]
        if not code or len(code) > 8192 or any(ord(char) < 33 for char in code):
            raise PanelLoginError("回调缺少有效授权码，请复制授权后地址栏中的完整链接。")
        # QQ's aggregation state is independent of Komari's cookie/cache state.
        params = {"code": code, "state": self.state}
        if "type" in values:
            params["type"] = values["type"][0]
        return params

    def exchange(self, params: dict[str, str]) -> AgentConfig:
        client = KomariClient(self.config)
        try:
            headers = client._http_headers()
            headers["Cookie"] = "; ".join(filter(None, (self.config.cookie_header(), f"oauth_state={self.state}")))
            # Keep the provider's registered callback URI unchanged.
            with client.session.get(f"{client.base_url}/api/oauth_callback", params=params,
                                    headers=headers, timeout=20, allow_redirects=False) as response:
                token = response_cookie(response, "session_token", self.config)
                if (response.status_code not in {302, 303} or not token or len(token) > 4096
                        or any(ord(char) < 33 or ord(char) > 126 or char == ";" for char in token)):
                    raise PanelLoginError("授权回调未能建立会话；请确认外部账号已绑定 Komari，然后重新开始授权。")
                return replace(self.config, session_token=token)
        finally:
            client.session.close()


class OAuthCallbackServer:
    """Only listens while unauthenticated; never logs callback URLs."""

    def __init__(self, on_session: Callable[[AgentConfig, int], None]):
        self.on_session = on_session
        self._lock = threading.RLock()
        self._operation = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._config = AgentConfig()
        self._flow: OAuthFlow | None = None
        self._generation = 0
        self._busy = False
        self._pending_session = False
        self._path = ""

    @property
    def url(self) -> str:
        with self._lock:
            return f"http://127.0.0.1:{self._server.server_port}{self._path}" if self._server else ""

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    @property
    def authorization(self) -> tuple[str, str]:
        with self._lock:
            if self._flow and time.monotonic() - self._flow.created_at >= FLOW_TIMEOUT_SECONDS:
                self._flow = None
            if self._flow is None or self._busy or self._pending_session:
                return "", ""
            return self._flow.authorization_url, self._flow.provider

    def configure(self, config: AgentConfig) -> None:
        config = replace(config, base_url=normalize_base_url(config.base_url))
        with self._lock:
            current = (self._config.base_url, self._config.user_agent, self._config.cookie_header())
            incoming = (config.base_url, config.user_agent, config.cookie_header())
            if current != incoming:
                self._generation += 1
                self._flow = None
                self._pending_session = False
            self._config = replace(config)

    def set_busy(self, busy: bool) -> None:
        with self._lock:
            if busy:
                self._generation += 1
                self._flow = None
                self._pending_session = False
            self._busy = busy

    def start(self) -> None:
        with self._lock:
            if self._server is not None:
                return
            self._path = "/" + secrets.token_urlsafe(32) + "/"
            owner = self

            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass  # Callback URLs and authorization codes are credentials.

                def setup(self):
                    super().setup()
                    self.connection.settimeout(5)

                def do_GET(self):
                    owner._handle(self, False)

                def do_POST(self):
                    owner._handle(self, True)

            self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            self._server.daemon_threads = True
            self._generation += 1
            self._thread = threading.Thread(target=self._server.serve_forever,
                                            kwargs={"poll_interval": 0.1}, daemon=True,
                                            name="oauth-loopback")
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            server, self._server = self._server, None
            thread, self._thread = self._thread, None
            self._generation += 1
            self._flow = None
            self._busy = False
            self._pending_session = False
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)

    def begin(self) -> str:
        if not self._operation.acquire(blocking=False):
            raise PanelLoginError("正在处理登录请求，请稍后重试。")
        try:
            with self._lock:
                if self._server is None or self._busy or self._pending_session:
                    raise PanelLoginError("当前正在验证登录或已经登录，请返回软件查看状态。")
                config, generation = replace(self._config), self._generation
                self._flow = None
            flow = OAuthFlow.begin(config)
            with self._lock:
                if self._server is None or generation != self._generation or self._busy:
                    raise PanelLoginError("登录状态或面板地址已改变，请重新生成授权链接。")
                self._flow = flow
                return flow.authorization_url
        finally:
            self._operation.release()

    def submit(self, callback_url: str) -> None:
        if not self._operation.acquire(blocking=False):
            raise PanelLoginError("正在处理登录请求，请稍后重试。")
        try:
            with self._lock:
                if self._server is None or self._busy or self._pending_session:
                    raise PanelLoginError("当前正在验证登录或已经登录，请返回软件查看状态。")
                flow, generation = self._flow, self._generation
                if flow is None:
                    raise PanelLoginError("请先在软件中生成 OAuth 授权链接。")
                params = flow.callback_parameters(callback_url)
                self._flow = None  # Never replay a code after an exchange attempt.
            config = flow.exchange(params)
            with self._lock:
                if self._server is None or generation != self._generation or self._busy:
                    raise PanelLoginError("登录状态或面板地址已改变，请返回软件重新登录。")
                self._pending_session = True
                self.on_session(config, generation)
        finally:
            self._operation.release()

    def _handle(self, request: BaseHTTPRequestHandler, post: bool) -> None:
        with self._lock:
            active = self._server is request.server
            path, base = self._path, self.url
        origin = base.rstrip("/").rsplit("/", 1)[0]
        if (not active or request.headers.get("Host") != urlsplit(base).netloc
                or request.headers.get("Origin") not in {None, origin}
                or request.headers.get("Sec-Fetch-Site") == "cross-site"):
            self._respond(request, 403, "回调来源不正确，请返回软件提交回调链接。")
            return
        if not post and request.path == path:
            self._respond(request, 200, "", forms=True)
            return
        if not post or request.path != path + "callback":
            self._respond(request, 404, "链接已失效，请返回软件查看当前授权状态。")
            return
        try:
            length = int(request.headers.get("Content-Length", "0"))
            if (not 0 <= length <= MAX_CALLBACK_LENGTH * 4
                    or request.headers.get("Transfer-Encoding")):
                raise PanelLoginError("回调请求格式不正确。")
            body = request.rfile.read(length)
            if request.headers.get_content_type() != "application/x-www-form-urlencoded":
                raise PanelLoginError("回调请求格式不正确。")
            fields = parse_qs(body.decode("utf-8"), max_num_fields=2)
            values = fields.get("callback_url", [])
            if len(values) != 1:
                raise PanelLoginError("请粘贴完整回调链接。")
            self.submit(values[0])
            self._respond(request, 200, "回调已交给软件，正在验证会话和管理员权限。请返回软件查看结果。")
        except (ValueError, UnicodeError):
            self._respond(request, 400, "回调请求格式不正确。")
        except Exception as exc:
            self._respond(request, 400, login_error_message(exc), forms=True)

    def _respond(self, request, status: int, message: str, *, forms: bool = False) -> None:
        with self._lock:
            path = self._path
            panel = escape(self._config.base_url)
        body = ("<!doctype html><html lang='zh-CN'><meta charset='utf-8'>"
                "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                "<title>Komari SSH Proxy · 浏览器授权</title>"
                "<style>body{font:16px/1.7 system-ui,sans-serif;max-width:760px;margin:48px auto;padding:0 24px;"
                "color:#22302c}input{box-sizing:border-box;width:100%;padding:12px;font:inherit}"
                "button{padding:10px 18px;font:inherit;cursor:pointer;margin:12px 0}"
                "p{overflow-wrap:anywhere}small{color:#586660}</style>"
                f"<h1>浏览器授权</h1><p>面板：{panel}</p><p>{escape(message)}</p>")
        if forms:
            body += ("<ol><li>在软件 OAuth 选项卡生成或打开授权链接，直接进入服务商授权。</li>"
                     "<li>授权后复制地址栏中的完整 /api/oauth_callback 链接。页面显示 Invalid state 属于预期："
                     "本次 state Cookie 保留在软件中。</li><li>回到此页粘贴链接，或粘贴到软件的 OAuth 选项卡。</li></ol>"
                     f"<form method='post' action='{path}callback'>"
                     "<label for='callback'>授权后的完整回调链接</label>"
                     "<input id='callback' name='callback_url' type='url' autocomplete='off' required>"
                     "<button type='submit'>交给软件完成登录</button></form>"
                     "<small>请使用软件发起的授权，五分钟内完成。外部账号需已绑定 Komari。</small>")
            body += ("<p><small>若授权后直接进入面板首页，浏览器可能已消费授权码；请使用 Cookie 登录。"
                     "QQ 聚合登录尤其可能遇到此情况。也可先退出面板登录并清除该面板 Cookie 后重新开始；"
                     "无需退出第三方服务商账号。</small></p>")
        raw = (body + "</html>").encode("utf-8")
        try:
            request.send_response(status)
            request.send_header("Content-Type", "text/html; charset=utf-8")
            request.send_header("Content-Length", str(len(raw)))
            request.send_header("Cache-Control", "no-store")
            request.send_header("Referrer-Policy", "no-referrer")
            request.send_header("X-Content-Type-Options", "nosniff")
            request.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'")
            request.end_headers()
            request.wfile.write(raw)
        except (OSError, TimeoutError):
            pass

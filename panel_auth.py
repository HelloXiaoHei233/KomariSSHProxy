"""Panel login helpers. Passwords and verification codes are never persisted."""
from __future__ import annotations

from dataclasses import replace
from urllib.parse import urlsplit

import requests

from config import AgentConfig, normalize_base_url
from komari_client import KomariClient, KomariError


class PanelLoginError(KomariError):
    """A credential-free login error suitable for display."""


LOGIN_ERRORS = {
    "Password login is disabled": "面板已禁用账号密码登录，请使用 OAuth 或 Cookie 登录。",
    "Invalid credentials": "账号或密码不正确。",
    "2FA code is required": "此账号已启用 2FA，请填写当前验证码后重新登录。",
    "Invalid 2FA code": "2FA 验证码不正确或已过期，请重新输入。",
}


def checked_config(config: AgentConfig) -> AgentConfig:
    base_url = normalize_base_url(config.base_url)
    try:
        parsed = urlsplit(base_url)
        valid = (parsed.scheme in {"http", "https"} and parsed.hostname
                 and not parsed.username and not parsed.password
                 and not parsed.query and not parsed.fragment)
        parsed.port  # Validate malformed ports before a network request.
    except ValueError:
        valid = False
    if not valid:
        raise PanelLoginError("请输入有效的 HTTP/HTTPS 面板地址，不要在地址中包含账号、密码或查询参数。")
    return replace(config, base_url=base_url)


def without_session(config: AgentConfig) -> AgentConfig:
    extras = replace(config, session_token="", cf_clearance="", sensorsdata2015jssdkcross="", language="")
    excluded = {"session_token", "oauth_state", "binding_external_account"}
    excluded.update(name for name in ("cf_clearance", "sensorsdata2015jssdkcross", "language")
                    if getattr(config, name))
    pairs = [(name, value) for name, value in extras.cookie_pairs() if name not in excluded]
    return replace(config, session_token="", extra_cookies="; ".join(f"{k}={v}" for k, v in pairs))


def request_origin(url: str) -> tuple[str, str, int]:
    """Use the same IDNA and IPv6 normalization as the HTTP client."""
    try:
        original = urlsplit(url)
        parsed = urlsplit(requests.Request("GET", url).prepare().url)
        if not parsed.hostname or parsed.scheme not in {"http", "https"}:
            raise ValueError("Invalid HTTP origin")
        port = original.port if original.port is not None else (443 if parsed.scheme == "https" else 80)
    except requests.RequestException as exc:
        raise ValueError("Invalid HTTP origin") from exc
    return parsed.scheme, parsed.hostname, port


def response_cookie(response: requests.Response, name: str, config: AgentConfig) -> str:
    host = request_origin(config.base_url)[1]
    if ":" in host:
        host = f"[{host}]"
    for cookie in response.cookies:
        domain = cookie.domain.lstrip(".").lower()
        # CookieJar appends .local to host-only, single-label hosts (including IPv6).
        matches = domain == host or (not cookie.domain_specified and "." not in host
                                     and domain == host + ".local")
        if cookie.name == name and matches and cookie.path == "/":
            return cookie.value
    return ""


def login_error_message(exc: Exception) -> str:
    if isinstance(exc, PanelLoginError):
        return str(exc)
    if isinstance(exc, requests.exceptions.SSLError):
        return "无法验证面板的 TLS 证书，请检查证书配置。"
    if isinstance(exc, requests.exceptions.Timeout):
        return "连接面板超时，请检查网络后重试。"
    if isinstance(exc, requests.exceptions.RequestException):
        return "无法连接面板，请检查地址、网络和站点访问限制。"
    # Server responses and request exceptions may contain credentials or OAuth codes.
    return "无法验证面板登录，请检查凭据、面板权限和网络后重试。"


def login_with_password(config: AgentConfig, username: str, password: str,
                        two_fa_code: str = "") -> AgentConfig:
    config = without_session(checked_config(config))
    if not username.strip() or not password:
        raise PanelLoginError("请填写账号和密码。")
    client = KomariClient(config)
    try:
        # Do not forward a password to a redirect target, including HTTP 307/308.
        response = client.session.post(
            f"{client.base_url}/api/login", headers=client._http_headers(),
            json={"username": username.strip(), "password": password,
                  "2fa_code": two_fa_code.strip()}, timeout=20, allow_redirects=False,
        )
        with response:
            try:
                payload = response.json()
            except ValueError:
                raise PanelLoginError("登录响应不是 JSON，请检查面板地址或站点访问限制。") from None
            if not isinstance(payload, dict):
                raise PanelLoginError("面板登录响应格式不正确。")
            if response.status_code != 200 or payload.get("status") != "success":
                message = payload.get("message")
                safe = LOGIN_ERRORS.get(message) if isinstance(message, str) else None
                raise PanelLoginError(safe or f"面板拒绝登录（HTTP {response.status_code}）。")
            data = payload.get("data")
            cookies = data.get("set-cookie") if isinstance(data, dict) else None
            token = cookies.get("session_token") if isinstance(cookies, dict) else None
            if not token:
                token = response_cookie(response, "session_token", config)
            if (not isinstance(token, str) or not token or len(token) > 4096
                    or any(ord(char) < 33 or ord(char) > 126 or char == ";" for char in token)):
                raise PanelLoginError("登录响应缺少有效的 session_token。")
            return replace(config, session_token=token)
    finally:
        client.session.close()

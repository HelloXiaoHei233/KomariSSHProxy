from __future__ import annotations

import json
import ssl
from typing import Any
from urllib.parse import quote, urlencode, urljoin, urlparse

import requests
import websocket

from config import AgentConfig, normalize_base_url


# Komari emits this exact TEXT frame while reattaching a browser to an agent.
# Agent/PTY output uses BINARY frames, even when it contains the same text.
TERMINAL_WAITING_MESSAGE = "等待被控端连接 waiting for agent...\n".encode("utf-8")


class KomariError(RuntimeError):
    pass


class KomariClient:
    def __init__(self, config: AgentConfig):
        self.config = config
        self.base_url = normalize_base_url(config.base_url)
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": config.user_agent})

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any | None = None,
        referer: str | None = None,
        timeout: float = 20,
    ) -> Any:
        url = urljoin(self.base_url + "/", path.lstrip("/"))
        headers = self._http_headers(referer=referer)
        response = self.session.request(
            method,
            url,
            headers=headers,
            json=json_body,
            timeout=timeout,
        )
        payload = self._read_json(response)
        if not response.ok:
            message = _message_from_payload(payload) or f"HTTP {response.status_code}"
            raise KomariError(message)
        return payload

    def raw_request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        data: bytes | None = None,
        json_body: Any | None = None,
        stream: bool = False,
        referer: str | None = None,
        timeout: float = 20,
    ) -> requests.Response:
        """发送不解析 JSON 的请求，供文件下载/上传等二进制通道使用。

        调用方负责检查状态码并关闭响应；Cookie 与 User-Agent 仍由本客户端统一注入。
        """
        url = urljoin(self.base_url + "/", path.lstrip("/"))
        merged = self._http_headers(referer=referer)
        if headers:
            merged.update(headers)
        return self.session.request(
            method,
            url,
            headers=merged,
            params=params,
            data=data,
            json=json_body,
            timeout=timeout,
            stream=stream,
        )

    def validate(self) -> list[dict[str, Any]]:
        self.get_xterm_settings()
        return self.get_clients()

    def get_xterm_settings(self) -> dict[str, Any]:
        payload = self.request(
            "GET",
            "/api/admin/settings/xtermjs",
            referer=f"{self.base_url}/terminal",
        )
        if isinstance(payload, dict) and payload.get("status") == "success":
            data = payload.get("data")
            return data if isinstance(data, dict) else {}
        raise KomariError(_message_from_payload(payload) or "无法读取 xtermjs 设置")

    def get_me(self) -> dict[str, Any]:
        payload = self.request("GET", "/api/me")
        return payload if isinstance(payload, dict) else {}

    def get_clients(self) -> list[dict[str, Any]]:
        payload = self.request("GET", "/api/admin/client/list", referer=f"{self.base_url}/admin")
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if isinstance(payload, dict) and isinstance(payload.get("data"), list):
            return [item for item in payload["data"] if isinstance(item, dict)]
        raise KomariError("服务器列表响应格式不正确")

    def rpc_call(self, method: str, params: Any | None = None) -> Any:
        body: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "id": 1}
        if params is not None:
            body["params"] = params
        payload = self.request("POST", "/api/rpc2", json_body=body)
        if isinstance(payload, dict):
            if "error" in payload:
                error = payload["error"]
                if isinstance(error, dict):
                    raise KomariError(str(error.get("message") or error))
                raise KomariError(str(error))
            return payload.get("result")
        raise KomariError("RPC2 响应格式不正确")

    def get_nodes(self) -> dict[str, Any]:
        result = self.rpc_call("common:getNodes")
        return result if isinstance(result, dict) else {}

    def get_latest_status(self) -> dict[str, Any]:
        result = self.rpc_call("common:getNodesLatestStatus")
        return result if isinstance(result, dict) else {}

    def connect_terminal(self, uuid: str, two_fa_code: str = "", timeout: float = 20,
                         request_id: str = ""):
        url = self.terminal_ws_url(uuid, two_fa_code, request_id)
        referer = f"{self.base_url}/terminal?uuid={quote(uuid)}"
        headers = [
            f"User-Agent: {self.config.user_agent}",
            "Cache-Control: no-cache",
            "Pragma: no-cache",
            f"Accept-Language: {self.config.language},zh;q=0.9,en;q=0.6",
            f"Referer: {referer}",
        ]
        cookie = self.config.cookie_header()
        ws = websocket.create_connection(
            url,
            timeout=timeout,
            header=headers,
            cookie=cookie or None,
            origin=self.config.origin,
            sslopt={"cert_reqs": ssl.CERT_REQUIRED},
            enable_multithread=True,
        )
        ws.settimeout(None)
        return ws

    def terminal_ws_url(self, uuid: str, two_fa_code: str = "", request_id: str = "") -> str:
        parsed = urlparse(self.base_url)
        ws_scheme = "wss" if parsed.scheme == "https" else "ws"
        path = f"/api/admin/client/{quote(uuid)}/terminal"
        # 带 request_id 时面板会把连接重挂到既有会话，且不再要求新的 2FA 验证码。
        query: dict[str, str] = {}
        if two_fa_code.strip():
            query["2fa_code"] = two_fa_code.strip()
        if request_id.strip():
            query["request_id"] = request_id.strip()
        suffix = "?" + urlencode(query) if query else ""
        return f"{ws_scheme}://{parsed.netloc}{path}{suffix}"

    def _http_headers(self, referer: str | None = None) -> dict[str, str]:
        headers = {
            "Accept": "*/*",
            "Accept-Language": f"{self.config.language},zh;q=0.9,en;q=0.6",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "DNT": "1",
            "Referer": referer or f"{self.base_url}/admin",
        }
        cookie = self.config.cookie_header()
        if cookie:
            headers["Cookie"] = cookie
        return headers

    @staticmethod
    def _read_json(response: requests.Response) -> Any:
        if not response.content:
            return None
        try:
            return response.json()
        except json.JSONDecodeError as exc:
            text = response.text[:300].replace("\n", " ")
            raise KomariError(f"响应不是 JSON: {text}") from exc


def normalize_latest_status(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    normalized: dict[str, dict[str, Any]] = {}
    for key, value in raw.items():
        if not isinstance(value, dict):
            continue
        uuid = str(value.get("client") or key)
        normalized[uuid] = value
    return normalized


def _message_from_payload(payload: Any) -> str:
    if isinstance(payload, dict):
        for key in ("message", "error", "msg"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
    return ""

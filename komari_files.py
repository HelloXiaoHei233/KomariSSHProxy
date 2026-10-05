"""Komari 面板文件接口客户端。

元数据（列目录、stat、mkdir、删除、改名、权限、属主）经 RPC2 的 ``admin:file*``
方法完成；文件内容经面板的二进制 HTTP 通道传输：下载使用带 ``Range`` 的 GET，
上传使用 ``init``/``chunk``/``merge`` 分片流程。两者都由面板在服务端编排 Agent
中转，因此本客户端不需要实现任何 relay 逻辑。

仅用于你拥有或已获授权管理的 Komari 面板和节点。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import io
from typing import Any, BinaryIO, Callable

import requests

from config import AgentConfig
from komari_client import KomariClient, KomariError


DEFAULT_UPLOAD_CHUNK = 25 * 1024 * 1024
MIN_UPLOAD_CHUNK = 1 * 1024 * 1024
MAX_UPLOAD_CHUNK = 128 * 1024 * 1024
MAX_UPLOAD_SIZE = 8 * 1024 * 1024 * 1024
READ_BLOCK = 256 * 1024

# 分类顺序敏感：「client not found」属于离线而不是文件缺失。
_ERROR_KINDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("offline", ("agent is not connected", "client not found", "agent offline")),
    ("unsupported", ("unsupported file operation", "does not support file operations",
                     "web control is disabled", "not implemented", "unimplemented")),
    ("too_large", ("payload too large", "request entity too large", "too large", "exceeds")),
    ("permission", ("permission denied", "operation not permitted", "access is denied",
                    "forbidden")),
    ("not_found", ("no such file", "not found", "cannot find", "does not exist")),
    ("timeout", ("timed out", "timeout", "deadline exceeded")),
)


def classify_error(message: str) -> str:
    """把远端错误文本归类，供上层映射到 SFTP 状态码。"""
    lowered = (message or "").lower()
    for kind, markers in _ERROR_KINDS:
        if any(marker in lowered for marker in markers):
            return kind
    return "other"


class KomariFileError(RuntimeError):
    """面向用户的文件操作错误；消息中不含 Cookie、密码或请求头。"""

    def __init__(self, message: str, kind: str = "other") -> None:
        super().__init__(message)
        self.kind = kind


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _parse_octal(value: Any) -> int:
    text = str(value or "").strip()
    if text.startswith("0o"):
        text = text[2:]
    try:
        return int(text, 8) & 0o7777
    except ValueError:
        return 0


def _parse_time(value: Any) -> float:
    if not value:
        return 0.0
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return 0.0
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def _read_exactly(source: BinaryIO, count: int) -> bytes:
    """读取恰好 count 字节；上游提前结束时返回较短结果，由调用方判断。"""
    chunks: list[bytes] = []
    total = 0
    while total < count:
        block = source.read(count - total)
        if not block:
            break
        chunks.append(block)
        total += len(block)
    return b"".join(chunks)


def _read_window(response: requests.Response, skip: int, count: int) -> bytes:
    """在一次流式遍历里跳过 skip 字节并读取 count 字节。

    ``requests`` 的迭代器与底层 socket 共用游标，跳过后必须继续用同一遍迭代，
    否则被多读走的字节会永久丢失。整个文件也不会因此被读进内存。
    """
    if count <= 0:
        return b""
    remaining_skip = max(0, skip)
    chunks: list[bytes] = []
    total = 0
    for block in response.iter_content(READ_BLOCK):
        if not block:
            continue
        if remaining_skip:
            if len(block) <= remaining_skip:
                remaining_skip -= len(block)
                continue
            block = block[remaining_skip:]
            remaining_skip = 0
        take = block[: count - total]
        chunks.append(take)
        total += len(take)
        if total >= count:
            break
    return b"".join(chunks)


@dataclass(frozen=True)
class RemoteEntry:
    """Agent 返回的文件条目（字段与面板 ``fileInfo`` 一致）。"""

    name: str
    path: str
    is_dir: bool
    is_symlink: bool
    size: int
    mode_octal: int
    uid: int
    gid: int
    mtime: float
    target: str = ""

    @classmethod
    def from_payload(cls, payload: Any) -> "RemoteEntry | None":
        if not isinstance(payload, dict):
            return None
        path = str(payload.get("path") or "")
        if not path:
            return None
        name = str(payload.get("name") or "") or path.rstrip("/").rsplit("/", 1)[-1]
        return cls(
            name=name,
            path=path,
            is_dir=bool(payload.get("is_dir")),
            is_symlink=bool(payload.get("is_symlink")),
            size=_as_int(payload.get("size")),
            mode_octal=_parse_octal(payload.get("mode_octal")),
            uid=_as_int(payload.get("uid")),
            gid=_as_int(payload.get("gid")),
            mtime=_parse_time(payload.get("modified_at")),
            target=str(payload.get("target") or ""),
        )


class KomariFileClient:
    """单个节点的文件操作客户端。

    每个实例持有一条 ``requests`` 会话，可复用于多次操作；
    但同一个实例不适合在多个线程里并发上传。
    """

    def __init__(self, config: AgentConfig, uuid: str, *, timeout: float = 60):
        self.config = config
        self.uuid = uuid
        self.timeout = timeout
        self.client = KomariClient(config)

    def close(self) -> None:
        self.client.session.close()

    def __enter__(self) -> "KomariFileClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ---- 元数据（RPC2） ----

    def roots(self) -> list[RemoteEntry]:
        return self._entries(self._rpc("fileListRoots", {"path": ""}))

    def list(self, path: str) -> list[RemoteEntry]:
        return self._entries(self._rpc("fileList", {"path": path}))

    def stat(self, path: str) -> RemoteEntry:
        entry = RemoteEntry.from_payload(self._rpc("fileStat", {"path": path}))
        if entry is None:
            raise KomariFileError("远端返回了无法解析的文件信息。", "other")
        return entry

    def mkdir(self, path: str, mode: str = "") -> None:
        self._rpc("fileMkdir", {"path": path, "mode": mode})

    def remove(self, path: str) -> None:
        self._rpc("fileDelete", {"path": path})

    def move(self, source: str, destination: str) -> None:
        self._rpc("fileMove", {"source": source, "destination": destination})

    def chmod(self, path: str, mode: str) -> None:
        self._rpc("fileChmod", {"path": path, "mode": mode})

    def chown(self, path: str, *, uid: int | None = None, gid: int | None = None) -> None:
        if uid is None and gid is None:
            raise KomariFileError("需要提供 uid 或 gid。", "other")
        params: dict[str, Any] = {"path": path}
        if uid is not None:
            params["uid"] = int(uid)
        if gid is not None:
            params["gid"] = int(gid)
        self._rpc("fileChown", params)

    def _rpc(self, method: str, params: dict[str, Any] | None = None) -> Any:
        try:
            return self.client.rpc_call(f"admin:{method}", {"uuid": self.uuid, **(params or {})})
        except KomariError as exc:
            raise self._error(exc) from None
        except Exception as exc:  # requests 等底层异常统一收敛为中文错误。
            raise self._error(exc) from None

    @staticmethod
    def _entries(payload: Any) -> list[RemoteEntry]:
        items = payload.get("data") if isinstance(payload, dict) and "data" in payload else payload
        if not isinstance(items, list):
            raise KomariFileError("远端返回的目录列表格式不正确。", "other")
        entries = [RemoteEntry.from_payload(item) for item in items]
        return [entry for entry in entries if entry is not None]

    # ---- 内容：下载 ----

    def read_range(self, path: str, offset: int, length: int) -> bytes:
        """读取 ``[offset, offset+length)``；服务端忽略 Range 时在本地丢弃前缀。"""
        if length <= 0:
            return b""
        offset = max(0, int(offset))
        response = self._download(path, offset=offset, length=length)
        try:
            status = response.status_code
            if status == 416:
                return b""
            if status not in (200, 206):
                message = self._response_message(response)
                raise KomariFileError(message, classify_error(message))
            # 206 的正文已是请求区间；200 表示服务端忽略了 Range，需要在本地跳过前缀。
            return _read_window(response, offset if status == 200 else 0, length)
        finally:
            response.close()

    def _download(self, path: str, *, offset: int, length: int) -> requests.Response:
        headers = {"Range": f"bytes={offset}-{offset + length - 1}"} if length > 0 else {}
        try:
            return self.client.raw_request(
                "GET",
                f"/api/admin/client/{self.uuid}/file/download",
                params={"path": path},
                headers=headers,
                referer=f"{self.client.base_url}/admin",
                stream=True,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise self._error(exc) from None

    # ---- 内容：上传 ----

    def write(self, path: str, source: BinaryIO, size: int, *,
              chunk_size: int = DEFAULT_UPLOAD_CHUNK,
              progress: Callable[[int], None] | None = None) -> None:
        """从 ``source`` 上传 ``size`` 字节；分片长度严格匹配，失败时尝试取消。"""
        size = int(size)
        if size < 0 or size > MAX_UPLOAD_SIZE:
            raise KomariFileError(f"文件大小超出面板上限（{MAX_UPLOAD_SIZE} 字节）。", "too_large")
        chunk = max(MIN_UPLOAD_CHUNK, min(MAX_UPLOAD_CHUNK, int(chunk_size or DEFAULT_UPLOAD_CHUNK)))
        session = self._upload_payload(self._upload_request(
            "init", json_body={"path": path, "size": size, "chunk_size": chunk}))
        if session.get("complete"):
            return
        upload_id = str(session.get("upload_id") or "")
        if not upload_id:
            raise KomariFileError("面板未返回 upload_id，无法上传。", "other")
        effective = int(session.get("chunk_size") or chunk)
        if not MIN_UPLOAD_CHUNK <= effective <= MAX_UPLOAD_CHUNK:
            effective = chunk
        try:
            sent = 0
            index = 0
            while sent < size:
                expected = min(effective, size - sent)
                block = _read_exactly(source, expected)
                if len(block) != expected:
                    raise KomariFileError("本地数据提前结束，上传已取消。", "other")
                self._upload_payload(self._upload_request(
                    "chunk", params={"upload_id": upload_id, "chunk_index": str(index)}, data=block))
                sent += len(block)
                index += 1
                if progress is not None:
                    progress(sent)
            self._upload_payload(self._upload_request("merge", json_body={"upload_id": upload_id}))
        except BaseException:
            self._cancel_upload(upload_id)
            raise

    def write_bytes(self, path: str, data: bytes) -> None:
        self.write(path, io.BytesIO(data), len(data))

    def _cancel_upload(self, upload_id: str) -> None:
        try:
            self._upload_request("cancel", params={"upload_id": upload_id})
        except Exception:
            pass  # 取消失败不应覆盖调用方原本看到的错误。

    def _upload_request(self, operation: str, *, params: dict[str, str] | None = None,
                        json_body: Any | None = None, data: bytes | None = None) -> requests.Response:
        query = {"operation": operation, **(params or {})}
        headers = {"Content-Type": "application/json"} if data is None else {}
        try:
            return self.client.raw_request(
                "POST",
                f"/api/admin/client/{self.uuid}/file/upload",
                params=query,
                json_body=json_body,
                data=data,
                headers=headers,
                referer=f"{self.client.base_url}/admin",
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise self._error(exc) from None

    def _upload_payload(self, response: requests.Response) -> dict[str, Any]:
        payload = self._json_or_none(response)
        if not response.ok:
            message = self._payload_message(payload) or f"面板返回 HTTP {response.status_code}。"
            raise KomariFileError(message, classify_error(message))
        if isinstance(payload, dict):
            data = payload.get("data")
            return data if isinstance(data, dict) else payload
        return {}

    # ---- 错误与解析 ----

    @staticmethod
    def _json_or_none(response: requests.Response) -> Any:
        try:
            return response.json()
        except ValueError:
            return None

    @staticmethod
    def _payload_message(payload: Any) -> str:
        if isinstance(payload, dict):
            for key in ("message", "error", "msg"):
                value = payload.get(key)
                if isinstance(value, str) and value:
                    return value
        return ""

    @classmethod
    def _response_message(cls, response: requests.Response) -> str:
        message = cls._payload_message(cls._json_or_none(response))
        return message or f"面板返回 HTTP {response.status_code}。"

    @staticmethod
    def _error(exc: Exception) -> KomariFileError:
        if isinstance(exc, KomariError):
            message = str(exc).strip() or "远端返回未知错误。"
        elif isinstance(exc, requests.RequestException):
            # requests 的异常文本可能包含带路径的 URL，这里只给可操作的提示。
            message = "无法连接面板文件接口；请检查网络与登录状态。"
        else:
            message = f"文件操作失败（{type(exc).__name__}）。"
        return KomariFileError(message, classify_error(message))

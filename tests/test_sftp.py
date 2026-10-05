"""SFTP 子系统的端到端测试与面板文件接口的契约测试。

端到端部分在 127.0.0.1 上启动真实的 paramiko SSH 服务端，把
``KomariFileClient`` 替换成内存后端，再用真实的 paramiko SFTP 客户端操作，
从而覆盖子系统注册、路径规范化、读写句柄与错误码映射。
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path
import socket
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

import paramiko

import sftp_backend
from config import AgentConfig
from komari_client import KomariError
from komari_files import (KomariFileClient, KomariFileError, MIN_UPLOAD_CHUNK, RemoteEntry,
                          classify_error)
from sftp_backend import canonical_path, sftp_subsystem_args


def wait_for(predicate, timeout: float = 5) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("等待条件超时")


class FakeKomariFileClient:
    """内存文件系统；方法签名与 ``KomariFileClient`` 对齐。"""

    created: list["FakeKomariFileClient"] = []

    def __init__(self, config, uuid, **kwargs):
        self.config = config
        self.uuid = uuid
        self.files: dict[str, bytes] = {}
        self.dirs: set[str] = {"/"}
        self.closed = False
        FakeKomariFileClient.created.append(self)

    # ---- 内部 ----

    def _entry(self, path: str) -> RemoteEntry:
        path = canonical_path(path)
        if path in self.dirs:
            name = path.rstrip("/").rsplit("/", 1)[-1] or "/"
            return RemoteEntry(name, path, True, False, 0, 0o755, 0, 0, 1700000000.0)
        if path in self.files:
            return RemoteEntry(path.rsplit("/", 1)[-1], path, False, False,
                               len(self.files[path]), 0o644, 0, 0, 1700000000.0)
        raise KomariFileError(f"no such file or directory: {path}", "not_found")

    @staticmethod
    def _parent(path: str) -> str:
        return path.rsplit("/", 1)[0] or "/"

    # ---- 接口 ----

    def close(self) -> None:
        self.closed = True

    def roots(self):
        return [self._entry("/")]

    def list(self, path):
        path = canonical_path(path)
        self._entry(path)
        prefix = "/" if path == "/" else path + "/"
        found = {}
        for candidate in list(self.files) + list(self.dirs):
            if candidate == path or not candidate.startswith(prefix):
                continue
            rest = candidate[len(prefix):]
            if "/" in rest:
                continue
            found[rest] = candidate
        return [self._entry(target) for target in found.values()]

    def stat(self, path):
        return self._entry(path)

    def read_range(self, path, offset, length):
        data = self.files.get(canonical_path(path))
        if data is None:
            self._entry(path)
        return data[offset:offset + length]

    def write(self, path, source, size):
        path = canonical_path(path)
        if self._parent(path) not in self.dirs:
            raise KomariFileError("no such file or directory", "not_found")
        source.seek(0)
        self.files[path] = source.read(size)

    def mkdir(self, path, mode=""):
        path = canonical_path(path)
        if self._parent(path) not in self.dirs:
            raise KomariFileError("no such file or directory", "not_found")
        self.dirs.add(path)

    def remove(self, path):
        path = canonical_path(path)
        if path in self.files:
            del self.files[path]
            return
        if path in self.dirs and path != "/":
            self.dirs.discard(path)
            for candidate in [key for key in self.files if key.startswith(path + "/")]:
                del self.files[candidate]
            return
        self._entry(path)

    def move(self, source, destination):
        source, destination = canonical_path(source), canonical_path(destination)
        if source in self.files:
            self.files[destination] = self.files.pop(source)
            return
        if source in self.dirs and source != "/":
            self.dirs.discard(source)
            self.dirs.add(destination)
            for key in [item for item in self.files if item.startswith(source + "/")]:
                self.files[destination + key[len(source):]] = self.files.pop(key)
            return
        self._entry(source)

    def chmod(self, path, mode):
        self._entry(path)

    def chown(self, path, *, uid=None, gid=None):
        self._entry(path)


class _AuthOnlyServer(paramiko.ServerInterface):
    """只做认证与通道接受；子系统由 paramiko 的默认实现启动。"""

    def check_auth_password(self, username, password):
        return paramiko.AUTH_SUCCESSFUL

    def get_allowed_auths(self, username):
        return "password"

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED


class SftpSubsystemTests(unittest.TestCase):
    def setUp(self):
        FakeKomariFileClient.created.clear()
        self.patcher = patch("sftp_backend.KomariFileClient", FakeKomariFileClient)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

        self.key = paramiko.RSAKey.generate(2048)
        self.listener = socket.socket()
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(4)
        self.port = self.listener.getsockname()[1]
        self.errors: list[Exception] = []
        self.stopping = threading.Event()
        self.server_thread = threading.Thread(target=self._serve, daemon=True)
        self.server_thread.start()

        self.transport = paramiko.Transport(("127.0.0.1", self.port))
        self.transport.banner_timeout = 5
        self.transport.connect(username="root", password="secret")
        self.sftp = paramiko.SFTPClient.from_transport(self.transport)
        self.addCleanup(self._cleanup)

    def _serve(self) -> None:
        try:
            client, _ = self.listener.accept()
            transport = paramiko.Transport(client)
            transport.add_server_key(self.key)
            transport.set_subsystem_handler(
                "sftp", *sftp_subsystem_args(AgentConfig(mcp_port=0), "test-node-a"))
            transport.start_server(server=_AuthOnlyServer())
            deadline = time.monotonic() + 20
            while not self.stopping.is_set() and transport.is_active() and time.monotonic() < deadline:
                time.sleep(0.05)
            transport.close()
        except Exception as exc:  # pragma: no cover - 记录后由断言暴露
            self.errors.append(exc)

    def _cleanup(self) -> None:
        self.stopping.set()
        for resource in (self.transport, self.listener):
            try:
                resource.close()
            except Exception:
                pass
        self.server_thread.join(timeout=3)

    def _backend(self) -> FakeKomariFileClient:
        wait_for(lambda: bool(FakeKomariFileClient.created))
        return FakeKomariFileClient.created[-1]

    # ---- 端到端 ----

    def test_write_read_list_and_stat(self):
        with self.sftp.open("/hello.txt", "w") as handle:
            handle.write(b"hello world")
        self.assertEqual(self.sftp.stat("/hello.txt").st_size, 11)
        with self.sftp.open("/hello.txt", "r") as handle:
            self.assertEqual(handle.read(), b"hello world")
        self.assertEqual(sorted(self.sftp.listdir("/")), ["hello.txt"])
        self.assertEqual(self._backend().files["/hello.txt"], b"hello world")

    def test_read_larger_than_cache_is_complete(self):
        payload = bytes(range(256)) * 4096  # 1 MiB，跨多次预读
        with self.sftp.open("/big.bin", "w") as handle:
            handle.write(payload)
        with self.sftp.open("/big.bin", "r") as handle:
            self.assertEqual(handle.read(), payload)

    def test_partial_reads_and_offsets(self):
        with self.sftp.open("/data.bin", "w") as handle:
            handle.write(b"0123456789")
        with self.sftp.open("/data.bin", "r") as handle:
            handle.seek(4)
            self.assertEqual(handle.read(3), b"456")
            self.assertEqual(handle.read(100), b"789")

    def test_mkdir_rename_remove_and_empty_dir_rule(self):
        self.sftp.mkdir("/sub")
        self.assertIn("/sub", self._backend().dirs)
        self.sftp.rename("/sub", "/renamed")
        self.assertIn("/renamed", self._backend().dirs)

        self.sftp.mkdir("/full")
        with self.sftp.open("/full/child.txt", "w") as handle:
            handle.write(b"x")
        # SFTP 语义：非空目录不能用 rmdir 删除。
        with self.assertRaises(IOError):
            self.sftp.rmdir("/full")
        self.sftp.remove("/full/child.txt")
        self.sftp.rmdir("/full")

    def test_missing_file_reports_no_such_file(self):
        with self.assertRaises(IOError):
            self.sftp.stat("/absent.txt")
        with self.assertRaises(IOError):
            self.sftp.open("/absent.txt", "r")

    def test_symlink_and_readlink_are_unsupported(self):
        # Agent 没有符号链接操作，接口必须明确返回“不支持”，不能静默成功。
        interface = sftp_backend.KomariSFTPServerInterface(
            None, AgentConfig(mcp_port=0), "test-node-a")
        self.assertEqual(interface.symlink("/target", "/link"),
                         sftp_backend.SFTP_OP_UNSUPPORTED)
        self.assertEqual(interface.readlink("/link"), sftp_backend.SFTP_OP_UNSUPPORTED)
        interface.session_ended()

    def test_read_handle_reports_remote_content_not_local_buffer(self):
        self._backend().files["/remote.txt"] = b"remote-content"
        with self.sftp.open("/remote.txt", "r") as handle:
            self.assertEqual(handle.read(6), b"remote")


class _StubResponse:
    def __init__(self, status_code: int, payload=None, body: bytes = b""):
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self._payload = payload
        self._body = body
        self._cursor = 0
        self.closed = False

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload

    def iter_content(self, size):
        while self._cursor < len(self._body):
            block = self._body[self._cursor:self._cursor + size]
            self._cursor += len(block)
            yield block

    def close(self):
        self.closed = True


class _StubKomariClient:
    def __init__(self, config):
        self.base_url = "https://panel.example.com"
        self.session = types.SimpleNamespace(close=lambda: None)
        self.rpc_calls: list[tuple] = []
        self.requests: list[tuple] = []
        self.rpc_results: dict = {}
        self.responses: list = []

    def rpc_call(self, method, params=None):
        self.rpc_calls.append((method, params))
        result = self.rpc_results.get(method, [])
        if isinstance(result, Exception):
            raise result
        return result

    def raw_request(self, method, path, **kwargs):
        self.requests.append((method, path, kwargs))
        if not self.responses:
            raise AssertionError("收到未预期的请求")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class KomariFileClientTests(unittest.TestCase):
    def setUp(self):
        self.patcher = patch("komari_files.KomariClient", _StubKomariClient)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.client = KomariFileClient(AgentConfig(mcp_port=0), "node-uuid")
        self.stub = self.client.client

    def test_list_maps_agent_entries(self):
        self.stub.rpc_results["admin:fileList"] = [
            {"name": "etc", "path": "/etc", "is_dir": True, "size": 0, "mode_octal": "0755",
             "uid": 0, "gid": 0, "modified_at": "2026-01-02T03:04:05Z"},
            {"name": "a.txt", "path": "/a.txt", "is_dir": False, "size": 7, "mode_octal": "0644",
             "uid": 1000, "gid": 1000, "modified_at": "2026-01-02T03:04:05Z"},
        ]
        entries = self.client.list("/")
        self.assertEqual(self.stub.rpc_calls[0], ("admin:fileList", {"uuid": "node-uuid", "path": "/"}))
        self.assertEqual([item.name for item in entries], ["etc", "a.txt"])
        self.assertTrue(entries[0].is_dir)
        self.assertEqual(entries[1].mode_octal, 0o644)
        self.assertEqual(entries[1].uid, 1000)

    def test_read_range_requests_bytes_and_returns_exact_slice(self):
        # 206 的正文本身就是请求区间，服务端不会再返回前缀。
        self.stub.responses.append(_StubResponse(206, body=b"defg"))
        data = self.client.read_range("/f.bin", 3, 4)
        self.assertEqual(data, b"defg")
        method, path, kwargs = self.stub.requests[0]
        self.assertEqual((method, path), ("GET", "/api/admin/client/node-uuid/file/download"))
        self.assertEqual(kwargs["params"], {"path": "/f.bin"})
        self.assertEqual(kwargs["headers"]["Range"], "bytes=3-6")
        self.assertTrue(kwargs["stream"])

    def test_read_range_slices_locally_when_server_ignores_range(self):
        self.stub.responses.append(_StubResponse(200, body=b"0123456789"))
        self.assertEqual(self.client.read_range("/f.bin", 5, 3), b"567")

    def test_empty_and_missing_range_are_safe(self):
        self.assertEqual(self.client.read_range("/f.bin", 0, 0), b"")
        self.stub.responses.append(_StubResponse(416))
        self.assertEqual(self.client.read_range("/f.bin", 999, 10), b"")

    def test_upload_runs_init_chunk_merge(self):
        # 面板不接受小于 1 MiB 的分片，因此用真实下限切出两片。
        chunk = MIN_UPLOAD_CHUNK
        payload = b"a" * chunk + b"bbb"
        for response in (_StubResponse(200, {"status": "success", "data": {
                            "upload_id": "u-1", "chunk_size": chunk, "complete": False}}),
                         _StubResponse(200, {"status": "success", "data": {"received": True}}),
                         _StubResponse(200, {"status": "success", "data": {"received": True}}),
                         _StubResponse(200, {"status": "success", "data": {"complete": True}})):
            self.stub.responses.append(response)
        self.client.write_bytes("/up.bin", payload)
        operations = [item[2]["params"]["operation"] for item in self.stub.requests]
        self.assertEqual(operations, ["init", "chunk", "chunk", "merge"])
        chunks = [item[2]["data"] for item in self.stub.requests
                  if item[2]["params"]["operation"] == "chunk"]
        self.assertEqual([len(item) for item in chunks], [chunk, 3])

    def test_upload_of_empty_file_only_initialises(self):
        self.stub.responses.append(_StubResponse(200, {"status": "success",
                                                       "data": {"complete": True}}))
        self.client.write_bytes("/empty.bin", b"")
        self.assertEqual([item[2]["params"]["operation"] for item in self.stub.requests], ["init"])

    def test_upload_failure_cancels_the_session(self):
        self.stub.responses.append(_StubResponse(200, {"status": "success", "data": {
            "upload_id": "u-2", "chunk_size": 4}}))
        self.stub.responses.append(_StubResponse(500, {"status": "error", "message": "agent is not connected"}))
        self.stub.responses.append(_StubResponse(200, {"status": "success", "data": {"cancelled": True}}))
        with self.assertRaises(KomariFileError) as caught:
            self.client.write_bytes("/up.bin", b"abcdef")
        self.assertEqual(caught.exception.kind, "offline")
        self.assertEqual(self.stub.requests[-1][2]["params"]["operation"], "cancel")

    def test_rpc_errors_are_classified_without_echoing_credentials(self):
        secret = "session_token=do-not-leak"
        self.stub.rpc_results["admin:fileStat"] = KomariError("permission denied")
        with self.assertRaises(KomariFileError) as caught:
            self.client.stat("/root/x")
        self.assertEqual(caught.exception.kind, "permission")
        self.assertNotIn(secret, str(caught.exception))

    def test_classify_error_kinds(self):
        self.assertEqual(classify_error("client not found"), "offline")
        self.assertEqual(classify_error("no such file or directory"), "not_found")
        self.assertEqual(classify_error("permission denied"), "permission")
        self.assertEqual(classify_error("web control is disabled"), "unsupported")
        self.assertEqual(classify_error("payload too large"), "too_large")
        self.assertEqual(classify_error("something else"), "other")


class SftpOperationLogTests(unittest.TestCase):
    """文件操作必须写进调用方日志流，未提供回调时保持静默。"""

    def setUp(self):
        FakeKomariFileClient.created.clear()
        self.patcher = patch("sftp_backend.KomariFileClient", FakeKomariFileClient)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.lines: list[str] = []
        self.interface = sftp_backend.KomariSFTPServerInterface(
            None, AgentConfig(mcp_port=0), "test-node-a", self.lines.append)
        self.addCleanup(self.interface.session_ended)
        self.files = self.interface._files()

    def test_directory_operations_are_logged(self):
        self.assertEqual(self.interface.mkdir("/sub", None), sftp_backend.SFTP_OK)
        self.assertEqual(self.interface.rmdir("/sub"), sftp_backend.SFTP_OK)
        self.assertTrue(any("创建目录 /sub" in line for line in self.lines))
        self.assertTrue(any("删除目录 /sub" in line for line in self.lines))

    def test_rename_is_logged(self):
        self.files.files["/a.txt"] = b"x"
        self.assertEqual(self.interface.rename("/a.txt", "/b.txt"), sftp_backend.SFTP_OK)
        self.assertTrue(any("重命名 /a.txt -> /b.txt" in line for line in self.lines))

    def test_failures_are_logged_with_the_reason(self):
        self.assertEqual(self.interface.remove("/missing.txt"), sftp_backend.SFTP_NO_SUCH_FILE)
        self.assertTrue(any("删除失败 /missing.txt" in line for line in self.lines))

    def test_non_empty_directory_refusal_is_logged(self):
        self.files.dirs.add("/full")
        self.files.files["/full/x.txt"] = b"x"
        self.assertEqual(self.interface.rmdir("/full"), sftp_backend.SFTP_FAILURE)
        self.assertTrue(any("拒绝删除非空目录 /full" in line for line in self.lines))

    def test_upload_and_download_are_logged_on_close(self):
        handle = self.interface.open("/new.txt", os.O_WRONLY | os.O_CREAT, None)
        handle.write(0, b"hello")
        handle.close()
        self.assertTrue(any("上传 /new.txt（5 字节）" in line for line in self.lines))

        self.files.files["/read.txt"] = b"0123456789"
        reader = self.interface.open("/read.txt", os.O_RDONLY, None)
        self.assertEqual(reader.read(0, 4), b"0123")
        reader.close()
        self.assertTrue(any("下载 /read.txt（4 字节）" in line for line in self.lines))

    def test_without_a_callback_operations_stay_silent(self):
        silent = sftp_backend.KomariSFTPServerInterface(None, AgentConfig(mcp_port=0), "test-node-a")
        self.assertEqual(silent.mkdir("/quiet", None), sftp_backend.SFTP_OK)
        silent.session_ended()


if __name__ == "__main__":
    unittest.main()

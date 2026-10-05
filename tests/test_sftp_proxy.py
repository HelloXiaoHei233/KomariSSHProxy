"""通过真实的 LocalSSHProxyServer 验证 SFTP 子系统与连接保活。

``tests/test_sftp.py`` 用的是最小化的服务端骨架；这里走完整的本地 SSH 代理，
覆盖 ``_handle_client`` 的通道接受、子系统注册与 ``_hold_transport`` 保活逻辑。
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import io
import socket
import unittest
from unittest.mock import patch

import paramiko

from config import AgentConfig
from ssh_bridge import LocalSSHProxyServer
from test_sftp import FakeKomariFileClient, wait_for


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class LocalProxySftpTests(unittest.TestCase):
    def setUp(self):
        FakeKomariFileClient.created.clear()
        self.patcher = patch("sftp_backend.KomariFileClient", FakeKomariFileClient)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

        key = paramiko.RSAKey.generate(2048)
        pem = io.StringIO()
        key.write_private_key(pem)
        self.config = AgentConfig(mcp_port=0, assist_port=0)
        self.config.ssh_host_key_pem = pem.getvalue()
        self.config.ssh_port = free_port()
        self.config.ssh_username = "root"
        self.config.ssh_password = "proxy-secret"
        self.logs: list[str] = []
        self.server = LocalSSHProxyServer(
            self.config, "test-node-a", "Example node",
            self.logs.append, lambda text, running: None)
        self.server.start()
        wait_for(lambda: self.server.is_running and self.server.listen_socket is not None)
        self.addCleanup(self.server.stop)

    def _connect(self, password: str | None = None) -> paramiko.SSHClient:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            "127.0.0.1", port=self.config.ssh_port, username=self.config.ssh_username,
            password=self.config.ssh_password if password is None else password,
            allow_agent=False, look_for_keys=False, timeout=5, banner_timeout=5, auth_timeout=5)
        self.addCleanup(client.close)
        return client

    def test_sftp_works_without_a_shell_channel(self):
        # 纯 SFTP 客户端不会打开 shell 通道，连接必须为子系统保活而不是被拆掉。
        client = self._connect()
        sftp = paramiko.SFTPClient.from_transport(client.get_transport())
        self.addCleanup(sftp.close)
        with sftp.open("/hello.txt", "w") as handle:
            handle.write(b"hello proxy")
        self.assertEqual(sorted(sftp.listdir("/")), ["hello.txt"])
        with sftp.open("/hello.txt", "r") as handle:
            self.assertEqual(handle.read(), b"hello proxy")
        self.assertEqual(FakeKomariFileClient.created[-1].files["/hello.txt"], b"hello proxy")

    def test_sftp_reuses_the_proxy_credentials(self):
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.addCleanup(client.close)
        with self.assertRaises(paramiko.AuthenticationException):
            client.connect(
                "127.0.0.1", port=self.config.ssh_port, username=self.config.ssh_username,
                password="wrong-password", allow_agent=False, look_for_keys=False, timeout=5)

    def test_subsystem_request_is_logged(self):
        client = self._connect()
        sftp = paramiko.SFTPClient.from_transport(client.get_transport())
        self.addCleanup(sftp.close)
        sftp.listdir("/")
        wait_for(lambda: any("SFTP 子系统请求" in line for line in self.logs))
        self.assertTrue(any("SFTP 子系统请求" in line for line in self.logs))

    def test_sftp_operations_reach_the_proxy_log(self):
        client = self._connect()
        sftp = paramiko.SFTPClient.from_transport(client.get_transport())
        self.addCleanup(sftp.close)
        sftp.mkdir("/logs")
        sftp.rename("/logs", "/renamed")
        wait_for(lambda: any("重命名 /logs -> /renamed" in line for line in self.logs))
        self.assertTrue(any("创建目录 /logs" in line for line in self.logs))

    def test_unknown_subsystem_is_still_rejected(self):
        client = self._connect()
        channel = client.get_transport().open_session()
        self.addCleanup(channel.close)
        with self.assertRaises(paramiko.SSHException):
            channel.invoke_subsystem("not-a-real-subsystem")


if __name__ == "__main__":
    unittest.main()

"""MCP access to the same native panel file APIs used by SSH SFTP."""
from __future__ import annotations

import base64
from dataclasses import asdict, replace
import os
from pathlib import Path
import posixpath
import threading

from komari_files import KomariFileClient, KomariFileError, MAX_UPLOAD_SIZE, READ_BLOCK
from terminal_sessions import TerminalError


def remote_path(value: str) -> str:
    if not value.startswith("/") or "\x00" in value or "\\" in value:
        raise TerminalError("远端路径必须为绝对虚拟路径；Windows 使用 /C/...，不能使用反斜杠。")
    return posixpath.normpath("/" + value.lstrip("/"))


def entry_info(entry) -> dict:
    info = asdict(entry)
    info["mode"] = format(info.pop("mode_octal"), "04o")
    return info


class FileOperations:
    def __init__(self, manager, client_factory=KomariFileClient):
        self.manager = manager
        self.client_factory = client_factory
        # Bounded stripes serialize this service's operations on each node.
        # The panel has no atomic no-clobber option against external writers.
        self._locks = [threading.Lock() for _ in range(32)]

    @staticmethod
    def _missing(client, path):
        try:
            client.stat(path)
        except KomariFileError as exc:
            if exc.kind == "not_found":
                return
            raise
        raise TerminalError("存在同名文件或目录；操作未执行。")

    def run(self, *, server_uuid: str, operation: str, actor: str, **args) -> dict:
        with self.manager._lock:
            if server_uuid not in self.manager._servers:
                raise TerminalError("找不到服务器 UUID；请先调用 list_terminals。")
            config = replace(self.manager.config)
        for field in ("path", "destination"):
            if field in args:
                args[field] = remote_path(args[field])
        with self._locks[hash(server_uuid) % len(self._locks)]:
            try:
                with self.client_factory(config, server_uuid) as client:
                    result = self._run(client, operation, args)
            except KomariFileError as exc:
                self.manager.log.add("file_failed", actor=actor, server_uuid=server_uuid,
                                     operation=operation, reason=exc.kind)
                # Remote error bodies can contain arbitrary or sensitive text.
                messages = {"not_found": "文件不存在。", "permission": "文件操作权限不足。",
                            "offline": "节点离线。", "unsupported": "节点不支持此文件操作。",
                            "exists": "存在同名文件或目录；操作未执行。",
                            "timeout": "文件操作超时；请检查远端状态再重试。",
                            "too_large": "文件超出面板大小上限。"}
                raise TerminalError(messages.get(exc.kind, "面板文件操作失败；请检查路径、权限和节点状态。")) from None
            except OSError as exc:
                raise TerminalError(f"本地文件操作失败（{type(exc).__name__}）；请检查本机路径、权限或同名文件。") from None
        self.manager.log.add("file_operation", actor=actor, server_uuid=server_uuid, operation=operation,
                             bytes=result.get("bytes_transferred", result.get("bytes_returned", 0)))
        return dict(server_uuid=server_uuid, operation=operation, **result)

    def _run(self, client, operation, args):
        path = args.get("path")
        if operation == "roots":
            return {"entries": [entry_info(entry) for entry in client.roots()]}
        if operation == "list":
            entries = client.list(path)
            offset, limit = args.get("offset", 0), args.get("limit", 100)
            entries.sort(key=lambda item: item.path)
            return dict(entries=[entry_info(entry) for entry in entries[offset:offset + limit]],
                        total=len(entries), next_offset=min(len(entries), offset + limit))
        if operation == "stat":
            return {"entry": entry_info(client.stat(path))}
        if operation == "read":
            offset = args.get("offset", 0)
            data = client.read_range(path, offset, args.get("length", 65536))
            result = dict(text=data.decode("utf-8", errors="replace"), bytes_returned=len(data),
                          next_offset=offset + len(data), encoding="utf-8")
            if args.get("include_base64", False):
                result["data_base64"] = base64.b64encode(data).decode("ascii")
            return result
        if operation == "upload":
            self._missing(client, path)
            local = Path(args["local_path"])
            if not local.is_absolute():
                raise TerminalError("local_path 必须是 MCP 服务所在电脑上的绝对路径。")
            with local.open("rb") as source:
                if not local.is_file():
                    raise TerminalError("上传源必须为普通文件。")
                size = os.fstat(source.fileno()).st_size
                if size > MAX_UPLOAD_SIZE:
                    raise TerminalError("文件超出面板大小上限（8 GiB）。")
                client.write(path, source, size)
            return dict(path=path, bytes_transferred=size)
        if operation == "download":
            entry = client.stat(path)
            if entry.is_dir:
                raise TerminalError("下载目标必须是文件。")
            local = Path(args["local_path"])
            if not local.is_absolute():
                raise TerminalError("local_path 必须是 MCP 服务所在电脑上的绝对路径。")
            # Exclusive local creation also protects against a concurrent writer.
            with local.open("xb") as target:
                offset = 0
                while offset < entry.size:
                    data = client.read_range(path, offset, min(READ_BLOCK, entry.size - offset))
                    if not data:
                        raise TerminalError("远端文件在下载时发生变化；本机保留了部分文件，请检查后处理。")
                    target.write(data)
                    offset += len(data)
            return dict(path=path, local_path=str(local), bytes_transferred=offset)
        if operation == "mkdir":
            self._missing(client, path)
            client.mkdir(path, args.get("mode", ""))
        elif operation == "rename":
            self._missing(client, args["destination"])
            client.move(path, args["destination"])
        elif operation == "chmod":
            client.chmod(path, args["mode"])
        elif operation == "chown":
            client.chown(path, uid=args.get("uid"), gid=args.get("gid"))
        elif operation == "delete":
            if path == "/" or (len(path) == 2 and path[1].isalpha()):
                raise TerminalError("不允许删除文件系统根目录。")
            entry = client.stat(path)
            if entry.is_dir and not entry.is_symlink and not args.get("recursive", False) and client.list(path):
                raise TerminalError("目录非空；如需删除目录树，必须明确传 recursive=true。")
            client.remove(path)
        else:
            raise TerminalError("未知文件操作。")
        return dict(path=path, success=True)

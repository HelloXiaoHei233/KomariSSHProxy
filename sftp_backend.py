"""把 Komari 面板的文件接口暴露为标准 SFTP 子系统。

面板只提供网页终端 WebSocket，没有文件协议；但它的 ``admin:file*`` RPC 与二进制
HTTP 通道可以读写节点文件。本模块把 ``KomariFileClient`` 适配成 paramiko 的
``SFTPServerInterface``，于是本地 SSH 代理端口和协助 SSH 端口都能直接使用
``sftp`` 与 ``scp``。

路径约定与 Agent 保持一致：Windows 节点上的 ``C:\\dir`` 在协议层表示为
``/C/dir``，Linux 节点就是普通的 ``/dir``。Agent 自己负责把虚拟路径还原成
本机路径，因此这里只做 POSIX 规范化，不做盘符映射。
"""
from __future__ import annotations

import os
import stat as stat_module
import tempfile
import threading
from typing import Any, BinaryIO

try:
    import paramiko
except ImportError:  # pragma: no cover - 由调用方的 require_paramiko 负责提示
    paramiko = None

from komari_files import KomariFileClient, KomariFileError, RemoteEntry


# paramiko 缺失时仍要保证模块可导入，便于 ssh_bridge 优雅降级。
_SFTPServerInterfaceBase = paramiko.SFTPServerInterface if paramiko else object
_SFTPHandleBase = paramiko.SFTPHandle if paramiko else object

SFTP_OK = paramiko.SFTP_OK if paramiko else 0
SFTP_EOF = paramiko.SFTP_EOF if paramiko else 1
SFTP_NO_SUCH_FILE = paramiko.SFTP_NO_SUCH_FILE if paramiko else 2
SFTP_PERMISSION_DENIED = paramiko.SFTP_PERMISSION_DENIED if paramiko else 3
SFTP_FAILURE = paramiko.SFTP_FAILURE if paramiko else 4
SFTP_OP_UNSUPPORTED = paramiko.SFTP_OP_UNSUPPORTED if paramiko else 8

# 单次远端读取的预读量；SFTP 客户端通常按 32 KiB 分块，预读可以显著减少往返。
CACHE_BYTES = 256 * 1024
# 写入先落在本地临时文件，超过该阈值自动溢写到磁盘，避免整文件占用内存。
SPOOL_LIMIT = 8 * 1024 * 1024

_ERROR_TO_SFTP = {
    "not_found": SFTP_NO_SUCH_FILE,
    "permission": SFTP_PERMISSION_DENIED,
    "unsupported": SFTP_OP_UNSUPPORTED,
    "offline": SFTP_FAILURE,
    "too_large": SFTP_FAILURE,
    "timeout": SFTP_FAILURE,
    "other": SFTP_FAILURE,
}


def sftp_subsystem_args(config, uuid: str, log=None) -> tuple:
    """返回 ``Transport.set_subsystem_handler`` 所需的 (处理器, 附加参数)。

    ``log`` 为可选的单行文本回调；传入后每次文件操作都会写入调用方的日志流。
    """
    if paramiko is None:
        raise RuntimeError("缺少 paramiko，无法提供 SFTP。")
    return paramiko.SFTPServer, KomariSFTPServerInterface, config, uuid, log


def _sftp_error(exc: Exception) -> int:
    if isinstance(exc, KomariFileError):
        return _ERROR_TO_SFTP.get(exc.kind, SFTP_FAILURE)
    return SFTP_FAILURE


def _open_mode(flags: int) -> str:
    """把 open 的 flags 归纳成日志里可读的中文模式。"""
    if flags & os.O_RDWR:
        return "读写"
    if flags & os.O_WRONLY:
        return "追加写" if flags & os.O_APPEND else "写入"
    return "读取"


def canonical_path(path: str) -> str:
    """POSIX 规范化：折叠 ``//``、``.`` 与 ``..``，并保留开头的 ``/``。"""
    text = (path or "/").strip() or "/"
    if not text.startswith("/"):
        text = "/" + text
    parts: list[str] = []
    for piece in text.split("/"):
        if piece in ("", "."):
            continue
        if piece == "..":
            if parts:
                parts.pop()
            continue
        parts.append(piece)
    return "/" + "/".join(parts)


def _mode_bits(entry: RemoteEntry) -> int:
    if entry.is_symlink:
        kind = stat_module.S_IFLNK
    elif entry.is_dir:
        kind = stat_module.S_IFDIR
    else:
        kind = stat_module.S_IFREG
    return kind | (entry.mode_octal & 0o7777)


def _attributes(entry: RemoteEntry, *, size: int | None = None) -> Any:
    attributes = paramiko.SFTPAttributes()
    attributes.filename = entry.name
    attributes.st_size = entry.size if size is None else size
    attributes.st_uid = entry.uid if entry.uid >= 0 else 0
    attributes.st_gid = entry.gid if entry.gid >= 0 else 0
    attributes.st_mode = _mode_bits(entry)
    attributes.st_mtime = int(entry.mtime)
    return attributes


class KomariSFTPHandle(_SFTPHandleBase):
    """读句柄按需预读；写句柄先本地缓冲，关闭时整体上传。"""

    def __init__(self, interface: "KomariSFTPServerInterface", path: str, flags: int):
        super().__init__(flags)
        self.interface = interface
        self.path = path
        self.flags = flags
        self._cache_start = 0
        self._cache = b""
        self._spool: BinaryIO | None = None
        self._dirty = False
        self._lock = threading.Lock()
        self._read_bytes = 0
        if flags & (os.O_WRONLY | os.O_RDWR):
            self._spool = tempfile.SpooledTemporaryFile(max_size=SPOOL_LIMIT)
            if flags & os.O_APPEND:
                self.interface.seed_append(self.path, self._spool)

    # ---- 读取 ----

    def read(self, offset: int, length: int) -> bytes | int:
        if length <= 0:
            return b""
        try:
            data = self._read(offset, length)
        except KomariFileError as exc:
            return _sftp_error(exc)
        except Exception:
            return SFTP_FAILURE
        if data:
            self._read_bytes += len(data)
        return data if data else SFTP_EOF

    def _read(self, offset: int, length: int) -> bytes:
        if self._spool is not None:
            # 已打开写句柄时以本地缓冲为准，避免读到尚未上传的旧内容。
            with self._lock:
                self._spool.flush()
                self._spool.seek(offset)
                return self._spool.read(length)
        cached = self._cache_start <= offset < self._cache_start + len(self._cache)
        if not self._cache or not cached:
            want = max(int(length), CACHE_BYTES)
            self._cache_start = offset
            self._cache = self.interface.read_range(self.path, offset, want)
        start = offset - self._cache_start
        return self._cache[start:start + length]

    # ---- 写入 ----

    def write(self, offset: int, data: bytes) -> int:
        if self._spool is None:
            return SFTP_PERMISSION_DENIED
        try:
            with self._lock:
                self._spool.seek(offset)
                self._spool.write(data)
                self._dirty = True
        except Exception:
            return SFTP_FAILURE
        return SFTP_OK

    def stat(self) -> Any:
        try:
            entry = self.interface.stat_entry(self.path)
        except KomariFileError as exc:
            return _sftp_error(exc)
        except Exception:
            return SFTP_FAILURE
        size = None
        if self._spool is not None and self._dirty:
            with self._lock:
                self._spool.flush()
                size = self._spool.seek(0, os.SEEK_END)
                self._spool.seek(0, os.SEEK_END)
        return _attributes(entry, size=size)

    def chattr(self, attr: Any) -> int:
        return self.interface.chattr(self.path, attr)

    def close(self) -> None:
        spool, dirty = self._spool, self._dirty
        read_bytes = self._read_bytes
        self._spool, self._dirty = None, False
        try:
            if spool is not None and dirty:
                # 上传失败会向上抛出，paramiko 会把它转换成 SFTP_FAILURE 状态。
                self.interface.upload(self.path, spool)
        finally:
            if spool is not None:
                spool.close()
            elif read_bytes:
                self.interface.log_line(f"下载 {self.path}（{read_bytes} 字节）")
        return super().close()


class KomariSFTPServerInterface(_SFTPServerInterfaceBase):
    """SFTP 会话级接口；每个子系统通道创建一个实例。"""

    def __init__(self, server: Any, config: Any, uuid: str, log=None):
        # 额外参数由 set_subsystem_handler 传入，不能再转发给父类。
        super().__init__(server)
        self.config = config
        self.uuid = uuid
        self.log = log
        self._client: KomariFileClient | None = None
        self._lock = threading.Lock()

    def log_line(self, text: str) -> None:
        """写入调用方日志流；未提供回调时静默，不影响文件操作。"""
        if self.log is not None:
            self.log(text)

    # ---- 会话生命周期 ----

    def session_started(self) -> None:
        with self._lock:
            if self._client is None:
                self._client = KomariFileClient(self.config, self.uuid)

    def session_ended(self) -> None:
        with self._lock:
            client, self._client = self._client, None
        if client is not None:
            client.close()

    def _files(self) -> KomariFileClient:
        with self._lock:
            if self._client is None:
                self._client = KomariFileClient(self.config, self.uuid)
            return self._client

    # ---- 供句柄使用 ----

    def read_range(self, path: str, offset: int, length: int) -> bytes:
        return self._files().read_range(canonical_path(path), offset, length)

    def stat_entry(self, path: str) -> RemoteEntry:
        return self._files().stat(canonical_path(path))

    def upload(self, path: str, source: BinaryIO) -> None:
        source.seek(0, os.SEEK_END)
        size = source.tell()
        source.seek(0)
        canonical = canonical_path(path)
        try:
            self._files().write(canonical, source, size)
        except KomariFileError as exc:
            self.log_line(f"上传失败 {canonical}（{size} 字节）：{exc}")
            raise
        self.log_line(f"上传 {canonical}（{size} 字节）")

    def seed_append(self, path: str, spool: BinaryIO) -> None:
        """O_APPEND：先把远端现有内容下载到本地缓冲，再续写。"""
        try:
            entry = self.stat_entry(path)
        except KomariFileError as exc:
            if exc.kind == "not_found":
                return
            raise
        remaining = entry.size
        offset = 0
        while remaining > 0:
            block = self.read_range(path, offset, min(CACHE_BYTES, remaining))
            if not block:
                break
            spool.write(block)
            offset += len(block)
            remaining -= len(block)
        spool.seek(0, os.SEEK_END)

    def chattr(self, path: str, attr: Any) -> int:
        canonical = canonical_path(path)
        try:
            mode = getattr(attr, "st_mode", None)
            if mode is not None:
                self._files().chmod(canonical, f"{stat_module.S_IMODE(mode):04o}")
            uid = getattr(attr, "st_uid", None)
            gid = getattr(attr, "st_gid", None)
            if uid is not None or gid is not None:
                self._files().chown(canonical, uid=uid, gid=gid)
        except KomariFileError as exc:
            self.log_line(f"修改属性失败 {canonical}：{exc}")
            return _sftp_error(exc)
        except Exception:
            return SFTP_FAILURE
        self.log_line(f"修改属性 {canonical}")
        return SFTP_OK

    # ---- paramiko 回调 ----

    def canonicalize(self, path: str) -> str:
        return canonical_path(path)

    def list_folder(self, path: str) -> Any:
        try:
            entries = self._files().list(canonical_path(path))
        except KomariFileError as exc:
            return _sftp_error(exc)
        except Exception:
            return SFTP_FAILURE
        return [_attributes(entry) for entry in entries]

    def stat(self, path: str) -> Any:
        try:
            return _attributes(self.stat_entry(path))
        except KomariFileError as exc:
            return _sftp_error(exc)
        except Exception:
            return SFTP_FAILURE

    def lstat(self, path: str) -> Any:
        return self.stat(path)

    def open(self, path: str, flags: int, attr: Any) -> Any:
        try:
            canonical = canonical_path(path)
            if flags & os.O_EXCL and self._exists(canonical):
                return SFTP_FAILURE
            if not flags & (os.O_WRONLY | os.O_RDWR):
                # os.O_RDONLY 恒为 0，只能按“既非只写也非读写”判断只读打开。
                # 只读打开必须先确认存在，否则客户端会拿到空句柄而不是错误。
                if not self._exists(canonical):
                    return SFTP_NO_SUCH_FILE
            self.log_line(f"打开 {canonical}（{_open_mode(flags)}）")
            return KomariSFTPHandle(self, canonical, flags)
        except KomariFileError as exc:
            return _sftp_error(exc)
        except Exception:
            return SFTP_FAILURE

    def _exists(self, path: str) -> bool:
        try:
            self._files().stat(path)
            return True
        except KomariFileError as exc:
            if exc.kind == "not_found":
                return False
            raise

    def remove(self, path: str) -> int:
        canonical = canonical_path(path)
        try:
            self._files().remove(canonical)
        except KomariFileError as exc:
            self.log_line(f"删除失败 {canonical}：{exc}")
            return _sftp_error(exc)
        except Exception:
            return SFTP_FAILURE
        self.log_line(f"删除 {canonical}")
        return SFTP_OK

    def rmdir(self, path: str) -> int:
        canonical = canonical_path(path)
        try:
            # Agent 的删除是递归的，SFTP 语义要求目录为空，这里先做检查以免误删内容。
            if self._files().list(canonical):
                self.log_line(f"拒绝删除非空目录 {canonical}")
                return SFTP_FAILURE
            self._files().remove(canonical)
        except KomariFileError as exc:
            self.log_line(f"删除目录失败 {canonical}：{exc}")
            return _sftp_error(exc)
        except Exception:
            return SFTP_FAILURE
        self.log_line(f"删除目录 {canonical}")
        return SFTP_OK

    def rename(self, oldpath: str, newpath: str) -> int:
        source, destination = canonical_path(oldpath), canonical_path(newpath)
        try:
            if self._exists(destination):
                self.log_line(f"重命名失败 {source} -> {destination}：目标已存在")
                return SFTP_FAILURE  # SFTP v3 的 rename 不覆盖已有目标。
            self._files().move(source, destination)
        except KomariFileError as exc:
            self.log_line(f"重命名失败 {source} -> {destination}：{exc}")
            return _sftp_error(exc)
        except Exception:
            return SFTP_FAILURE
        self.log_line(f"重命名 {source} -> {destination}")
        return SFTP_OK

    def posix_rename(self, oldpath: str, newpath: str) -> int:
        source, destination = canonical_path(oldpath), canonical_path(newpath)
        try:
            self._files().move(source, destination)
        except KomariFileError as exc:
            self.log_line(f"重命名失败 {source} -> {destination}：{exc}")
            return _sftp_error(exc)
        except Exception:
            return SFTP_FAILURE
        self.log_line(f"重命名 {source} -> {destination}")
        return SFTP_OK

    def mkdir(self, path: str, attr: Any) -> int:
        canonical = canonical_path(path)
        try:
            mode = ""
            if attr is not None and getattr(attr, "st_mode", None) is not None:
                mode = f"{stat_module.S_IMODE(attr.st_mode):04o}"
            self._files().mkdir(canonical, mode)
        except KomariFileError as exc:
            self.log_line(f"创建目录失败 {canonical}：{exc}")
            return _sftp_error(exc)
        except Exception:
            return SFTP_FAILURE
        self.log_line(f"创建目录 {canonical}")
        return SFTP_OK

    def symlink(self, target_path: str, path: str) -> int:
        return SFTP_OP_UNSUPPORTED  # Agent 没有提供创建符号链接的操作。

    def readlink(self, path: str) -> int:
        return SFTP_OP_UNSUPPORTED

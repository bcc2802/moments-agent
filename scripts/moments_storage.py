"""Standard-library write serialization and recoverable local snapshots.

Locks cover the full read/validate/write operation, including direct API callers.
The OS releases locks on process death; persistent temp lock files are not leases
and must not be deleted to 'unlock' another process.
"""
from contextlib import contextmanager, ExitStack, nullcontext
from functools import wraps
import hashlib
import os
from pathlib import Path
import stat
import tempfile
import threading
import time


class StorageError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


_slots = {}
_slots_guard = threading.Lock()


def serialized(method):
    @wraps(method)
    def guarded(self, *args, **kwargs):
        runner = getattr(self, "runner", self)
        # Pure in-memory adapters have no filesystem to serialize.
        with getattr(runner, "_write_guard", nullcontext)():
            return method(self, *args, **kwargs)
    return guarded


@contextmanager
def _lock(key, timeout):
    with _slots_guard:
        slot = _slots.setdefault(key, {"mutex": threading.RLock(), "depth": 0})
    if not slot["mutex"].acquire(timeout=timeout):
        raise StorageError("WRITE_BUSY", "另一个操作正在写入，请稍后重试同一指令。")
    handle = None
    try:
        if slot["depth"] == 0:
            root = Path(tempfile.gettempdir()) / "moments-agent-locks"
            root.mkdir(exist_ok=True)
            path = root / (key + ".lock")
            if path.is_symlink():
                raise StorageError("UNSAFE_PATH", "写锁路径异常，未执行写入。")
            handle = path.open("a+b")
            if path.stat().st_size == 0:
                handle.write(b"0")
                handle.flush()
            deadline = time.monotonic() + timeout
            while True:
                try:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except (BlockingIOError, PermissionError, OSError):
                    if time.monotonic() >= deadline:
                        raise StorageError("WRITE_BUSY", "另一个进程正在写入，请稍后重试同一指令。")
                    time.sleep(0.025)
            slot["handle"] = handle
            handle = None
        slot["depth"] += 1
        try:
            yield
        finally:
            slot["depth"] -= 1
            if slot["depth"] == 0:
                held = slot.pop("handle")
                try:
                    held.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(held.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(held.fileno(), fcntl.LOCK_UN)
                finally:
                    held.close()
    finally:
        if handle is not None:
            handle.close()
        slot["mutex"].release()


@contextmanager
def write_lock(paths, timeout=2.0):
    keys = sorted({hashlib.sha256(os.path.normcase(str(Path(p).resolve())).encode("utf-8")).hexdigest()
                   for p in paths})
    with ExitStack() as stack:
        for key in keys:
            stack.enter_context(_lock(key, timeout))
        yield


def snapshot(path, backup_root):
    """Content-addressed, byte-exact backup; never overwrite or follow links."""
    path, backup_root = Path(path), Path(backup_root)
    if not path.is_file() or path.is_symlink():
        raise StorageError("UNSAFE_PATH", "备份源文件缺失或包含链接，未执行替换。")
    for parent in [backup_root, *backup_root.parents]:
        if parent.exists():
            info = parent.lstat()
            if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0)
                    & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
                raise StorageError("UNSAFE_PATH", "备份目录包含链接或重解析点，未执行替换。")
    data = path.read_bytes()
    identity = hashlib.sha256(data).hexdigest()
    backup_root.mkdir(parents=True, exist_ok=True)
    target = backup_root / (path.name + "." + identity + ".bak")
    try:
        with target.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        if target.is_symlink() or target.read_bytes() != data:
            raise StorageError("BACKUP_INVALID", "已有备份损坏或路径异常，未执行替换。")
    if target.read_bytes() != data:
        raise StorageError("BACKUP_INVALID", "备份校验失败，未执行替换。")
    return target

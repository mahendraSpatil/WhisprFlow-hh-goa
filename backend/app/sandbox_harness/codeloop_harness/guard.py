"""Best-effort isolation applied inside the sandboxed interpreter.

In Docker mode the container already has no network; these guards matter for
subprocess mode, where they are a safety net against accidents, not a security
boundary: native code can bypass them.

- network: sockets may only reach loopback, and DNS lookups of other hosts fail
- filesystem: writes outside the sandbox (and the temp dir) raise
- processes: optionally, spawning subprocesses raises (used for probes)
"""

import os
import socket
import sys
import tempfile


class SandboxViolation(PermissionError):
    pass


_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0", ""}
_installed = False


def _is_local(host) -> bool:
    if isinstance(host, bytes):
        host = host.decode(errors="replace")
    host = str(host)
    return host in _LOCAL_HOSTS or host.startswith("127.")


def block_network() -> None:
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo
    inet = {socket.AF_INET, getattr(socket, "AF_INET6", socket.AF_INET)}

    def check(sock, address):
        if sock.family in inet and isinstance(address, tuple) and not _is_local(address[0]):
            raise SandboxViolation(f"network access is blocked in the CodeLoop sandbox ({address[0]}:{address[1]})")

    def connect(self, address):
        check(self, address)
        return real_connect(self, address)

    def connect_ex(self, address):
        check(self, address)
        return real_connect_ex(self, address)

    def getaddrinfo(host, *args, **kwargs):
        if host is not None and not _is_local(host):
            raise SandboxViolation(f"network access is blocked in the CodeLoop sandbox (DNS lookup of {host})")
        return real_getaddrinfo(host, *args, **kwargs)

    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    socket.getaddrinfo = getaddrinfo


_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
_PATH_EVENTS = {
    "os.remove", "os.rmdir", "os.mkdir", "os.rename", "os.replace", "os.truncate", "os.chmod",
    "os.symlink", "os.link", "shutil.rmtree", "shutil.move", "shutil.copyfile", "shutil.copytree",
}
_PROCESS_EVENTS = {"subprocess.Popen", "os.system", "os.exec", "os.spawn", "os.posix_spawn", "os.startfile", "os.fork"}
# Devices that discard or echo output: harmless to write (pytest's logging plugin opens NUL on Windows).
_DEVICES = {"nul", "\\\\.\\nul", "con", "/dev/null", "/dev/stdout", "/dev/stderr", os.devnull.lower()}


def guard_filesystem_and_processes(allowed_roots, block_processes: bool) -> None:
    allowed = [os.path.realpath(p) for p in (*allowed_roots, tempfile.gettempdir())]

    def inside(path) -> bool:
        if isinstance(path, int):
            return True  # an already-open file descriptor
        try:
            if os.fsdecode(path).lower() in _DEVICES:
                return True
        except (TypeError, ValueError):
            return True
        try:
            real = os.path.realpath(os.fsdecode(path))
        except (TypeError, ValueError):
            return True
        return any(real == root or real.startswith(root + os.sep) for root in allowed)

    def hook(event, args):
        if event == "open":
            path, mode, flags = args
            if path is None:
                return
            writing = bool(mode and any(c in mode for c in "wax+")) or bool((flags or 0) & _WRITE_FLAGS)
            if writing and not inside(path):
                raise SandboxViolation(f"writing outside the CodeLoop sandbox is blocked: {path!r}")
        elif event in _PATH_EVENTS:
            for arg in args[:2]:
                if isinstance(arg, (str, bytes, os.PathLike)) and not inside(arg):
                    raise SandboxViolation(f"{event} outside the CodeLoop sandbox is blocked: {arg!r}")
        elif event == "sqlite3.connect":
            database = args[0]
            if isinstance(database, (str, bytes, os.PathLike)):
                name = os.fsdecode(database)
                if name not in ("", ":memory:") and not name.startswith("file:") and not inside(name):
                    raise SandboxViolation(f"opening a database outside the CodeLoop sandbox is blocked: {name!r}")
        elif block_processes and event in _PROCESS_EVENTS:
            raise SandboxViolation(f"starting processes is blocked while probing ({event})")

    sys.addaudithook(hook)


def install(root: str, *, block_processes: bool = False) -> None:
    """Apply every guard once per interpreter. Extra writable dirs come from CODELOOP_ALLOW_WRITE."""
    global _installed
    if _installed:
        return
    _installed = True
    extra = [p for p in os.environ.get("CODELOOP_ALLOW_WRITE", "").split(os.pathsep) if p]
    block_network()
    guard_filesystem_and_processes([root, *extra], block_processes)

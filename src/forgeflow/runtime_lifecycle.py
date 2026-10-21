"""Local lifecycle primitives. No credentials, process arguments or ERP calls.

A lease serializes cooperating launchers; it does not fence unknown Docker or
provider jobs. PID identity includes kernel start time, never just a PID file.
"""
from __future__ import annotations

import ctypes
import fcntl
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path


class LifecycleError(RuntimeError):
    pass


def read_record(path: Path) -> dict | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError:
        raise LifecycleError("unsafe_lifecycle_record") from None
    try:
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_nlink != 1 or info.st_mode & 0o077 or info.st_size > 65536):
                raise ValueError
            result = json.loads(handle.read(65537))
        if not isinstance(result, dict):
            raise ValueError
        return result
    except (OSError, ValueError):
        raise LifecycleError("invalid_lifecycle_record") from None


def write_record(path: Path, value: dict) -> None:
    # Refuse unsafe existing records, including legacy plain PID files.
    read_record(path)
    fd, temporary = tempfile.mkstemp(prefix=".lifecycle-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextmanager
def exclusive_lease(path: Path, *, create: bool = True):
    """Nonblocking lock for a cooperating lifecycle or heavy-operation scope."""
    if create:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.is_symlink():
        raise LifecycleError("unsafe_lease_directory")
    try:
        fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
                     | (os.O_CREAT if create else 0), 0o600)
    except FileNotFoundError:
        if not create:
            yield
            return
        raise
    except OSError:
        raise LifecycleError("unsafe_lease") from None
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or info.st_mode & 0o077):
            raise LifecycleError("unsafe_lease")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise LifecycleError("operation_in_progress") from None
        yield
    finally:
        os.close(fd)


def process_identity(pid: int) -> dict | None:
    """Kernel identity on the supported macOS/Linux hosts; never read cmdline/env."""
    if type(pid) is not int or pid <= 1:
        raise LifecycleError("invalid_pid")
    if sys.platform == "darwin":
        class BsdInfo(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint32) for name in (
                "flags", "status", "xstatus", "pid", "ppid", "uid", "gid", "ruid",
                "rgid", "svuid", "svgid", "reserved")]
            _fields_ += [("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32)]
            _fields_ += [(name, ctypes.c_uint32) for name in (
                "nfiles", "pgid", "jobc", "tdev", "tpgid", "nice")]
            _fields_ += [("start_sec", ctypes.c_uint64), ("start_usec", ctypes.c_uint64)]
        info = BsdInfo()
        library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        library.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                                        ctypes.c_void_p, ctypes.c_int]
        library.proc_pidinfo.restype = ctypes.c_int
        length = library.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
        if length == 0:
            if ctypes.get_errno() in (0, 3):  # ESRCH: absent, not permission denied.
                return None
            raise LifecycleError("process_identity_unavailable")
        if length != ctypes.sizeof(info) or info.pid != pid or not info.start_sec:
            raise LifecycleError("process_identity_unavailable")
        if info.status == 5:  # zombie
            return None
        return {"pid": pid, "uid": info.uid, "ppid": info.ppid, "pgid": info.pgid,
                "start": f"mac:{info.start_sec}:{info.start_usec}"}
    if sys.platform.startswith("linux"):
        try:
            path = Path(f"/proc/{pid}")
            raw = (path / "stat").read_text()
            fields = raw[raw.rfind(")") + 2:].split()
            if fields[0] == "Z":
                return None
            boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            return {"pid": pid, "uid": path.stat().st_uid, "ppid": int(fields[1]),
                    "pgid": int(fields[2]), "start": f"linux:{boot}:{fields[19]}"}
        except FileNotFoundError:
            return None
        except (OSError, ValueError, IndexError):
            raise LifecycleError("process_identity_unavailable") from None
    raise LifecycleError("process_identity_unsupported")


def same_process(identity: dict, observed: dict | None) -> bool:
    return observed is not None and all(
        identity.get(key) == observed.get(key) for key in ("pid", "uid", "start")
    )


def managed_process(path: Path) -> tuple[dict | None, str]:
    record = read_record(path)
    if record is None:
        return None, "not_recorded"
    identity = record.get("identity")
    if (record.get("schema_version") != 1 or not isinstance(identity, dict)
            or type(identity.get("pid")) is not int or identity["pid"] <= 1
            or identity.get("uid") != os.getuid()
            or not isinstance(identity.get("start"), str) or not identity["start"]):
        raise LifecycleError("invalid_process_record")
    current = process_identity(identity["pid"])
    return record, "exited" if current is None else "running" if same_process(identity, current) else "pid_reused"


def record_process(path: Path, pid: int, role: str) -> dict:
    identity = process_identity(pid)
    if identity is None or identity["uid"] != os.getuid() or identity["pgid"] != pid:
        raise LifecycleError("cannot_bind_process_identity")
    write_record(path, {"schema_version": 1, "role": role, "identity": identity,
                        "children": [], "recorded_at": time.time()})
    return identity


def spawn_managed(path: Path, command: list[str], role: str, **kwargs) -> subprocess.Popen:
    """Persist process ownership before the child can start a service or fork.

    The short bootstrap waits on a private pipe. A parent failure closes it and
    exits the bootstrap; no unrecorded daemon is launched when saving fails.
    """
    reader, writer = os.pipe()
    process = None
    bootstrap = (
        "import os,sys; fd=int(sys.argv[1]); token=os.read(fd,1); os.close(fd); "
        "command=sys.argv[2:]; "
        "os.execvpe(command[0],command,os.environ) if token==b'G' else sys.exit(125)"
    )
    try:
        process = subprocess.Popen(
            [sys.executable, "-I", "-B", "-S", "-c", bootstrap, str(reader), *command],
            pass_fds=(reader,), start_new_session=True, **kwargs,
        )
        os.close(reader)
        reader = -1
        process._forgeflow_identity = record_process(path, process.pid, role)
        os.write(writer, b"G")
        return process
    except BaseException:
        os.close(writer)
        writer = -1
        if process is not None:
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                # Before G the child is our bootstrap, with no service children.
                process.kill()
                process.wait(timeout=2)
        raise
    finally:
        if reader >= 0:
            os.close(reader)
        if writer >= 0:
            os.close(writer)


def descendants(parent: int) -> list[dict]:
    table = subprocess.run(["ps", "-axo", "pid=,ppid="], capture_output=True,
                           text=True, check=True, timeout=5)
    pairs = [tuple(map(int, line.split())) for line in table.stdout.splitlines() if line.strip()]
    found, frontier = [], {parent}
    while frontier:
        parents = {pid: ppid for pid, ppid in pairs if ppid in frontier and pid != parent}
        children = set(parents)
        frontier = children - {item["pid"] for item in found}
        if not frontier:
            break
        for pid in frontier:
            identity = process_identity(pid)
            if (identity is not None and identity["uid"] == os.getuid()
                    and identity["ppid"] == parents[pid]):
                found.append(identity)
        # Processes disappearing during capture must not produce an endless pass.
        pairs = [(pid, ppid) for pid, ppid in pairs if pid not in children]
    return found


def stop_process(path: Path, *, timeout: float = 5) -> None:
    record, status = managed_process(path)
    if record is None:
        return
    if status == "pid_reused":
        raise LifecycleError("pid_reused_refusing_signal")
    children = record.get("children", [])
    if not isinstance(children, list) or any(not isinstance(item, dict) for item in children):
        raise LifecycleError("invalid_process_record")
    if any(type(item.get("pid")) is not int or item["pid"] <= 1
           or item.get("uid") != os.getuid() or not isinstance(item.get("start"), str)
           for item in children):
        raise LifecycleError("invalid_child_identity")
    if status == "running":
        observed = descendants(record["identity"]["pid"])
        if not same_process(record["identity"], process_identity(record["identity"]["pid"])):
            raise LifecycleError("parent_identity_changed_during_capture")
        # A previous interrupted stop may already have orphaned captured children.
        # Preserve their identities; do not discard them when scanning the tree.
        children = list({(item["pid"], item["uid"], item["start"]): item
                         for item in [*children, *observed]}.values())
        record["children"] = children
        write_record(path, record)  # Durable before the parent can orphan children.
    identities = [record["identity"], *children]
    if any(type(item.get("pid")) is not int or item["pid"] <= 1
           or item.get("uid") != os.getuid() or not isinstance(item.get("start"), str)
           for item in identities):
        raise LifecycleError("invalid_child_identity")
    for item in identities:
        if same_process(item, process_identity(item["pid"])):
            try:
                os.kill(item["pid"], signal.SIGTERM)
            except ProcessLookupError:
                pass
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not any(same_process(item, process_identity(item["pid"])) for item in identities):
            break
        time.sleep(0.1)
    for item in identities:
        if same_process(item, process_identity(item["pid"])):
            try:
                os.kill(item["pid"], signal.SIGKILL)
            except ProcessLookupError:
                pass
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if not any(same_process(item, process_identity(item["pid"])) for item in identities):
            path.unlink(missing_ok=True)
            return
        time.sleep(0.05)
    raise LifecycleError("process_stop_unconfirmed")


def resource_snapshot(path: Path) -> dict:
    """Best-effort diagnostics; unavailable measurements remain null."""
    result = {"measured_at": time.time(), "disk_free_bytes": None,
              "available_memory_bytes": None, "free_memory_bytes": None, "swap_used_bytes": None}
    try:
        result["disk_free_bytes"] = shutil.disk_usage(path).free
    except OSError:
        pass
    try:
        if sys.platform.startswith("linux"):
            values = {parts[0].rstrip(":"): int(parts[1]) * 1024
                      for line in Path("/proc/meminfo").read_text().splitlines()
                      if len(parts := line.split()) >= 2}
            result.update(available_memory_bytes=values.get("MemAvailable"),
                          swap_used_bytes=values["SwapTotal"] - values["SwapFree"])
        elif sys.platform == "darwin":
            pages = subprocess.run(["vm_stat"], capture_output=True, text=True,
                                   check=True, timeout=5).stdout
            size = re.search(r"page size of (\d+) bytes", pages)
            free = re.search(r"Pages free:\s+(\d+)", pages)
            if size and free:
                result["free_memory_bytes"] = int(size[1]) * int(free[1])
            output = subprocess.run(["sysctl", "-n", "vm.swapusage"], capture_output=True,
                                    text=True, check=True, timeout=5).stdout.split()
            value = output[output.index("used") + 2]
            if value.endswith("M"):
                result["swap_used_bytes"] = int(float(value[:-1]) * 1024**2)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        pass
    return result


def process_rss(identity: dict) -> int | None:
    if not same_process(identity, process_identity(identity["pid"])):
        return None
    try:
        output = subprocess.run(["ps", "-p", str(identity["pid"]), "-o", "rss="],
                                capture_output=True, text=True, check=True, timeout=5)
        value = int(output.stdout.strip()) * 1024
        return value if same_process(identity, process_identity(identity["pid"])) else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None

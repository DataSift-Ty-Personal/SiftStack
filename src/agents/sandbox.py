"""The code sandbox: Python an employee writes, run on the brain's own machine with no key, no
network, no brain data and a hard stop (the owner, 2026-11-13: "we should absolutely allow the code
sandbox").

Why it exists: the owner's everyday Claude Code move is a one-off script (a recount, a join of two
lists, a spreadsheet, a chart), and in Slack the team could only run its wired workflows. The brain's
machine is also where every API key lives (environment variables of its main process), where the
volume holds the queue, the account's log copy, every workspace and every Claude session, and where
a Fly API socket can reach the platform. So the code runs as somebody else, with nothing:

  1. a fresh network namespace (unshare --net): one loopback, and it is down. No socket leaves the
     machine, and the brain's own port is out of reach too;
  2. a dedicated unprivileged user (setpriv --reuid/--regid --clear-groups). The brain's environment
     (/proc/<pid>/environ) is readable by its owner only, and /data is 0700 root (harden(), run at
     brain startup);
  3. no_new_privs: a setuid binary cannot hand root back;
  4. an environment that is REPLACED, never inherited: PATH, HOME, LANG, PYTHONIOENCODING,
     MPLBACKEND and TMPDIR, nothing else;
  5. limits set in the child before exec (CPU, address space, file size, open files, processes, no
     core files), a lower CPU priority and the kernel's first pick when memory runs out (the brain's
     machine is one shared CPU and 1 GB, so a runaway must lose to the brain, never the other way
     round), a wall clock, and a watch on how much the code writes;
  6. a bootstrap that re-checks 1 to 4 from INSIDE the child and refuses to start the code when any
     of them did not take;
  7. afterwards the process group and every process the sandbox user still owns is killed (a
     process can leave its group, it cannot leave its user), the work folder is deleted, and only
     regular files the code itself wrote at the top of out/ are copied out, each opened without
     following a link: the brain copies as root, so a link the code planted at /data/agents.db would
     otherwise be read with root's rights and served in Slack.

It FAILS CLOSED. Off Linux, without the user, without unshare or setpriv, with a brain folder open,
or when the bootstrap's own check fails, nothing runs and the tool says why in one sentence.

    python -m agents.sandbox --check      on the box, from /app/src: PASS or FAIL per guarantee
    python -m agents.sandbox --harden     close the brain's folders now, as the brain does at startup

export_data writes the CSV inputs the code reads (it has no network): records through query.py's
proven filters and its budgeted client, the brain's copy of the activity log, and the brain's own
tables. A phone number or an email never goes into an export.
"""
from __future__ import annotations

import asyncio
import contextlib
import csv
import json
import logging
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import config

log = logging.getLogger(__name__)

EXECUTOR = None     # the selftest injects a fake; otherwise a LinuxExecutor for each run

ENV_KEYS = ("PATH", "HOME", "LANG", "PYTHONIOENCODING", "MPLBACKEND", "TMPDIR")
SAFE_PATH = "/usr/local/bin:/usr/bin:/bin"
TOOL_PATH = "/usr/sbin:/usr/bin:/sbin:/bin"     # where unshare and setpriv are found, never the brain's own PATH
MARK = "sandbox: isolated, starting the code"
REFUSED_EXIT = 97
KINDS = (".xlsx", ".csv", ".png", ".pdf", ".md", ".json", ".txt", ".docx")
FLY_DIR = "/.fly"
MB = 1024 * 1024
SIGKILL, SIGXCPU, SIGXFSZ = 9, 24, 25
WINDOW = 10000      # the records search refuses offset + limit past this
_BIN = getattr(os, "O_BINARY", 0)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_UUIDISH = re.compile(r"^(?:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
                      r"|[0-9a-fA-F]{32})$")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_PHONE = re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)")


class Refused(ValueError):
    """A run or an export this module will not do as asked. The message is one plain sentence the
    employee can repeat to the operator."""


class Busy(Exception):
    """Another run holds the sandbox on this machine."""


# ------------------------------------------------------------------------------------- settings
def _num(name: str, default: float, low: float = 1.0) -> float:
    """A number from the environment; the default when it is unset, unreadable or below `low`."""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return float(default)
    try:
        v = float(raw)
    except ValueError:
        return float(default)
    return v if math.isfinite(v) and v >= low else float(default)


def enabled() -> bool:
    """AGENTS_SANDBOX=0 takes run_python and export_data off every employee."""
    return (os.environ.get("AGENTS_SANDBOX") or "1").strip().lower() not in ("0", "off", "no", "false")


def user_name() -> str:
    """The dedicated user the code runs as. It must own nothing else: every process it owns is
    killed after each run."""
    return (os.environ.get("AGENTS_SANDBOX_USER") or "sandbox").strip()


def base_dir() -> Path:
    return Path(os.environ.get("AGENTS_SANDBOX_DIR") or "/tmp/sbx")


def python_path() -> str:
    return (os.environ.get("AGENTS_SANDBOX_PYTHON") or "/usr/local/bin/python3").strip()


@dataclass
class Limits:
    """What one run may use. The defaults are the owner's (2026-11-13); each one is a setting."""
    cpu_s: int = 120
    wall_s: float = 180.0
    mem_mb: int = 2048
    file_mb: int = 200
    open_files: int = 256
    procs: int = 64
    work_mb: int = 1024
    made_files: int = 20000

    @classmethod
    def from_env(cls) -> "Limits":
        return cls(cpu_s=int(_num("AGENTS_SANDBOX_CPU_S", 120)), wall_s=_num("AGENTS_SANDBOX_WALL_S", 180),
                   mem_mb=int(_num("AGENTS_SANDBOX_MEM_MB", 2048, 64)),
                   file_mb=int(_num("AGENTS_SANDBOX_FILE_MB", 200)),
                   open_files=int(_num("AGENTS_SANDBOX_OPEN_FILES", 256, 16)),
                   procs=int(_num("AGENTS_SANDBOX_PROCS", 64, 4)), work_mb=int(_num("AGENTS_SANDBOX_WORK_MB", 1024)),
                   made_files=int(_num("AGENTS_SANDBOX_MADE_FILES", 20000, 10)))

    def rlimits(self) -> list[tuple[str, int, int]]:
        """(resource, soft, hard), set in the child before exec. The CPU hard limit sits 5 s above the
        soft one, so the soft one arrives as a signal and the hard one as a kill nothing can catch.
        Core files are off: a crash must not leave a copy of the process on disk."""
        return [("RLIMIT_CPU", self.cpu_s, self.cpu_s + 5),
                ("RLIMIT_AS", self.mem_mb * MB, self.mem_mb * MB),
                ("RLIMIT_FSIZE", self.file_mb * MB, self.file_mb * MB),
                ("RLIMIT_NOFILE", self.open_files, self.open_files),
                ("RLIMIT_NPROC", self.procs, self.procs),
                ("RLIMIT_CORE", 0, 0)]

    def words(self) -> str:
        mem = ("%g GB" % (self.mem_mb / 1024.0)) if self.mem_mb >= 1024 else ("%d MB" % self.mem_mb)
        return "%d s of CPU, a %d s wall clock, %s of memory, files up to %d MB and %d processes" % (
            self.cpu_s, self.wall_s, mem, self.file_mb, self.procs)


# ------------------------------------------------------------------------------------- the child
def command(uid: int, gid: int, boot, code, python: str | None = None, unshare: str = "unshare",
            setpriv: str = "setpriv") -> list[str]:
    """The child's command line, in list form (never a shell). The network goes first, while the
    process is still root (a namespace needs root); then the user changes and no_new_privs is set;
    the interpreter runs isolated (-I: no PYTHON* variables, no user site, the script's folder not on
    the path) and writes no bytecode (-B). The bootstrap then becomes `python3 -I -B code.py`."""
    py = python or python_path()
    return [unshare, "--net", "--", setpriv, "--reuid=%d" % uid, "--regid=%d" % gid, "--clear-groups",
            "--no-new-privs", "--", py, "-I", "-B", str(boot), str(uid), str(gid), str(code)]


def child_env(work) -> dict:
    """The child's whole environment, built from nothing, so no key can ride along. Python under -I
    ignores PYTHONIOENCODING, so LANG carries the UTF-8; the variable stays for any Python the code
    starts itself."""
    w = str(work)
    return {"PATH": SAFE_PATH, "HOME": w, "LANG": "C.UTF-8", "PYTHONIOENCODING": "utf-8", "MPLBACKEND": "Agg",
            "TMPDIR": w}


_BOOT = r'''"""The sandbox's first step, inside the child: start the code only if the isolation took."""
import os
import socket
import sys

ALLOWED = @ALLOWED@


def refuse(why):
    sys.stderr.write("sandbox refused to start: %s\n" % why)
    sys.stderr.flush()
    os._exit(@EXIT@)


def main():
    uid, gid, code = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
    if 0 in (os.getuid(), os.geteuid()) or {os.getuid(), os.geteuid()} != {uid}:
        refuse("it is not running as the sandbox user")
    if {os.getgid(), os.getegid()} != {gid}:
        refuse("it is not running in the sandbox group")
    if os.getgroups():
        refuse("it still carries other groups")
    status = {}
    with open("/proc/self/status") as f:
        for line in f:
            key, _, value = line.partition(":")
            status[key] = value.strip()
    if status.get("NoNewPrivs") != "1":
        refuse("no_new_privs is not set")
    for cap in ("CapEff", "CapPrm", "CapAmb"):
        if int(status.get(cap) or "1", 16) != 0:
            refuse("it still holds capabilities")
    if sorted(name for _, name in socket.if_nameindex()) != ["lo"]:
        refuse("the network is not cut off")
    if set(os.environ) != ALLOWED:
        refuse("its environment is not the sandbox's own")
    sys.stderr.write(@MARK@ + "\n")
    sys.stderr.flush()
    os.execv(sys.executable, [sys.executable, "-I", "-B", code])


main()
'''
BOOT = (_BOOT.replace("@ALLOWED@", "{%s}" % ", ".join(repr(k) for k in sorted(ENV_KEYS)))
        .replace("@EXIT@", str(REFUSED_EXIT)).replace("@MARK@", repr(MARK)))


# ------------------------------------------------------------------------------------- the machine
def _bits(st, uid: int, gid: int) -> int:
    """The rwx bits (0 to 7) the sandbox user gets on one inode. It has no supplementary group."""
    if st.st_uid == uid:
        return (st.st_mode >> 6) & 7
    if st.st_gid == gid:
        return (st.st_mode >> 3) & 7
    return st.st_mode & 7


def closed_to(path, uid: int, gid: int) -> bool:
    """True when the sandbox user can neither list nor read `path`: a folder on the way down denies
    it passage, or the path itself grants it nothing. A path that does not exist yet counts as open
    unless a folder above it is closed, since the brain may create it later with the usual mode."""
    p = Path(os.path.abspath(str(path)))
    chain = list(reversed(p.parents)) + [p]
    for i, d in enumerate(chain):
        try:
            st = os.stat(str(d))
        except FileNotFoundError:
            return False
        except OSError:
            return True
        bits = _bits(st, uid, gid)
        if i < len(chain) - 1:
            if not bits & 1:
                return True
            continue
        return (bits & (5 if stat.S_ISDIR(st.st_mode) else 4)) == 0
    return True


def fly_sockets(uid: int, gid: int, root: str = FLY_DIR) -> list[str]:
    """Unix sockets under the Fly folder the sandbox user could connect to (a connect needs write
    permission on the socket and passage through the folders). /.fly/api is the Machines API: whoever
    reaches it can start a machine that carries every secret of this app, with a network."""
    out: list[str] = []
    try:
        if not _bits(os.stat(root), uid, gid) & 1:
            return out
    except OSError:
        return out
    stack = [(root, 0)]
    while stack:
        d, depth = stack.pop()
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        for e in entries:
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            bits = _bits(st, uid, gid)
            if stat.S_ISSOCK(st.st_mode) and bits & 2:
                out.append(e.path)
            elif stat.S_ISDIR(st.st_mode) and bits & 1 and depth < 2:
                stack.append((e.path, depth + 1))
    return sorted(out)


def base_problem(path, uid: int, gid: int) -> str:
    """Why the folder the runs live in cannot be trusted, or "". /tmp is open to every user, so a
    folder named /tmp/sbx that someone else made (or a link of that name) could steer the brain's
    root-owned writes; it must be root's own plain folder that nobody else can change."""
    p = str(path)
    try:
        st = os.lstat(p)
    except FileNotFoundError:
        parent = os.path.dirname(p.rstrip("/")) or "/"
        return ("the sandbox folder %s would sit where the sandbox user cannot reach it" % p
                if closed_to(parent, uid, gid) else "")
    except OSError as exc:
        return "the sandbox folder %s cannot be read (%s)" % (p, exc.strerror or exc)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        return "the sandbox folder %s is not a plain folder" % p
    if st.st_uid != 0:
        return "the sandbox folder %s is not owned by root" % p
    if st.st_mode & 0o022:
        return "other users can change the sandbox folder %s" % p
    if closed_to(p, uid, gid):
        return "the sandbox user cannot reach the sandbox folder %s" % p
    return ""


def brain_paths() -> list[Path]:
    """Every folder of the brain's the code must never reach: the volume (the queue, the log copy,
    the run folders) and, wherever they are set, the workspaces and the Claude sessions."""
    raw = [config.DATA_DIR, Path(config.DB_PATH).parent, config.RUNS_DIR, config.WORKSPACES_DIR]
    if os.environ.get("CLAUDE_CONFIG_DIR"):
        raw.append(Path(os.environ["CLAUDE_CONFIG_DIR"]))
    out, seen = [], set()
    for p in raw:
        key = os.path.abspath(str(p))
        if key not in seen:
            seen.add(key)
            out.append(Path(key))
    return out


class Host:
    """The facts the preflight reads, in one object, so the selftest can stand in for the box."""

    platform = sys.platform

    def euid(self) -> int:
        return os.geteuid()

    def user(self, name: str):
        import pwd
        try:
            p = pwd.getpwnam(name)
        except KeyError:
            return None
        return p.pw_uid, p.pw_gid

    def which(self, tool: str) -> str:
        return shutil.which(tool, path=TOOL_PATH) or ""

    def exists(self, path) -> bool:
        return os.path.exists(str(path))

    def owner(self, path) -> int:
        try:
            return os.lstat(str(path)).st_uid
        except OSError:
            return -1

    def chmod(self, path, mode: int) -> None:
        os.chmod(str(path), mode)

    def closed(self, path, uid: int, gid: int) -> bool:
        return closed_to(path, uid, gid)

    def fly_sockets(self, uid: int, gid: int) -> list[str]:
        return fly_sockets(uid, gid)

    def base_problem(self, path, uid: int, gid: int) -> str:
        return base_problem(path, uid, gid)


def preflight(host=None) -> tuple[bool, str, dict]:
    """(ready, why not, the ids and tools to use). Every step the isolation needs is checked before a
    run; one that fails means no run, never a run with less isolation."""
    h = host or Host()
    plat = str(getattr(h, "platform", sys.platform))
    if not plat.startswith("linux"):
        return False, "it runs only on the brain's Linux machine, and this one is %s" % plat, {}
    if h.euid() != 0:
        return False, "the brain is not running as root, so it cannot hand the code to an unprivileged user", {}
    name = user_name()
    found = h.user(name)
    if not found:
        return False, "there is no %r user on this machine (the image adds it with useradd)" % name, {}
    uid, gid = found
    if uid == 0 or gid == 0:
        return False, "the %r user is root or in the root group" % name, {}
    tools = {}
    for tool in ("unshare", "setpriv"):
        p = h.which(tool)
        if not p:
            return False, "%s is not installed (it comes with util-linux)" % tool, {}
        tools[tool] = p
    if not h.exists(python_path()):
        return False, "the sandbox's Python (%s) is not on this machine" % python_path(), {}
    for p in brain_paths():
        if not h.closed(p, uid, gid):
            return False, ("%s is open to the sandbox user; the brain closes it to root only at startup "
                           "(sandbox.harden)" % p), {}
    socks = h.fly_sockets(uid, gid)
    if socks:
        return False, "the sandbox user could open %s, the machine's Fly API socket" % socks[0], {}
    bad = h.base_problem(base_dir(), uid, gid)
    if bad:
        return False, bad, {}
    return True, "", {"uid": uid, "gid": gid, "unshare": tools["unshare"], "setpriv": tools["setpriv"]}


def harden(host=None) -> list[str]:
    """Close the brain's own folders to the sandbox user, at brain startup: /data (the queue, the log
    copy, every workspace and Claude session) to 0700 root, and the Fly folder when the sandbox user
    could open a socket in it. Linux and root only, and only a folder root owns; changes nothing
    anywhere else. Returns one line per change, for the startup log."""
    h = host or Host()
    if not str(getattr(h, "platform", "")).startswith("linux") or h.euid() != 0:
        return []
    found = h.user(user_name())
    uid, gid = found if found else (-1, -1)     # no user yet: close it against everyone else anyway
    said = []
    for p in brain_paths():
        if h.exists(p) and h.owner(p) == 0 and not h.closed(p, uid, gid):
            h.chmod(p, 0o700)
            said.append("sandbox: closed %s to root only (mode 0700)" % p)
    if found and h.fly_sockets(uid, gid):
        h.chmod(FLY_DIR, 0o700)
        said.append("sandbox: closed %s to root only, since the sandbox user could open a socket in it" % FLY_DIR)
    return said


# ------------------------------------------------------------------------------------- executors
@dataclass
class Job:
    argv: list
    env: dict
    cwd: str
    work: str
    private: str
    limits: Limits
    uid: int | None = None


@dataclass
class Outcome:
    returncode: int | None = None
    killed_for: str = ""         # "", "wall", "size", "files"
    seconds: float = 0.0
    stdout: bytes = b""
    stderr: bytes = b""
    max_procs: int = 0
    left_running: int = 0
    error: str = ""              # the process could not be started at all
    stdout_total: int = 0        # bytes the code wrote, of which `stdout` holds the first ones
    stderr_total: int = 0


@dataclass
class Ran:
    started: bool
    why: str = ""
    returncode: int | None = None
    killed_for: str = ""
    ended: str = ""
    seconds: float = 0.0
    stdout: str = ""
    stderr: str = ""
    files: list = field(default_factory=list)
    skipped: list = field(default_factory=list)
    max_procs: int = 0
    left_running: int = 0
    cut: str = ""                # which streams were longer than the log keeps

    @property
    def exit_code(self):
        return self.returncode if self.returncode is not None and self.returncode >= 0 else None


class Executor:
    """What execute() needs from the machine. The selftest's fake answers the same calls."""
    real = False

    def ready(self) -> tuple[bool, str]:
        return False, "no executor is set up"

    def ids(self) -> tuple[int, int]:
        return 0, 0

    def tools(self) -> tuple[str, str]:
        return "unshare", "setpriv"

    @contextlib.contextmanager
    def lock(self, wait_s: float):
        yield

    def before(self) -> None:
        pass

    def run(self, job: Job) -> Outcome:
        raise NotImplementedError

    def after(self, run_dir: Path) -> None:
        shutil.rmtree(str(run_dir), ignore_errors=True)


def _owned_pids(uid) -> list[int]:
    """Every live process the sandbox user owns (any of its real, effective, saved or file uids)."""
    if uid is None:
        return []
    out = []
    try:
        names = os.listdir("/proc")
    except OSError:
        return out
    want = str(uid)
    for name in names:
        if not name.isdigit():
            continue
        uids, state = [], ""
        try:
            with open("/proc/%s/status" % name) as f:
                for line in f:
                    if line.startswith("Uid:"):
                        uids = line.split()[1:5]
                    elif line.startswith("State:"):
                        state = (line.split() + ["", ""])[1]
        except OSError:
            continue
        if want in uids and state != "Z":
            out.append(int(name))
    return out


def _sweep(uid) -> int:
    """Kill every process the sandbox user owns; how many are still there afterwards (must be 0).
    A process can leave its process group with setsid, so the group kill alone is not enough."""
    if uid is None:
        return 0
    for _ in range(40):
        pids = _owned_pids(uid)
        if not pids:
            return 0
        for pid in pids:
            try:
                os.kill(pid, SIGKILL)
            except OSError:
                pass
        time.sleep(0.05)
    return len(_owned_pids(uid))


def _killpg(pgid: int) -> None:
    try:
        os.killpg(pgid, SIGKILL)
    except OSError:
        pass


def _remove(path: str) -> None:
    try:
        st = os.lstat(path)
    except OSError:
        return
    if stat.S_ISDIR(st.st_mode):
        shutil.rmtree(path, ignore_errors=True)
    else:
        try:
            os.unlink(path)
        except OSError:
            pass


TEMP_DIRS = ("/tmp", "/var/tmp", "/dev/shm", "/run/lock")


def _owned_tops(uid, skip: str = "") -> list[str]:
    """Top-level entries of the machine's shared temp folders that the sandbox user made."""
    out = []
    for d in TEMP_DIRS:
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        for e in entries:
            try:
                if e.stat(follow_symlinks=False).st_uid == uid and e.path != skip:
                    out.append(e.path)
            except OSError:
                continue
    return out


def _usage(roots: list, limit: int) -> tuple[int, int]:
    """(bytes, entries) under these paths, without following a link; stops counting past `limit`."""
    total, n, stack = 0, 0, list(roots)
    while stack and n <= limit:
        p = stack.pop()
        try:
            st = os.lstat(p)
        except OSError:
            continue
        if stat.S_ISREG(st.st_mode):
            total += st.st_size
            continue
        if not stat.S_ISDIR(st.st_mode):
            continue
        try:
            entries = list(os.scandir(p))
        except OSError:
            continue
        for e in entries:
            n += 1
            try:
                est = e.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISDIR(est.st_mode):
                stack.append(e.path)
            elif stat.S_ISREG(est.st_mode):
                total += est.st_size
    return total, n


def _ensure_base() -> Path:
    base = base_dir()
    try:
        os.mkdir(str(base), 0o711)
    except FileExistsError:
        pass
    st = os.lstat(str(base))
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o022:
        raise Refused("the sandbox folder %s is not the brain's own" % base)
    os.chmod(str(base), 0o711)
    return base


class LinuxExecutor(Executor):
    """The real one: unshare, setpriv and rlimits on the brain's Linux machine."""
    real = True

    def __init__(self, host=None):
        self.host = host or Host()
        self.info: dict = {}

    def ready(self):
        ok, why, info = preflight(self.host)
        self.info = info
        return ok, why

    def ids(self):
        return self.info["uid"], self.info["gid"]

    def tools(self):
        return self.info["unshare"], self.info["setpriv"]

    @contextlib.contextmanager
    def lock(self, wait_s: float):
        """One run at a time per machine, across processes (the brain and a --check over ssh): each
        run ends by killing every process the sandbox user owns, which would kill the other's."""
        import fcntl
        base = _ensure_base()
        fd = os.open(str(base / ".lock"), os.O_RDWR | os.O_CREAT | _NOFOLLOW, 0o600)
        try:
            deadline = time.monotonic() + wait_s
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() > deadline:
                        raise Busy() from None
                    time.sleep(0.5)
            yield
        finally:
            os.close(fd)

    def before(self) -> None:
        """Start clean: no process left from a run a restart cut short, and no old run folder."""
        _sweep(self.info["uid"])
        for e in list(os.scandir(str(base_dir()))):
            if e.name != ".lock":
                _remove(e.path)

    def run(self, job: Job) -> Outcome:
        import resource
        pairs = [(getattr(resource, name), soft, hard) for name, soft, hard in job.limits.rlimits()]
        setrlimit, nice = resource.setrlimit, os.nice
        oom = "/proc/self/oom_score_adj"

        def limit():
            # Runs in the child between fork and exec. No import, no logging and no lock in here: the
            # brain is threaded, and only plain system calls are safe in a forked copy of it.
            for res, soft, hard in pairs:
                setrlimit(res, (soft, hard))
            try:
                nice(10)
            except OSError:
                pass
            try:
                fd = os.open(oom, os.O_WRONLY)
                os.write(fd, b"1000")
                os.close(fd)
            except OSError:
                pass

        start = time.monotonic()
        killed, peak = "", 0
        log_cap = int(_num("AGENTS_SANDBOX_LOG_MB", 5) * MB)
        with tempfile.TemporaryFile(dir=job.private) as fo, tempfile.TemporaryFile(dir=job.private) as fe:
            try:
                proc = subprocess.Popen(job.argv, cwd=job.cwd, env=job.env, stdin=subprocess.DEVNULL, stdout=fo,
                                        stderr=fe, close_fds=True, start_new_session=True, preexec_fn=limit)
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                return Outcome(error="the code could not be started (%s)" % str(exc)[:200])
            deadline, watch = start + job.limits.wall_s, 0.0
            while True:
                # Waited on WITHOUT reaping, so the finished leader keeps its pid and its group id
                # until the group has been killed: a reused id could otherwise point at a stranger.
                if os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None:
                    break
                now = time.monotonic()
                if now >= deadline:
                    killed = "wall"
                    break
                if now >= watch:
                    watch = now + 1.0
                    peak = max(peak, len(_owned_pids(job.uid)))
                    size, entries = _usage([job.work] + _owned_tops(job.uid, str(base_dir())), job.limits.made_files)
                    if size > job.limits.work_mb * MB:
                        killed = "size"
                        break
                    if entries > job.limits.made_files:
                        killed = "files"
                        break
                time.sleep(0.2)
            _killpg(proc.pid)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
            left = _sweep(job.uid)
            seconds = time.monotonic() - start
            totals = []
            for f in (fo, fe):
                f.seek(0, 2)
                totals.append(f.tell())
                f.seek(0)
            out, err = fo.read(log_cap), fe.read(log_cap)
        return Outcome(returncode=proc.returncode, killed_for=killed, seconds=seconds, stdout=out, stderr=err,
                       max_procs=peak, left_running=left, stdout_total=totals[0], stderr_total=totals[1])

    def after(self, run_dir: Path) -> None:
        uid = self.info["uid"]
        _sweep(uid)
        _remove(str(run_dir))
        for p in _owned_tops(uid):
            _remove(p)


def _executor():
    return EXECUTOR if EXECUTOR is not None else LinuxExecutor()


# ------------------------------------------------------------------------------------- one run
_RUN_LOCK = threading.Lock()
_BUSY = "another code run is still going on this machine; try again in a few minutes"


def _write_new(path: Path, data: bytes, mode: int = 0o644) -> None:
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _BIN, mode)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
    finally:
        os.close(fd)


def _copy_fd(fd: int, dst: Path, size: int) -> int:
    out = os.open(str(dst), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _NOFOLLOW | _BIN, 0o644)
    n = 0
    try:
        while n < size:
            chunk = os.read(fd, min(MB, size - n))
            if not chunk:
                break
            view = memoryview(chunk)
            while view:
                view = view[os.write(out, view):]
            n += len(chunk)
    finally:
        os.close(out)
    return n


def _copy_file(src: Path, dst: Path) -> int:
    try:
        fd = os.open(str(src), os.O_RDONLY | _NOFOLLOW | _BIN)
    except OSError as exc:
        raise Refused("%s could not be opened (%s)." % (src.name, exc.strerror or exc)) from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise Refused("%s is not a plain file." % src.name)
        return _copy_fd(fd, dst, st.st_size)
    finally:
        os.close(fd)


def _new_run_dir(label: str) -> Path:
    base = base_dir()
    base.mkdir(mode=0o711, parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", label)[:60] or "run"
    for i in range(200):
        p = base / (safe if i == 0 else "%s_%d" % (safe, i))
        try:
            os.mkdir(str(p), 0o711)
            return p
        except FileExistsError:
            continue
    raise Refused("The sandbox could not make a folder for this run.")


def _stage(run_dir: Path, code: str, inputs: list, owner) -> tuple[Path, Path]:
    """Write the bootstrap and the code (root's, read-only to the child), copy the inputs into in/
    (root's too, so the code cannot change what it was given), and only THEN hand work/ and out/ to
    the sandbox user, so nothing it could do can touch the staging."""
    _write_new(run_dir / "boot.py", BOOT.encode("utf-8"))
    _write_new(run_dir / "code.py", code.encode("utf-8"))
    work = run_dir / "work"
    inbox, out = work / "in", work / "out"
    os.mkdir(str(work), 0o700)
    os.mkdir(str(inbox), 0o755)
    os.mkdir(str(out), 0o700)
    for name, src in inputs:
        _copy_file(Path(src), inbox / name)
    if owner:
        uid, gid = owner
        os.chmod(str(run_dir), 0o711)
        os.chmod(str(inbox), 0o755)
        os.chown(str(work), uid, gid)
        os.chown(str(out), uid, gid)
        os.chmod(str(work), 0o700)
        os.chmod(str(out), 0o700)
    return work, out


def _safe_name(name: str, keep: int = 80) -> str:
    """A file name a signed link can carry as it is: letters, digits, dot, dash and underscore."""
    stem, ext = os.path.splitext(os.path.basename(str(name)))
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._-")[:keep] or "file"
    return stem + re.sub(r"[^A-Za-z0-9.]", "", ext.lower())[:10]


def _unique(name: str, taken: set) -> str:
    stem, ext = os.path.splitext(name)
    cand, i = name, 2
    while cand.lower() in taken:
        cand = "%s-%d%s" % (stem, i, ext)
        i += 1
    taken.add(cand.lower())
    return cand


def collect(out_dir, dest, uid=None, max_files: int = 20, max_mb: float = 50.0) -> tuple[list, list]:
    """The files the code left at the top of out/, copied into `dest` (a run folder's files/). Each is
    opened WITHOUT following a link and taken only when it is a regular file the sandbox user owns,
    with one name. The brain copies as root, so a link the code planted (at /data/agents.db, at
    /proc/1/environ) would otherwise be read with root's rights and served in Slack. A hard link fails
    the same test: the code cannot make one to a file it does not own, and one to its own file is a
    second name for something already kept."""
    files, skipped, total, cap = [], [], 0, int(max_mb * MB)
    try:
        entries = sorted(os.scandir(str(out_dir)), key=lambda e: e.name)
    except OSError:
        return [], [{"name": "out/", "why": "the code removed or replaced out/, so nothing was copied"}]
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    taken: set = set()
    for e in entries:
        ext = os.path.splitext(e.name)[1].lower()
        why = ""
        if e.is_symlink():
            why = "a link; only files the code wrote are kept"
        elif e.is_dir(follow_symlinks=False):
            why = "a folder; only files at the top of out/ are kept"
        elif not e.is_file(follow_symlinks=False):
            why = "not a regular file"
        elif ext not in KINDS:
            why = "%s files are not kept (kept: %s)" % (ext or "extensionless", " ".join(KINDS))
        elif len(files) >= max_files:
            why = "past the %d files kept from one run" % max_files
        if why:
            skipped.append({"name": e.name, "why": why})
            continue
        try:
            fd = os.open(e.path, os.O_RDONLY | _NOFOLLOW | _NONBLOCK | _BIN)
        except OSError as exc:
            skipped.append({"name": e.name, "why": "it could not be opened (%s)" % (exc.strerror or exc)})
            continue
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                why = "not a regular file"
            elif uid is not None and st.st_uid != uid:
                why = "not written by the code"
            elif st.st_nlink != 1:
                why = "a hard link to another file"
            elif total + st.st_size > cap:
                why = "past the %g MB kept from one run" % max_mb
            if why:
                skipped.append({"name": e.name, "why": why})
                continue
            name = _unique(_safe_name(e.name), taken)
            n = _copy_fd(fd, dest / name, st.st_size)
        finally:
            os.close(fd)
        total += n
        row = {"name": name, "bytes": n}
        if name != e.name:
            row["was"] = e.name
        files.append(row)
    return files, skipped


def _strip_mark(err: str) -> tuple[bool, str]:
    i = err.find(MARK, 0, 4096)
    if i < 0:
        return False, err
    j = err.find("\n", i)
    return True, err[:i] + (err[j + 1:] if j >= 0 else "")


def _ended(oc: Outcome, limits: Limits) -> str:
    if oc.killed_for == "wall":
        return "stopped at the %d s wall clock" % limits.wall_s
    if oc.killed_for == "size":
        return "stopped: it wrote more than %d MB" % limits.work_mb
    if oc.killed_for == "files":
        return "stopped: it made more than %d files" % limits.made_files
    rc = oc.returncode
    if rc is None:
        return "stopped"
    if rc >= 0:
        return "finished" if rc == 0 else "finished with exit code %d" % rc
    sig = -rc
    if sig == SIGXCPU:
        return "stopped: it used its %d s of CPU" % limits.cpu_s
    if sig == SIGXFSZ:
        return "stopped: a file passed the %d MB limit" % limits.file_mb
    if sig == SIGKILL:
        return "stopped by the system (the CPU limit, or the machine running out of memory)"
    return "stopped by signal %d" % sig


def _why_not(err: str, oc: Outcome, ended: str) -> str:
    lines = [ln.strip() for ln in err.splitlines() if ln.strip()]
    for ln in lines:
        if ln.startswith("sandbox refused to start:"):
            return "its own check inside the sandbox failed (%s)" % ln.split(":", 1)[1].strip()
    if lines:
        return "the isolation step failed (%s)" % lines[0][:200]
    if oc.killed_for:
        return "it did not start before it was %s" % ended
    return "the isolation step ended (exit code %s) before the code started" % oc.returncode


def _finish(oc: Outcome, out_dir: Path, dest, limits: Limits, uid) -> Ran:
    if oc.error:
        return Ran(started=False, why=oc.error)
    err = oc.stderr.decode("utf-8", "replace") if isinstance(oc.stderr, bytes) else str(oc.stderr or "")
    out = oc.stdout.decode("utf-8", "replace") if isinstance(oc.stdout, bytes) else str(oc.stdout or "")
    started, err = _strip_mark(err)
    ended = _ended(oc, limits)
    if not started:
        return Ran(started=False, why=_why_not(err, oc, ended), returncode=oc.returncode, killed_for=oc.killed_for)
    files, skipped = [], []
    if oc.left_running:
        skipped.append({"name": "out/", "why": "%d process(es) the code started could not be stopped, so nothing was "
                                               "copied out" % oc.left_running})
    elif dest is not None:
        files, skipped = collect(out_dir, dest, uid=uid, max_files=int(_num("AGENTS_SANDBOX_OUT_FILES", 20)),
                                 max_mb=_num("AGENTS_SANDBOX_OUT_MB", 50))
    cut = [name for name, got, total in (("stdout", oc.stdout, oc.stdout_total), ("stderr", oc.stderr, oc.stderr_total))
           if total > len(got or b"")]
    return Ran(started=True, returncode=oc.returncode, killed_for=oc.killed_for, ended=ended, seconds=oc.seconds,
               stdout=out, stderr=err, files=files, skipped=skipped, max_procs=oc.max_procs,
               left_running=oc.left_running, cut=" and ".join(cut))


def execute(code: str, inputs: list, dest, limits: Limits, label: str, executor=None) -> Ran:
    """Run `code` once, fully isolated. `inputs` is [(name in in/, source path)]; files the code
    writes to out/ are copied to `dest` (None: nothing is kept). Never runs with less isolation: a
    step that cannot be applied returns Ran(started=False, why=...)."""
    ex = executor or _executor()
    ok, why = ex.ready()
    if not ok:
        return Ran(started=False, why=why)
    wait = limits.wall_s + 60
    if not _RUN_LOCK.acquire(timeout=wait):
        return Ran(started=False, why=_BUSY)
    try:
        with ex.lock(wait):
            ex.before()
            run_dir = _new_run_dir(label)
            try:
                uid, gid = ex.ids()
                work, out_dir = _stage(run_dir, code, inputs, (uid, gid) if ex.real else None)
                unshare, setpriv = ex.tools()
                job = Job(argv=command(uid, gid, run_dir / "boot.py", run_dir / "code.py", unshare=unshare,
                                       setpriv=setpriv),
                          env=child_env(work), cwd=str(work), work=str(work), private=str(run_dir), limits=limits,
                          uid=uid if ex.real else None)
                return _finish(ex.run(job), out_dir, dest, limits, uid if ex.real else None)
            finally:
                ex.after(run_dir)
    except Busy:
        return Ran(started=False, why=_BUSY)
    except Refused as exc:
        return Ran(started=False, why=str(exc).rstrip("."))
    except OSError as exc:
        # A folder or a file of the run that could not be made or opened: the run did not happen, and
        # it must read that way, never as a crash of the employee's turn.
        return Ran(started=False, why="the sandbox could not prepare the run (%s)" % (exc.strerror or exc))
    finally:
        _RUN_LOCK.release()


# ------------------------------------------------------------------------------------- run_python
def run_dir_for(task: dict) -> Path:
    """A turn's run folder, by the convention every workflow uses: <workflow>-<task id>."""
    return Path(config.RUNS_DIR) / ("%s-%s" % (task.get("workflow") or "ask", task.get("id") or 0))


def turn_result(task: dict) -> dict:
    """{"run_dir": ...} when this turn's code left files, for the worker to store on the turn's result.
    The brain's /files route finds a file through that one key, so without it every link the sandbox
    handed out would answer 404."""
    files = run_dir_for(task) / "files"
    try:
        if files.is_dir() and any(p.is_file() for p in files.iterdir()):
            return {"run_dir": str(run_dir_for(task))}
    except OSError:
        pass
    return {}


def _inside(root, p) -> bool:
    try:
        return Path(p).resolve().is_relative_to(Path(root).resolve())
    except (OSError, ValueError, RuntimeError):
        return False


def _run_file(p) -> bool:
    """A file a run folder shares: <runs>/<run>/(files|packs|exports)/<name>, nothing deeper."""
    try:
        parts = Path(p).resolve().relative_to(Path(config.RUNS_DIR).resolve()).parts
    except (OSError, ValueError, RuntimeError):
        return False
    return len(parts) == 3 and parts[1] in ("files", "packs", "exports")


def _checked(p) -> Path:
    try:
        real = Path(p).resolve(strict=True)
    except (OSError, RuntimeError):
        raise Refused("%s could not be opened." % Path(p).name) from None
    if not real.is_file():
        raise Refused("%s is not a plain file." % Path(p).name)
    return real


def _thread_dirs(task: dict) -> list[Path]:
    """This turn's run folder first, then those of the earlier tasks on its thread, newest first, so an
    export made before an ask-back is still found after the operator's answer resumes the work."""
    from . import store
    dirs = [run_dir_for(task)]
    tid = task.get("thread_id") or ""
    if tid:
        try:
            rows = store.thread(tid)
        except Exception:  # noqa: BLE001  (a thread that will not read just means fewer places to look)
            rows = []
        for t in sorted(rows, key=lambda r: -int(r.get("id") or 0))[:30]:
            if t.get("id") != task.get("id"):
                dirs.append(run_dir_for(t))
    return dirs


_LINK_RE = re.compile(r"/files/(\d+)/[0-9a-fA-F]{8,64}/([^/?#\s]+)$")
_TASK_RE = re.compile(r"task\s*[:#]?\s*(\d+)\s*/\s*([^/\\]+)", re.I)


def _task_file(task_id: int, name: str) -> Path:
    from . import store
    name = name.strip()
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        raise Refused("A task's file is named by its file name alone.")
    t = store.task(task_id)
    if not t:
        raise Refused("There is no task %d." % task_id)
    rd = str(store.result_of(t).get("run_dir") or "")
    cands = ([Path(rd) / "packs" / name, Path(rd) / "files" / name] if rd else []) + [
        run_dir_for(t) / "files" / name, run_dir_for(t) / "exports" / name]
    for c in cands:
        if c.is_file() and _inside(config.RUNS_DIR, c):
            return _checked(c)
    raise Refused("Task %d has no file named %r." % (task_id, name))


def resolve_input(given: str, task: dict, employee_id: str) -> Path:
    """The real path of one file the employee gives the code, or Refused. Allowed: a file in its OWN
    workspace (the Slack files it was handed under inbox/), an export or an earlier run's output on
    this thread, and a file a task shared (task:<id>/<name>, or the signed link itself). Nothing
    else: another employee's workspace, the queue, the log copy and the Claude sessions are not
    inputs, and a path that climbs out with .. or a link that leads out is refused, not followed."""
    s = str(given or "").strip()
    if not s:
        raise Refused("One of the files is an empty name.")
    m = _LINK_RE.search(s) or _TASK_RE.fullmatch(s)
    if m:
        return _task_file(int(m.group(1)), m.group(2))
    norm = s.replace("\\", "/")
    parts = [x for x in norm.split("/") if x not in ("", ".")]
    if ".." in parts:
        raise Refused("%r climbs out of its folder; name a file in your workspace, an export, or a run's files." % s)
    if not parts:
        raise Refused("%r is not a file name." % s)
    ws = Path(config.WORKSPACES_DIR) / employee_id
    if norm.startswith("/") or re.match(r"^[A-Za-z]:", norm):
        p = Path(s)
        if p.is_file() and (_inside(ws, p) or _run_file(p)):
            return _checked(p)
        raise Refused("%r is outside the files you may give the code (your workspace, this thread's exports and run "
                      "files, or a task's files)." % s)
    cands: list[Path] = []
    for d in _thread_dirs(task):
        if len(parts) == 2 and parts[0] in ("exports", "files"):
            cands.append(d / parts[0] / parts[1])
        elif len(parts) == 1:
            cands += [d / "exports" / parts[0], d / "files" / parts[0]]
    cands.append(ws.joinpath(*parts))
    for c in cands:
        if c.is_file():
            if _inside(ws, c) or _run_file(c):
                return _checked(c)
            raise Refused("%r leads outside the files you may give the code." % s)
    raise Refused("There is no file %r in your workspace, this thread's exports or its run files." % s)


def stage_inputs(task: dict, employee_id: str, files) -> list[dict]:
    if files in (None, "", []):
        return []
    if isinstance(files, str):
        files = [files]
    if not isinstance(files, (list, tuple)):
        raise Refused("files is a list of file names.")
    max_n, max_b = int(_num("AGENTS_SANDBOX_IN_FILES", 20)), _num("AGENTS_SANDBOX_IN_MB", 100) * MB
    if len(files) > max_n:
        raise Refused("At most %d files go into one run; this asked for %d." % (max_n, len(files)))
    out, names, total = [], set(), 0
    for given in files:
        p = resolve_input(str(given), task, employee_id)
        size = p.stat().st_size
        total += size
        if total > max_b:
            raise Refused("The files come to more than the %g MB one run takes." % (max_b / MB))
        out.append({"given": str(given), "as": _unique(_safe_name(p.name), names), "path": p, "bytes": size})
    return out


_RUNS: dict = {}
_RUNS_LOCK = threading.Lock()


def _run_number(task_id: int, files_dir: Path) -> int:
    with _RUNS_LOCK:
        if task_id not in _RUNS:
            seen = []
            if files_dir.is_dir():
                for p in files_dir.iterdir():
                    m = re.match(r"run-(\d+)-output\.txt$", p.name)
                    if m:
                        seen.append(int(m.group(1)))
            _RUNS[task_id] = max(seen, default=0)
        _RUNS[task_id] += 1
        return _RUNS[task_id]


def _cut(text: str, room: int, note: str, head_share: float) -> str:
    if len(text) <= room:
        return text
    marker = "\n[... %d characters cut; %s ...]\n" % (len(text) - room, note)
    keep = max(0, room - len(marker))
    head = int(keep * head_share)
    return text[:head] + marker + (text[-(keep - head):] if keep - head > 0 else "")


def clip(out: str, err: str, total: int, log_name: str) -> tuple[str, str, bool]:
    """At most `total` characters of output for the model. stderr keeps its end (a traceback ends
    there), stdout keeps its start and its end; the whole of both is in the run's output file."""
    if len(out) + len(err) <= total:
        return out, err, False
    err_room = min(len(err), max(total // 3, total - len(out)))
    note = "the whole output is in %s" % log_name
    return _cut(out, total - err_room, note, 0.6), _cut(err, err_room, note, 0.25), True


def run_code(task: dict, employee_id: str, code, files=None, timeout_s=None) -> dict:
    """One run_python call: resolve and stage the files, run the code isolated, keep what it wrote and
    its whole output in the turn's run folder, and answer with links. Raises Refused."""
    text = code if isinstance(code, str) else ""
    if not text.strip():
        raise Refused("Give run_python the code to run.")
    max_code = int(_num("AGENTS_SANDBOX_CODE_KB", 200)) * 1024
    if len(text.encode("utf-8")) > max_code:
        raise Refused("The code is longer than %d KB; write a shorter script." % (max_code // 1024))
    lim = Limits.from_env()
    if timeout_s not in (None, ""):
        try:
            t = float(timeout_s)
        except (TypeError, ValueError):
            raise Refused("timeout_s is a number of seconds.") from None
        if not math.isfinite(t) or t <= 0:
            raise Refused("timeout_s is a number of seconds above 0.")
        lim.wall_s = min(lim.wall_s, t)
    inputs = stage_inputs(task, employee_id, files)
    dest = run_dir_for(task) / "files"
    n = _run_number(int(task.get("id") or 0), dest)
    ran = execute(text, [(i["as"], i["path"]) for i in inputs], dest, lim, "%s-%d" % (task.get("id") or 0, n))
    if not ran.started:
        log.warning("sandbox not available for task %s: %s", task.get("id"), ran.why)
        raise Refused("The code sandbox is not available on this machine: %s. Nothing ran." % ran.why.rstrip("."))
    from . import slack
    dest.mkdir(parents=True, exist_ok=True)
    log_name = "run-%d-output.txt" % n
    # The volume is small (1 GB holds the queue, the sessions and every run folder), so the log keeps the
    # first AGENTS_SANDBOX_LOG_MB of each stream and says so when there was more.
    head = ("The code wrote more %s than this file keeps (the first %g MB of each).\n\n" % (
        ran.cut, _num("AGENTS_SANDBOX_LOG_MB", 5))) if ran.cut else ""
    (dest / log_name).write_text("%sstdout\n------\n%s\n\nstderr\n------\n%s\n" % (head, ran.stdout, ran.stderr),
                                 encoding="utf-8")
    chars = int(_num("AGENTS_SANDBOX_OUTPUT_CHARS", 20000, 200))
    out, err, clipped = clip(ran.stdout, ran.stderr, chars, log_name)
    tid = int(task.get("id") or 0)
    notes = []
    if clipped:
        notes.append("The output was cut to %d characters; the whole of it is in %s." % (chars, log_name))
    if ran.files:
        notes.append("Share the links in your reply; they open once this turn ends.")
    if not config.PUBLIC_URL:
        notes.append("This install has no public address (AGENTS_PUBLIC_URL), so the files have no links.")
    return {"ran": True, "run": n, "ended": ran.ended, "exit_code": ran.exit_code, "seconds": round(ran.seconds, 1),
            "stdout": out, "stderr": err,
            "inputs": [{"given": i["given"], "as": "in/" + i["as"], "bytes": i["bytes"]} for i in inputs],
            "files": [dict(f, link=slack.file_url(tid, f["name"])) for f in ran.files],
            "skipped": ran.skipped,
            "output_log": {"name": log_name, "link": slack.file_url(tid, log_name)},
            "limits": lim.words(), "notes": notes}


# ------------------------------------------------------------------------------------- export_data
SOURCES = ("records", "log", "tasks", "stage_events", "batches")
# The records_query inputs this export builds a filter from, one by one. An input query.py gains later
# is refused here until it is handled, never dropped: dropping it would export a wider set than asked.
RECORD_FILTERS = frozenset({"preset", "lists", "all_lists", "tags", "all_tags", "statuses", "status_group", "not_lists",
                            "not_tags", "not_statuses", "not_status_group", "created_from", "created_to", "sold_from",
                            "sold_to", "assigned", "owner_of", "has_phone", "skip_traced", "call_attempts",
                            "text_attempts", "include_incomplete"})
SOURCE_INPUTS = {"records": RECORD_FILTERS | {"max_rows", "with_titles"},
                 "log": frozenset({"what", "since", "until"}),
                 "tasks": frozenset({"since", "status", "workflow"}),
                 "stage_events": frozenset({"since", "stage", "action"}),
                 "batches": frozenset({"since", "status", "stage"})}
RECORD_COLUMNS = ["uuid", "street", "city", "state", "zip", "status", "owner", "owner_is_company", "lists", "tags",
                  "list_count", "tag_count", "created", "has_phones", "skiptraced", "last_skip_traced", "mail_attempts",
                  "do_not_mail", "vacant", "realtor_score", "on_market_score"]
LOG_WHAT = ("events", "calls", "texts", "status_changes", "assignments", "tags", "lists", "created", "board",
            "phone_status")
LOG_FIELDS = ("direction", "duration", "status", "caller", "from", "to", "tags", "lists", "by", "board", "column",
              "column_from", "title", "kind", "due")
LOG_COLUMNS = ["id", "at", "day", "event_type", "resource", "resource_type", "source", "author"] + list(LOG_FIELDS)
TABLE_COLUMNS = {
    "tasks": ["id", "created", "started", "finished", "agent", "workflow", "status", "attempts", "hop", "mode",
              "source", "thread_id", "parent_id", "record_uuid", "subject", "question", "error", "usd"],
    "stage_events": ["id", "at", "day", "run_id", "stage", "record_uuid", "action", "usd", "detail"],
    "batches": ["id", "created", "stage", "kind", "status", "items", "usd", "released"],
}
# Keys under which a number, an address for mail or a recording can sit inside a stored detail. Their
# values never leave: a list becomes its length, anything else "[removed]".
SENSITIVE = frozenset({"number", "numbers", "phone", "phones", "mobile", "landline", "line", "email", "emails",
                       "recording", "destination_number", "origin_number", "to_number", "from_number"})


def _scrub(text) -> str:
    """Free text with anything shaped like a phone number or an email replaced. Run only on free text,
    never on an id: a uuid or a Slack stamp can hold ten digits in a row."""
    return _PHONE.sub("[phone]", _EMAIL.sub("[email]", str(text)))


def _scrub_value(v):
    if isinstance(v, (list, tuple)):
        return [_scrub_value(x) for x in v]
    if isinstance(v, str):
        return v if _UUIDISH.match(v) else _scrub(v)
    return v


def _clean(obj, depth: int = 0):
    """A stored detail (JSON) with every sensitive key emptied and the free text scrubbed."""
    if depth > 8:
        return "[nested]"
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if str(k).lower() in SENSITIVE:
                if isinstance(v, (list, tuple, set, dict)):
                    out[k] = len(v)
                elif isinstance(v, bool) or (isinstance(v, int) and len(str(abs(v))) < 7):
                    out[k] = v
                else:
                    out[k] = "[removed]"
            else:
                out[k] = _clean(v, depth + 1)
        return out
    if isinstance(obj, (list, tuple)):
        return [_clean(v, depth + 1) for v in list(obj)[:500]]
    if isinstance(obj, str):
        return obj if _UUIDISH.match(obj) else _scrub(obj)
    return obj


def _cell(v) -> str:
    """One CSV cell. A text that starts with = or @ gets a leading apostrophe: code that copies it
    into a workbook would otherwise write a live formula (a link that phones home when opened)."""
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        v = "; ".join(str(x) for x in v)
    s = str(v)
    return "'" + s if s[:1] in ("=", "@") else s


def _write_csv(path: Path, columns: list, rows: list) -> None:
    with open(str(path), "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(columns)
        for r in rows:
            w.writerow([_cell(r.get(c)) for c in columns])


def _safe_stem(name: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", os.path.splitext(os.path.basename(str(name)))[0]).strip("._-")[:60]
    return stem or "export"


def _title(v) -> str:
    if isinstance(v, dict):
        return str(v.get("title") or v.get("name") or v.get("label") or "")
    return str(v or "")


def _export_reads(q, crm, cap: int):
    """query.py's counted record client (a search and a GET, nothing that writes), with paging."""
    class Reads(q._Reads):
        def page(self, must, limit, offset):
            self._take()
            return self._crm.search(must, limit=int(limit), offset=int(offset))
    return Reads(crm, cap=cap)


def _records_must(q, a: dict, reads, today) -> tuple:
    """The records search filter for these inputs, built exactly the way query.records_query builds
    its own (the same helpers, in the same order), so an export and a count never disagree. Returns
    (must, record types, applied, limits); must is None when no record can match."""
    from datetime import timedelta
    applied: dict = {}
    ask: dict = {}
    neg: dict = {}
    limits: list = []
    for fld, key, floor in (("created", "created", q.CREATED_FLOOR), ("sold", "last_sold", q.SOLD_FLOOR)):
        lo, hi = a.get(fld + "_from"), a.get(fld + "_to")
        if lo is None and hi is None:
            continue
        d_lo = q._date(lo, fld + "_from", today) if lo is not None else None
        d_hi = q._date(hi, fld + "_to", today) if hi is not None else today
        if d_lo and d_lo > d_hi:
            raise q.Refused("%s_from is after %s_to." % (fld, fld))
        ask[key] = [d_lo.isoformat() if d_lo else floor, (d_hi + timedelta(days=1)).isoformat()]
        applied[key] = [d_lo.isoformat() if d_lo else "any", d_hi.isoformat()]
    for fld, key in (("call_attempts", "predictivecall_attempts"), ("text_attempts", "sms_attempts")):
        if fld in a:
            ask[key] = q._counter(a[fld], fld)
            applied[fld] = a[fld]
    for fld, key in (("has_phone", "phone"), ("skip_traced", "skiptraced")):
        if fld in a:
            ask[key] = 1 if a[fld] else 0
            applied[fld] = a[fld]
    if "assigned" in a:
        uid, who = q._assignee(a["assigned"])
        ask["assigned_to"] = uid
        applied["assigned"] = who
    for fld, key, find in (("lists", "any_lists", q._list_id), ("all_lists", "all_lists", q._list_id),
                           ("tags", "any_tags", q._tag_id), ("all_tags", "all_tags", q._tag_id),
                           ("not_lists", "any_lists", q._list_id), ("not_tags", "any_tags", q._tag_id)):
        if fld not in a:
            continue
        found = [find(reads, t) for t in a[fld]]
        (neg if fld.startswith("not_") else ask)[key] = [u for _, u in found]
        applied[fld] = [t for t, _ in found]
    fam = q.families()
    for fld, group, target in (("statuses", "status_group", ask), ("not_statuses", "not_status_group", neg)):
        vals = [q._status_value(reads, w) for w in a.get(fld) or []] + list(fam.get(a.get(group) or "", []))
        if vals:
            target["any_property_status"] = list(dict.fromkeys(vals))
        if fld in a:
            applied[fld] = [q.status_word(v) for v in vals[:len(a[fld])]]
        if group in a:
            applied[group] = a[group]
    if a.get("owner_of"):
        u = a["owner_of"]
        if not q.UUID_RE.match(u):
            raise q.Refused("owner_of takes a record uuid; records_query rows and record_lookup show it.")
        try:
            rec = reads.get("/api/internal/property/%s/" % u) or {}
        except q.Refused:
            raise
        except Exception as exc:  # noqa: BLE001  (a 404 is a plain answer, anything else is reported)
            if getattr(exc, "code", None) == 404:
                raise q.Refused("No record in this account has uuid %s." % u) from None
            raise
        owner = (rec.get("owner") or {}).get("uuid") or ""
        if not owner:
            raise q.Refused("Record %s has no owner on it." % u)
        ask["owners"] = [owner]
        applied["owner_of"] = (rec.get("address") or {}).get("street") or u
    base, title = {}, ""
    if a.get("preset"):
        title, base, notes = q._preset(reads, a["preset"], today)
        applied["preset"] = title
        limits += notes
    try:
        must = q._merge(base, ask, neg, title)
    except q._Nothing as nothing:
        limits.append("The preset %s already limits %s in a way no record can also meet as asked, so the export is "
                      "empty with no search." % (title, q.KEY_WORDS.get(str(nothing), str(nothing))))
        return None, [], applied, limits
    types = ["clean", "incomplete"] if a.get("include_incomplete") else ["clean"]
    if "property_type" in must:
        types = [None]
        if a.get("include_incomplete"):
            limits.append("The preset sets its own record type, so include_incomplete was not applied.")
    return must, types, applied, limits


def _page(reads, must: dict, types: list, max_rows: int) -> tuple[list, int]:
    rows: list = []
    total = 0
    for ptype in types:
        m = dict(must) if ptype is None else dict(must, property_type=ptype)
        if len(rows) >= max_rows:
            n, _ = reads.page(m, 1, 0)          # the count still has to include this record type
            total += int(n or 0)
            continue
        off = 0
        while True:
            lim = min(250, max_rows - len(rows), WINDOW - off)
            if lim <= 0:
                break
            n, got = reads.page(m, lim, off)
            if off == 0:
                total += int(n or 0)
            got = [r for r in (got or []) if isinstance(r, dict)]
            rows += got
            if len(got) < lim:
                break
            off += lim
    return rows[:max_rows], total


def _created_days(q, uuids: list) -> tuple[dict, str]:
    """uuid -> the business-local day the record was added, from the brain's log copy (the records
    search and the record itself carry no such date), and the first day the copy holds."""
    from . import store
    found: dict = {}
    first = None
    with store._LOCK, store._db() as c:
        for i in range(0, len(uuids), 400):
            chunk = uuids[i:i + 400]
            sql = ("SELECT resource, MIN(at) AS at FROM ap_log WHERE event_type='property.created' "
                   "AND resource IN (%s) GROUP BY resource" % ",".join("?" * len(chunk)))
            for row in c.execute(sql, chunk).fetchall():
                found[row["resource"]] = row["at"]
        first = c.execute("SELECT MIN(at) AS at FROM ap_log WHERE event_type='property.created'").fetchone()
    days = {}
    for u, at in found.items():
        try:
            days[u] = q._day_of(datetime.fromisoformat(at))
        except (TypeError, ValueError):
            continue
    floor = ""
    if first is not None and first["at"]:
        try:
            floor = q._day_of(datetime.fromisoformat(first["at"]))
        except (TypeError, ValueError):
            floor = ""
    return days, floor


def _record_row(q, r: dict, created: str) -> dict:
    """One record as an export carries it. Built field by field: a search row also carries the owner's
    phone number, that number's do-not-call flag and, on some rows, emails, none of which may leave."""
    a = r.get("address") if isinstance(r.get("address"), dict) else {}
    o = r.get("owner") if isinstance(r.get("owner"), dict) else {}
    person = " ".join(str(x) for x in (o.get("first_name"), o.get("last_name")) if x)
    company = str(o.get("company") or "")
    return {"uuid": r.get("uuid") or "", "street": a.get("street") or "", "city": a.get("city") or "",
            "state": a.get("state") or "", "zip": str(a.get("zip5") or a.get("postal_code") or "")[:5],
            "status": q.status_word(r.get("status")), "owner": person or company,
            "owner_is_company": bool(company) and not person, "lists": r.get("_lists") or "",
            "tags": r.get("_tags") or "", "list_count": r.get("list_count"), "tag_count": r.get("tag_count"),
            "created": created, "has_phones": r.get("has_phones"), "skiptraced": r.get("skiptraced"),
            "last_skip_traced": r.get("last_skip_traced") or "", "mail_attempts": r.get("directmail_attempts"),
            "do_not_mail": r.get("do_not_mail_ever"), "vacant": a.get("vacant"),
            "realtor_score": r.get("realtor_score"), "on_market_score": r.get("on_market_score")}


def _records_export(q, a: dict) -> tuple[list, list, dict]:
    if not q.enabled():
        raise Refused("Records exports are off on this install (AGENTS_QUERY_TOOLS=0), so nothing was read.")
    cap = int(_num("AGENTS_EXPORT_MAX_ROWS", 2000))
    notes: list = []
    want = a.pop("max_rows", None)
    if want is None:
        max_rows = cap
    else:
        try:
            if isinstance(want, bool):
                raise ValueError(want)
            max_rows = int(float(str(want).strip()))
        except ValueError:
            raise Refused("max_rows is a whole number.") from None
        if max_rows < 1:
            raise Refused("max_rows is at least 1.")
        if max_rows > cap:
            notes.append("max_rows is at most %d on this install, so %d were asked for and at most %d are written."
                         % (cap, max_rows, cap))
            max_rows = cap
    titles = q._bool(a.pop("with_titles", False), "with_titles")
    clean = q.clean("records_query", a)
    unhandled = sorted(set(clean) - RECORD_FILTERS)
    if unhandled:
        raise Refused("export_data cannot filter on %s yet; records_query can count it." % ", ".join(unhandled))
    detail_max = int(_num("AGENTS_EXPORT_DETAIL_MAX", 100, 0)) if titles else 0
    today = q._today()
    pages = 2 * math.ceil(max_rows / 250.0) + 2
    reads = _export_reads(q, q._crm(), pages + 12 + detail_max)
    try:
        must, types, applied, limits = _records_must(q, clean, reads, today)
        notes += limits
        rows, total = ([], 0) if must is None else _page(reads, must, types, max_rows)
        titled, stopped = 0, False
        for r in rows[:detail_max]:
            u = r.get("uuid") or ""
            if not u:
                continue
            try:
                rec = reads.get("/api/internal/property/%s/" % u) or {}
            except q.Refused:
                stopped = True
                break
            except Exception as exc:  # noqa: BLE001  (a record deleted since the search is a blank, not a failure)
                if getattr(exc, "code", None) == 404:
                    continue
                raise
            r["_lists"] = [t for t in (_title(x) for x in rec.get("lists") or []) if t]
            r["_tags"] = [t for t in (_title(x) for x in rec.get("tags") or []) if t]
            titled += 1
    except q.Refused:
        raise
    except Exception as exc:  # noqa: BLE001  (every failure reaches the employee as one sentence)
        raise Refused(q._plain(exc)) from exc
    days, floor = _created_days(q, [r.get("uuid") for r in rows if r.get("uuid")])
    out = [_record_row(q, r, days.get(r.get("uuid") or "", "")) for r in rows]
    if total > len(out):
        notes.append("%d records match and %d are written." % (total, len(out)))
    if titles:
        notes.append("lists and tags are filled on the first %d rows (one request each)%s; list_count and tag_count "
                     "are on every row and include DataSift's own classification lists." % (
                         titled, ", where the request cap for one export stopped it" if stopped else ""))
    else:
        notes.append("lists and tags are blank unless with_titles is true; list_count and tag_count are on every row.")
    notes.append("created comes from the brain's copy of the account log%s, so a record added before that is blank."
                 % (", which reaches back to %s" % floor if floor else ", which holds no record additions yet"))
    notes.append("No phone number or email is ever exported; has_phones says whether the owner has a number.")
    return RECORD_COLUMNS, out, {"count": total, "applied": applied, "notes": notes, "requests": reads.n}


def _log_types(what: str) -> tuple:
    from .autopilot import activity
    return {"events": (), "calls": activity.CALLS, "texts": activity.TEXTS,
            "status_changes": ("property.status.updated",), "assignments": ("property.assigned",),
            "tags": ("property.tags.added", "property.tags.removed"), "lists": ("property.lists.added",),
            "created": ("property.created",), "board": activity.BOARD, "phone_status": activity.PHONE_STATUS}[what]


def _log_export(q, a: dict) -> tuple[list, list, dict]:
    from .autopilot import activity
    what = q._word(a.get("what") or "events")
    if what not in LOG_WHAT:
        raise Refused("what is one of: %s." % ", ".join(LOG_WHAT))
    today = q._today()
    since = q._date(a.get("since") or "7-days", "since", today)
    until = q._date(a.get("until") or "today", "until", today)
    if since > until:
        raise Refused("since is after until.")
    start, end = q._bounds(since, until)
    cap = int(_num("AGENTS_EXPORT_LOCAL_ROWS", 20000))
    evs = activity.events(_log_types(what), since=start, until=end, limit=cap + 1)
    rows = []
    for e in evs[:cap]:
        d = e.get("data") if isinstance(e.get("data"), dict) else {}
        row = {"id": e.get("id"), "at": e.get("at"), "day": q._day_of(e["when"]), "event_type": e.get("event_type"),
               "resource": e.get("resource"), "resource_type": e.get("resource_type"), "source": e.get("source"),
               "author": _scrub_value(e.get("author"))}
        for k in LOG_FIELDS:
            row[k] = _scrub_value(d.get(k))
        rows.append(row)
    notes = ["From the brain's own copy of the account log, with no DataSift request. Phone numbers, the line a call "
             "came from and call recordings are never exported."]
    if len(evs) > cap:
        notes.append("Only the first %d events are written; ask for a shorter range." % cap)
    return LOG_COLUMNS, rows, {"count": len(rows), "range": [since.isoformat(), until.isoformat()], "notes": notes}


def _table_export(q, source: str, a: dict) -> tuple[list, list, dict]:
    from . import store
    today = q._today()
    since = q._date(a.get("since") or "30-days", "since", today)
    start = q._bounds(since, since)[0].isoformat(timespec="seconds")
    cap = int(_num("AGENTS_EXPORT_LOCAL_ROWS", 20000))
    if source == "tasks":
        sql = ("SELECT t.id, t.created, t.started, t.finished, t.agent, t.workflow, t.status, t.attempts, t.hop, "
               "t.mode, t.source, t.thread_id, t.parent_id, t.record_uuid, t.subject, t.question, t.error, "
               "(SELECT COALESCE(SUM(s.usd), 0) FROM spend s WHERE s.task_id = t.id) AS usd FROM tasks t "
               "WHERE t.created >= ?")
        args: list = [start]
        for key, col in (("status", "t.status"), ("workflow", "t.workflow")):
            if a.get(key):
                sql, args = sql + " AND %s = ?" % col, args + [str(a[key]).strip()]
        sql += " ORDER BY t.id LIMIT ?"
    elif source == "stage_events":
        sql = "SELECT id, at, day, run_id, stage, record_uuid, action, usd, detail FROM ap_events WHERE day >= ?"
        args = [since.isoformat()]
        for key in ("stage", "action"):
            if a.get(key):
                sql, args = sql + " AND %s = ?" % key, args + [str(a[key]).strip()]
        sql += " ORDER BY id LIMIT ?"
    else:
        sql = "SELECT id, created, stage, kind, status, items, usd, released FROM ap_batches WHERE created >= ?"
        args = [start]
        for key in ("status", "stage"):
            if a.get(key):
                sql, args = sql + " AND %s = ?" % key, args + [str(a[key]).strip()]
        sql += " ORDER BY id LIMIT ?"
    args.append(cap + 1)
    with store._LOCK, store._db() as c:
        fetched = [dict(r) for r in c.execute(sql, args).fetchall()]
    rows = []
    for r in fetched[:cap]:
        if source == "tasks":
            for k in ("subject", "question", "error"):
                r[k] = _scrub(r.get(k) or "")[:500]
            r["usd"] = round(float(r.get("usd") or 0), 4)
        elif source == "stage_events":
            try:
                d = json.loads(r.get("detail") or "{}")
            except ValueError:
                d = {}
            r["detail"] = json.dumps(_clean(d), separators=(",", ":"), default=str)
        else:
            try:
                items = json.loads(r.pop("items", None) or "[]")
            except ValueError:
                items = []
            r["items"] = len(items) if isinstance(items, (list, dict)) else ""
        rows.append(r)
    notes = ["From the brain's own tables, with no DataSift request. A task's message and result are not exported, a "
             "stage event's numbers are counted, never listed, and a staged batch shows how many items it holds, never "
             "the items."]
    if len(fetched) > cap:
        notes.append("Only the first %d rows are written; ask for a later since." % cap)
    return TABLE_COLUMNS[source], rows, {"count": len(rows), "since": since.isoformat(), "notes": notes}


def export(task: dict, args: dict) -> dict:
    """One export_data call: write the CSV into this turn's run folder (exports/) and say how the code
    reads it. Raises Refused."""
    from . import query as q
    a = {k: v for k, v in dict(args or {}).items() if v not in (None, "", [])}
    source = str(a.pop("source", "") or "").strip().lower()
    if source not in SOURCES:
        raise Refused("export_data needs source, one of: %s." % ", ".join(SOURCES))
    name = _safe_stem(str(a.pop("name", "") or source))
    for k in [k for k, v in a.items() if v is False and k not in SOURCE_INPUTS[source]]:
        a.pop(k)
    extra = sorted(k for k in a if k not in SOURCE_INPUTS[source])
    if extra:
        # query.py already knows what to ask instead of most inputs a model reaches for (a ZIP, a city,
        # value, equity), and that sentence is worth more to the employee than a list of names.
        hint = q.HINTS.get("records_query", {}).get(str(extra[0]).strip().lower()) if source == "records" else ""
        if hint:
            raise Refused(hint)
        raise Refused("export_data with source=%s has no input %s; it takes: %s." % (
            source, ", ".join(extra), ", ".join(sorted(SOURCE_INPUTS[source]))))
    try:
        if source == "records":
            columns, rows, info = _records_export(q, a)
        elif source == "log":
            columns, rows, info = _log_export(q, a)
        else:
            columns, rows, info = _table_export(q, source, a)
    except q.Refused as exc:
        raise Refused(str(exc)) from None
    folder = run_dir_for(task) / "exports"
    folder.mkdir(parents=True, exist_ok=True)
    fname = name + ".csv"
    _write_csv(folder / fname, columns, rows)
    out = {"file": "exports/" + fname, "rows": len(rows), "columns": columns}
    out.update(info)
    out["use"] = 'Give run_python files=["exports/%s"]; the code reads it as in/%s.' % (fname, fname)
    return out


# ------------------------------------------------------------------------------------- the tools
def run_python_tool(ctx):
    from .tools import Tool, _obj, _text
    lim = Limits.from_env()
    desc = ("Run Python you write in a sealed sandbox on the brain's machine, the way you would in a terminal, to "
            "count, join, chart or build a spreadsheet: pandas, numpy, matplotlib and openpyxl are installed, the "
            "files you pass are in in/, and what the code writes to out/ (xlsx, csv, png, pdf, md, json, txt, docx) "
            "comes back as links to share in your reply. There is no network, no key and no brain data inside, and "
            "each run gets %s, so pull the data first with export_data." % lim.words())
    schema = _obj({
        "code": {"type": "string", "description": "The whole Python script: read inputs from in/, write results to "
                                                  "out/, and print a short summary"},
        "files": {"type": "array", "items": {"type": "string"},
                  "description": "Files to put in in/: an export (exports/name.csv), a file in your workspace "
                                 "(inbox/...), or a task's file (task:123/name.xlsx)"},
        "timeout_s": {"type": "number",
                      "description": "Stop the run after this many seconds (at most %d)" % lim.wall_s},
    }, ["code"])

    async def handler(args: dict) -> dict:
        a = dict(args or {})
        try:
            res = await asyncio.to_thread(run_code, ctx.task, ctx.employee.id, a.get("code"), a.get("files"),
                                          a.get("timeout_s"))
        except Refused as exc:
            return _text(str(exc), error=True)
        except Exception as exc:  # noqa: BLE001  (a sandbox fault must never crash the employee's turn)
            log.exception("run_python failed")
            return _text("The code sandbox failed: %s: %s" % (type(exc).__name__, str(exc)[:300]), error=True)
        return _text(json.dumps(res, default=str, separators=(",", ":")))

    return Tool("run_python", desc, schema, handler)


def export_data_tool(ctx):
    from . import query as q
    from .tools import Tool, _obj, _text
    cap = int(_num("AGENTS_EXPORT_MAX_ROWS", 2000))
    detail_max = int(_num("AGENTS_EXPORT_DETAIL_MAX", 100, 0))
    desc = ("Write a CSV for run_python to read, since its code has no network: source=records (a saved preset and/or "
            "records_query's filters, up to %d rows, owner names but never a phone number or email), log (the brain's "
            "copy of the activity log), or tasks, stage_events or batches (the brain's own tables). It returns the "
            "file name to put in run_python's files; the code reads it from in/." % cap)
    props: dict = {
        "source": {"type": "string", "enum": list(SOURCES),
                   "description": "records (DataSift records), log (the account log copy), tasks, stage_events or "
                                  "batches (the brain's own tables)"},
        "name": {"type": "string", "description": "A short file name for the CSV (default: the source)"}}
    for k, v in q.schema("records_query")["properties"].items():
        if k != "rows":
            props[k] = dict(v, description="records: " + str(v.get("description") or ""))
    props["max_rows"] = {"type": "integer", "description": "records: rows to write, at most %d" % cap}
    props["with_titles"] = {"type": "boolean", "description": "records: also read each record's list and tag titles "
                                                              "(one request a record, the first %d rows)" % detail_max}
    props["what"] = {"type": "string", "enum": list(LOG_WHAT), "description": "log: which events (default all)"}
    props["since"] = {"type": "string",
                      "description": "log and tables: the first day, YYYY-MM-DD or a count back such as 7-days or "
                                     "3-months (default 7 days for the log, 30 for tables)"}
    props["until"] = {"type": "string", "description": "log: the last day (default today)"}
    props["status"] = {"type": "string", "description": "tasks or batches: only this status"}
    props["workflow"] = {"type": "string", "description": "tasks: only this workflow"}
    props["stage"] = {"type": "string", "description": "stage_events or batches: only this stage"}
    props["action"] = {"type": "string", "description": "stage_events: only this action"}

    async def handler(args: dict) -> dict:
        try:
            res = await asyncio.to_thread(export, ctx.task, dict(args or {}))
        except Refused as exc:
            return _text(str(exc), error=True)
        except Exception as exc:  # noqa: BLE001
            log.exception("export_data failed")
            return _text("The export failed: %s: %s" % (type(exc).__name__, str(exc)[:300]), error=True)
        return _text(json.dumps(res, default=str, separators=(",", ":")))

    return Tool("export_data", desc, _obj(props, ["source"]), handler, read_only=True)


def sandbox_tools(ctx) -> list:
    """run_python and export_data, for every employee. AGENTS_SANDBOX=0 removes both."""
    if not enabled():
        return []
    return [run_python_tool(ctx), export_data_tool(ctx)]


# ------------------------------------------------------------------------------------- the box check
CHECK_ROOTS = "/etc,/app,/fly,/.fly,/run,/var,/tmp,/root,/home,/opt,/srv,/usr/local/etc"
SOCKET_ROOTS = ("/.fly", "/run", "/var/run", "/tmp", "/dev", "/var/lib")
SECRET_WORDS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASS", "AUTH", "WEBHOOK", "CREDENTIAL", "PRIVATE", "SIGNING",
                "DSN")

PROBE_BODY = r'''
import os
import socket
import stat

rep = {"uid": [os.getuid(), os.geteuid(), os.getgid(), os.getegid()], "groups": os.getgroups(), "env": dict(os.environ)}
status = {}
with open("/proc/self/status") as f:
    for line in f:
        key, _, value = line.partition(":")
        if key in ("NoNewPrivs", "CapEff", "CapPrm", "CapAmb", "CapInh"):
            status[key] = value.strip()
rep["status"] = status
rep["ifaces"] = sorted(n for _, n in socket.if_nameindex())


def readable(p):
    try:
        with open(p, "rb") as f:
            f.read(1)
        return True
    except OSError:
        return False


def listable(p):
    try:
        os.listdir(p)
        return True
    except OSError:
        return False


rep["proc1_environ"] = readable("/proc/1/environ")
rep["parent_environ"] = readable("/proc/%d/environ" % os.getppid())
rep["parent_fd"] = listable("/proc/%d/fd" % os.getppid())
rep["data"] = {p: (listable(p) or readable(p)) for p in CFG["data"]}

net = {}
if rep["ifaces"] == ["lo"]:
    tries = [("tcp to 192.0.2.1:80", socket.AF_INET, socket.SOCK_STREAM, ("192.0.2.1", 80)),
             ("udp to 192.0.2.1:53", socket.AF_INET, socket.SOCK_DGRAM, ("192.0.2.1", 53)),
             ("tcp to the brain's own port", socket.AF_INET, socket.SOCK_STREAM, ("127.0.0.1", CFG["port"])),
             ("tcp to 2001:db8::1", socket.AF_INET6, socket.SOCK_STREAM, ("2001:db8::1", 80, 0, 0))]
    for name, fam, kind, addr in tries:
        s = None
        try:
            s = socket.socket(fam, kind)
            s.settimeout(3)
            if kind == socket.SOCK_DGRAM:
                s.sendto(b"x", addr)
                net[name] = "sent"
            else:
                s.connect(addr)
                net[name] = "connected"
        except socket.timeout:
            net[name] = "timed out"
        except OSError as e:
            net[name] = e.errno
        finally:
            if s is not None:
                s.close()
else:
    net["skipped"] = "the child sees interfaces " + ", ".join(rep["ifaces"])
rep["net"] = net

unix, seen = [], 0
for root in CFG["socket_roots"]:
    for dirpath, dirnames, filenames in os.walk(root):
        if dirpath[len(root):].count(os.sep) >= 4:
            dirnames[:] = []
        for name in filenames:
            seen += 1
            if seen > CFG["max_entries"]:
                break
            p = os.path.join(dirpath, name)
            try:
                st = os.lstat(p)
            except OSError:
                continue
            if stat.S_ISSOCK(st.st_mode):
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.settimeout(1)
                try:
                    s.connect(p)
                    unix.append(p)
                except OSError:
                    pass
                finally:
                    s.close()
rep["unix"] = unix

found, here = [], os.getcwd()
for root in CFG["sweep_roots"]:
    if len(found) >= CFG["max_files"]:
        break
    for dirpath, dirnames, filenames in os.walk(root):
        if dirpath.startswith(here):
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames if os.path.join(dirpath, d) not in CFG["skip"]]
        for name in filenames:
            p = os.path.join(dirpath, name)
            try:
                st = os.lstat(p)
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode) and st.st_size <= CFG["max_bytes"] and readable(p):
                found.append(p)
                if len(found) >= CFG["max_files"]:
                    break
        if len(found) >= CFG["max_files"]:
            break
rep["readable_capped"] = len(found) >= CFG["max_files"]
for pid in os.listdir("/proc"):
    if pid.isdigit():
        for leaf in ("cmdline", "environ"):
            p = "/proc/%s/%s" % (pid, leaf)
            if readable(p):
                found.append(p)
rep["readable"] = found

with open("out/probe.txt", "w") as f:
    f.write("ok")
for target, name in (("/proc/1/environ", "leak.txt"), (CFG["db"], "db.json")):
    try:
        os.symlink(target, os.path.join("out", name))
    except OSError:
        pass
try:
    os.link("/etc/passwd", "out/hard.txt")
    rep["hardlink"] = "made"
except OSError as e:
    rep["hardlink"] = e.errno

libs = {}
for name in ("numpy", "pandas", "matplotlib", "openpyxl"):
    try:
        __import__(name)
        libs[name] = "ok"
    except Exception as e:
        libs[name] = type(e).__name__
if libs.get("matplotlib") == "ok":
    try:
        import matplotlib.pyplot as plt
        plt.plot([1, 2, 3], [3, 1, 2])
        plt.savefig("out/chart.png")
        libs["chart"] = "ok"
    except Exception as e:
        libs["chart"] = type(e).__name__
rep["libs"] = libs
print(json.dumps(rep))
'''

FORK_BOMB = """import os
import time
while True:
    try:
        os.fork()
    except OSError:
        time.sleep(0.05)
"""

ESCAPE = """import os
import time
if os.fork() == 0:
    os.setsid()
    if os.fork() == 0:
        time.sleep(300)
    os._exit(0)
print("left a process running in its own session")
"""

BIG_FILE = """with open("out/big.txt", "wb") as f:
    for _ in range(10):
        f.write(b"x" * (1024 * 1024))
print("wrote it all")
"""

WRITE_MUCH = """import os
import time
for i in range(60):
    path = ("/tmp/sbx-check-fill-%d" % i) if i % 2 else ("fill-%d" % i)
    with open(path, "wb") as f:
        f.write(b"x" * (1024 * 1024))
    time.sleep(0.1)
print("wrote 60 MB")
"""


def _probe_code() -> str:
    roots = [r.strip() for r in (os.environ.get("AGENTS_SANDBOX_CHECK_ROOTS") or CHECK_ROOTS).split(",") if r.strip()]
    cfg = {"data": [str(p) for p in brain_paths()] + [str(config.DB_PATH)], "db": str(config.DB_PATH),
           "port": int(_num("PORT", 8080)), "sweep_roots": roots, "socket_roots": list(SOCKET_ROOTS),
           "skip": ["/var/lib/dpkg", "/var/lib/apt", "/var/cache", "/var/lib/ucf"], "max_files": 40000,
           "max_bytes": 2 * MB, "max_entries": 20000}
    return "import json\nCFG = json.loads(%r)\n" % json.dumps(cfg) + PROBE_BODY


def _secret_values(env) -> list[str]:
    """The values in an environment that are keys: a name that says so, and long enough to matter."""
    return sorted({v for k, v in dict(env).items() if len(v or "") >= 12 and any(w in k.upper() for w in SECRET_WORDS)})


def _secret_files(paths: list, secrets: list, budget_mb: int = 400) -> list[str]:
    """Which of these files (that the CHILD could read) hold a key. Read here, by root, so the child
    itself never sees a key even while the check proves it could not have."""
    needles = [s.encode("utf-8") for s in secrets]
    hits, read = [], 0
    for p in paths:
        try:
            with open(p, "rb") as f:
                data = f.read(2 * MB + 1)
        except OSError:
            continue
        read += len(data)
        if any(n in data for n in needles):
            hits.append(p)
        if read > budget_mb * MB:
            break
    return hits


def _report(ran: Ran):
    if not ran.started:
        return None
    for line in reversed(ran.stdout.strip().splitlines()):
        try:
            rep = json.loads(line)
        except ValueError:
            continue
        if isinstance(rep, dict):
            return rep
    return None


def _net_words(net: dict) -> str:
    return "; ".join("%s: %s" % (k, ("errno %d" % v) if isinstance(v, int) else v) for k, v in net.items())


def _leftovers(uid: int) -> list[str]:
    out = []
    try:
        out += ["run folder %s" % e.name for e in os.scandir(str(base_dir())) if e.name != ".lock"]
    except OSError:
        pass
    pids = _owned_pids(uid)
    if pids:
        out.append("%d process(es)" % len(pids))
    out += _owned_tops(uid, str(base_dir()))
    return out


def check(say=print) -> int:
    """Prove each guarantee on this machine with real runs, and say PASS or FAIL for each. Exit 1 on
    any FAIL. Meant for the box, as root, from /app/src: python -m agents.sandbox --check."""
    fails: list = []

    def line(ok: bool, text: str) -> None:
        say(("PASS  " if ok else "FAIL  ") + text)
        if not ok:
            fails.append(text)

    ok, why, info = preflight()
    line(ok, "the sandbox can start here" + ("" if ok else ": " + why))
    if not ok:
        say("1 failed: nothing else can be proven until this passes")
        return 1
    uid, gid = info["uid"], info["gid"]
    line(True, "the %s user is uid %d in group %d, not root; unshare is %s and setpriv is %s" % (
        user_name(), uid, gid, info["unshare"], info["setpriv"]))
    for p in brain_paths():
        line(closed_to(p, uid, gid), "%s is closed to the sandbox user" % p)
    secrets = _secret_values(os.environ)
    line(bool(secrets), "this check's own environment holds %d key%s to look for%s" % (
        len(secrets), "" if len(secrets) == 1 else "s",
        "" if secrets else " (run it on the box with fly ssh console, where the brain's keys are set)"))
    use = Limits.from_env()

    dest = Path(tempfile.mkdtemp(prefix="sbx-check-"))
    try:
        lim = Limits.from_env()
        lim.wall_s = min(lim.wall_s, 150.0)
        ran = execute(_probe_code(), [], dest, lim, "check-probe")
        rep = _report(ran)
        if rep is None:
            line(False, "the probe ran and reported (%s)" % (ran.why or ran.ended or "no report") +
                 ((": " + ran.stderr.strip().splitlines()[-1][:200]) if ran.stderr.strip() else ""))
        else:
            st = rep.get("status") or {}
            caps_clear = all(int(st.get(k) or "1", 16) == 0 for k in ("CapEff", "CapPrm", "CapAmb"))
            line(rep.get("uid") == [uid, uid, gid, gid] and rep.get("groups") == [] and st.get("NoNewPrivs") == "1"
                 and caps_clear, "the code runs as the sandbox user, with no other group, no capability and no way "
                                 "back to root")
            env = rep.get("env") or {}
            extra = sorted(set(env) - set(ENV_KEYS))
            hit = any(s in str(v) for s in secrets for v in env.values())
            line(set(env) == set(ENV_KEYS) and not hit, "no key in the child's environment: it holds only %s%s" % (
                ", ".join(sorted(env)), " (extra: %s)" % ", ".join(extra) if extra else ""))
            line(rep.get("proc1_environ") is False, "/proc/1/environ is unreadable")
            line(rep.get("parent_environ") is False and rep.get("parent_fd") is False,
                 "the brain's own process (its environment and its open files) is unreadable")
            opened = [p for p, v in (rep.get("data") or {}).items() if v]
            line(not opened, "/data cannot be listed or read" + (" (open: %s)" % ", ".join(opened) if opened else ""))
            net = rep.get("net") or {}
            gone = rep.get("ifaces") == ["lo"] and bool(net) and all(isinstance(v, int) for v in net.values())
            line(gone, "the network is gone: only a loopback, and every socket to an outside address fails (%s)"
                 % _net_words(net))
            unix = rep.get("unix") or []
            line(not unix, "no Unix socket on the machine accepts the sandbox user" +
                 (" (it reached %s)" % ", ".join(unix[:3]) if unix else ""))
            readable = rep.get("readable") or []
            found = _secret_files(readable, secrets)
            line(bool(secrets) and not found, "no file the sandbox user can read holds a key (%d readable files "
                 "checked%s)%s" % (len(readable), ", the list was capped" if rep.get("readable_capped") else "",
                                   (": " + ", ".join(found[:3])) if found else ""))
            names = {f["name"] for f in ran.files}
            try:
                body = (dest / "probe.txt").read_text(encoding="utf-8")
            except OSError:
                body = ""
            line("probe.txt" in names and body == "ok", "out/ is writable and what the code writes there comes back "
                                                        "as a file")
            planted = sorted({"leak.txt", "db.json", "hard.txt"} & names)
            line(not planted, "a link the code plants in out/ is never followed or copied" +
                 (" (copied: %s)" % ", ".join(planted) if planted else ""))
            libs = rep.get("libs") or {}
            broken = ["%s %s" % (k, v) for k, v in libs.items() if v != "ok"]
            line(not broken and "chart.png" in names, "pandas, numpy, matplotlib and openpyxl import inside the "
                 "sandbox and a chart renders" + (" (not working: %s)" % ", ".join(broken) if broken else ""))
    finally:
        shutil.rmtree(str(dest), ignore_errors=True)

    r = execute("while True:\n    pass\n", [], None, Limits(cpu_s=2, wall_s=30), "check-cpu")
    line(r.started and not r.killed_for and r.returncode is not None and r.returncode < 0 and r.seconds < 25,
         "a CPU busy loop is stopped at the CPU limit (2 s here, %d s in use): %s" % (use.cpu_s, r.ended or r.why))
    r = execute("import time\ntime.sleep(600)\n", [], None, Limits(wall_s=3), "check-wall")
    line(r.started and r.killed_for == "wall" and r.seconds < 15,
         "a sleep is stopped at the wall clock (3 s here, %d s in use): %s" % (use.wall_s, r.ended or r.why))
    r = execute(FORK_BOMB, [], None, Limits(procs=16, wall_s=6), "check-fork")
    line(r.started and r.left_running == 0 and 0 < r.max_procs <= 16,
         "a fork bomb is contained: at most %d processes at once (16 allowed here, %d in use), none left after"
         % (r.max_procs, use.procs))
    r = execute(ESCAPE, [], None, Limits(wall_s=20), "check-escape")
    line(r.started and r.left_running == 0 and not _owned_pids(uid),
         "a process that leaves its group and session is stopped with the rest")
    r = execute("x = bytearray(512 * 1024 * 1024)\nprint(len(x))\n", [], None, Limits(mem_mb=256, wall_s=30),
                "check-memory")
    line(r.started and "MemoryError" in r.stderr, "memory past the limit is refused (256 MB here, %d MB in use)"
         % use.mem_mb)
    big = Path(tempfile.mkdtemp(prefix="sbx-check-"))
    try:
        r = execute(BIG_FILE, [], big, Limits(file_mb=2, wall_s=30), "check-fsize")
        size = (big / "big.txt").stat().st_size if (big / "big.txt").is_file() else -1
        line(r.started and ("File too large" in r.stderr or r.returncode == -SIGXFSZ) and size <= 2 * MB,
             "a file past the size limit is stopped (2 MB here, %d MB in use)" % use.file_mb)
    finally:
        shutil.rmtree(str(big), ignore_errors=True)
    r = execute(WRITE_MUCH, [], None, Limits(work_mb=5, wall_s=30), "check-disk")
    line(r.started and r.killed_for == "size", "a run that writes past its allowance is stopped, in its folder or in "
         "/tmp (5 MB here, %d MB in use): %s" % (use.work_mb, r.ended or r.why))
    left = _leftovers(uid)
    line(not left, "nothing is left behind: no run folder, no process and no temp file of the sandbox user" +
         (" (%s)" % ", ".join(left[:5]) if left else ""))
    say(("%d failed" % len(fails)) if fails else "all passed")
    return 1 if fails else 0


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="python -m agents.sandbox",
                                 description="The code sandbox (src/agents/sandbox.py).")
    ap.add_argument("--check", action="store_true",
                    help="prove each isolation guarantee on this machine (as root, on the box)")
    ap.add_argument("--harden", action="store_true",
                    help="close the brain's folders now, as the brain does at startup")
    args = ap.parse_args(argv)
    if args.harden:
        for said in harden() or ["sandbox: nothing to close"]:
            print(said)
    if args.check:
        return check()
    if not args.harden:
        ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())

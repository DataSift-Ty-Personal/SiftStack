"""Where the cold email program keeps its files, which install runs it, and one run at a time.

The four jobs (replies every 5 minutes, the nightly top-up, the weekday health check, the Monday
stats) ran as Windows scheduled tasks on Ty's PC until 2026-11-20 and now run in the agents brain.
Each must run in exactly ONE place: two reply watchers post every hand raise twice, two top-ups race
the ledger and spend ZeroBounce credits twice.

The state folder, first match wins:
    COLD_EMAIL_STATE_DIR            an explicit folder (a test, a one-off)
    AGENTS_DATA_DIR/cold_email      the agents brain (its image sets AGENTS_DATA_DIR=/data)
    <repo>/output/cold_email        a workstation, where the jobs ran first

The home: the one install that acts. It is written in the state folder (home.json, {"home": "<install>",
"since": ..., "by": ...}). An install acts only where home.json names it. With no home.json the
workstation is the home: that is how the jobs ran before the brain took them, so a workstation that
was never cut over keeps working, and the brain does nothing until the cutover names it
(deploy/cold_email_cutover.py writes home.json on both sides in the same step).

This install: COLD_EMAIL_INSTALL, else the Fly app's name (FLY_APP_NAME, set by Fly on every
machine), else "workstation".

One run at a time on a machine: RunLock takes an operating system lock on a file in the state folder,
so the clock's run and a run by hand never overlap, and a run that dies (a redeploy) leaves no lock
behind for the next one to wait out.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKSTATION = "workstation"
HOME_FILE = "home.json"
HEARTBEAT_FILE = "replies_heartbeat.json"


def _env(name: str) -> str:
    """A setting from the environment, else from the repo's .env (how every cold email module reads one)."""
    v = os.environ.get(name, "").strip()
    if not v and (ROOT / ".env").exists():
        try:
            lines = (ROOT / ".env").read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            lines = []
        for line in lines:
            if line.startswith(name + "="):
                v = line.split("=", 1)[1].strip().strip('"').strip("'")
    return v


def state_dir() -> Path:
    """The folder that holds ledger.json, rejected.json, replies_state.json and the rest."""
    explicit = _env("COLD_EMAIL_STATE_DIR")
    if explicit:
        return Path(explicit)
    data = _env("AGENTS_DATA_DIR")
    if data:
        return Path(data) / "cold_email"
    return ROOT / "output" / "cold_email"


def install_name() -> str:
    return _env("COLD_EMAIL_INSTALL") or os.environ.get("FLY_APP_NAME", "").strip() or WORKSTATION


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_home(state: Path | None = None) -> dict:
    p = Path(state or state_dir()) / HOME_FILE
    try:
        d = json.loads(p.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        # A home.json nobody can read names nobody: refusing is the safe answer, never "anyone may run".
        return {"home": "", "unreadable": True}
    return d if isinstance(d, dict) else {"home": "", "unreadable": True}


def home_name(state: Path | None = None) -> str:
    d = read_home(state)
    if d.get("unreadable"):
        return ""
    return str(d.get("home") or "").strip() or WORKSTATION


def owns(state: Path | None = None) -> tuple[bool, str]:
    """(True, "") when this install is the cold email program's home, else (False, why in words)."""
    d = read_home(state)
    if d.get("unreadable"):
        return False, "the cold email home file (%s) cannot be read, so no install acts until it is fixed" % HOME_FILE
    home, me = home_name(state), install_name()
    if home == me:
        return True, ""
    since = (" since %s" % str(d.get("since"))[:10]) if d.get("since") else ""
    return False, "cold email runs on %s%s, not on %s" % (home, since, me)


def require(action: str, state: Path | None = None) -> None:
    """For a command line: stop with exit code 3 when this install is not the home, so a run by hand on
    the PC after the cutover cannot post a hand raise twice or write a ledger nobody reads."""
    ok, why = owns(state)
    if not ok:
        import sys
        print("not run: %s, because %s. The files on this machine are not the live state." % (action, why),
              file=sys.stderr)
        raise SystemExit(3)


def write_json(path: Path, data) -> None:
    """Write a JSON file whole or not at all: a reader never sees half of it (the ledger is read by the
    reply watcher while a top-up writes it)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp-%d" % os.getpid())
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def claim(home: str, state: Path | None = None, by: str = "", note: str = "") -> Path:
    """Name the install that runs cold email from now on (the cutover writes this on both sides)."""
    p = Path(state or state_dir()) / HOME_FILE
    write_json(p, {"home": home, "since": now_iso(), "by": by or install_name(), "note": note})
    return p


def heartbeat(state: Path | None = None) -> dict:
    """The reply watcher's last committed pass ({"at", "install", "new", "handled"}), or {}."""
    p = Path(state or state_dir()) / HEARTBEAT_FILE
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def beat(state: Path | None = None, **info) -> None:
    write_json(Path(state or state_dir()) / HEARTBEAT_FILE, dict(info, at=now_iso(), install=install_name()))


# ---------------------------------------------------------------------- one run at a time
def _lock(fh) -> bool:
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(fh) -> None:
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


class RunLock:
    """One job at a time on this machine (`replies`, `topup`). acquire() never waits by default: a job
    that finds the lock held skips this run, and the next slot tries again. The lock belongs to the open
    file, so the operating system lets it go when the process ends, however it ends."""

    def __init__(self, name: str, state: Path | None = None):
        self.path = Path(state or state_dir()) / "locks" / ("%s.lock" % name)
        self._fh = None

    def acquire(self, wait_s: float = 0.0, poll_s: float = 1.0) -> bool:
        import time
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.time() + max(0.0, wait_s)
        while True:
            fh = open(self.path, "a+")
            if _lock(fh):
                self._fh = fh
                return True
            fh.close()
            if time.time() >= deadline:
                return False
            time.sleep(poll_s)

    def release(self) -> None:
        if self._fh is not None:
            _unlock(self._fh)
            self._fh.close()
            self._fh = None

    @property
    def held(self) -> bool:
        return self._fh is not None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()
        return False

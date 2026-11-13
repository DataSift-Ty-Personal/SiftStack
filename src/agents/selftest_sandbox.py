"""Code sandbox selftest: the command line and the environment the executor builds, every fail-closed
refusal (the preflight on a stand-in machine, and a run that never shows the bootstrap's mark), the
files an employee may and may not hand the code, what comes back out of out/ and what never does,
the run folder and its signed links, and export_data, whose rows never carry a phone number or an
email. Zero network: a fake executor stands in for the Linux one (the real one is proven on the box
by `python -m agents.sandbox --check`), and a fake records search stands in for DataSift. Every
name, street, number and id here is invented; the phone numbers sit on the never-assigned 865-000
exchange.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from . import config, sandbox

NOW = datetime(2026, 11, 13, 15, 0, tzinfo=timezone.utc)      # Friday 10:00 Eastern
MB = 1024 * 1024
SETTINGS = ("AGENTS_SANDBOX", "AGENTS_SANDBOX_DIR", "AGENTS_SANDBOX_USER", "AGENTS_SANDBOX_PYTHON", "AGENTS_SANDBOX_CPU_S",
            "AGENTS_SANDBOX_WALL_S", "AGENTS_SANDBOX_MEM_MB", "AGENTS_SANDBOX_FILE_MB", "AGENTS_SANDBOX_OPEN_FILES",
            "AGENTS_SANDBOX_PROCS", "AGENTS_SANDBOX_WORK_MB", "AGENTS_SANDBOX_MADE_FILES", "AGENTS_SANDBOX_IN_FILES",
            "AGENTS_SANDBOX_IN_MB", "AGENTS_SANDBOX_OUT_FILES", "AGENTS_SANDBOX_OUT_MB", "AGENTS_SANDBOX_CODE_KB",
            "AGENTS_SANDBOX_OUTPUT_CHARS", "AGENTS_SANDBOX_LOG_MB", "AGENTS_EXPORT_MAX_ROWS", "AGENTS_EXPORT_DETAIL_MAX",
            "AGENTS_EXPORT_LOCAL_ROWS", "AGENTS_QUERY_TOOLS", "CLAUDE_CONFIG_DIR")


def U(n: int) -> str:
    """An invented uuid in the real 8-4-4-4-12 shape."""
    return "%08d-2222-4222-8222-%012d" % (n, n)


def PHONE(n: int) -> str:
    return "+18650000%03d" % (n % 1000)


def EMAIL(n: int) -> str:
    return "owner%d@example.com" % n


LISTS = {"Probate": U(901), "Foreclosure": U(902)}
TAGS = [("FTM", U(911)), ("Courthouse Data", U(912))]
PRESETS = [{"title": "Probate - No Phone", "uuid": U(701), "type": "properties",
            "filters": {"must": {"any_lists": [LISTS["Probate"]], "phone": 0}}}]


@contextlib.contextmanager
def _env(**kw):
    old = {k: os.environ.get(k) for k in kw}
    try:
        for k, v in kw.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = str(v)
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class FakeApiError(Exception):
    def __init__(self, code: int, body: str = ""):
        super().__init__("HTTP %s: %s" % (code, body))
        self.code, self.body = code, body


class FakeCRM:
    """The records search and the record GET, as far as the export uses them. It refuses every write."""

    def __init__(self, n: int = 600):
        self.recs = []
        for i in range(1, n + 1):
            lists = [LISTS["Probate"]] + ([LISTS["Foreclosure"]] if i % 3 == 0 else [])
            self.recs.append({"n": i, "uuid": U(1000 + i), "street": "%d Invented Way" % i, "lists": lists,
                              "phones": i % 2 == 0, "owner": U(5000 + i)})
        self.recs[0]["first"] = "=HYPERLINK(1)"
        self.searches, self.gets, self.writes = [], [], []
        self.fail = None
        self.missing = set()

    def _match(self, r, must) -> bool:
        for k, v in must.items():
            if k == "property_type":
                ok = v == "clean"
            elif k == "any_lists":
                ok = bool(set(v) & set(r["lists"]))
            elif k == "all_lists":
                ok = set(v) <= set(r["lists"])
            elif k == "phone":
                ok = bool(r["phones"]) == bool(v)
            elif k == "owners":
                ok = r["owner"] in v
            elif k == "must_not":
                ok = not (set(v.get("any_lists") or []) & set(r["lists"]))
            else:
                raise AssertionError("the export sent a key the fake does not know: %s" % k)
            if not ok:
                return False
        return True

    def _row(self, r) -> dict:
        n = r["n"]
        return {"uuid": r["uuid"],
                "address": {"street": r["street"], "city": "Exampleton", "state": "TN", "zip5": "37990",
                            "postal_code": "37990-0001", "vacant": n % 5 == 0},
                "owner": {"uuid": r["owner"], "first_name": r.get("first", "Quincy"), "last_name": "Testowner%d" % n,
                          "company": None, "emails": [EMAIL(n)], "phones": [{"number": PHONE(n)}]},
                "status": "not_interested" if n % 4 == 0 else None, "list_count": len(r["lists"]) + 2,
                "system_list_count": 2, "tag_count": 1, "has_phones": r["phones"], "skiptraced": True,
                "last_skip_traced": "2026-10-01 06:00:00", "directmail_attempts": n % 3, "do_not_mail_ever": False,
                "realtor_score": 50, "on_market_score": 40, "emails": [EMAIL(n)],
                "phone": {"number": PHONE(n), "doNotCall": True, "status": "CORRECT"} if r["phones"] else None}

    def search(self, must, limit=250, offset=0):
        self.searches.append({"must": json.loads(json.dumps(must)), "limit": limit, "offset": offset})
        if self.fail:
            raise self.fail
        if offset + limit > 10000:
            raise FakeApiError(400, '{"detail": "Can not fetch more than 10000 items"}')
        hits = [r for r in self.recs if self._match(r, must)]
        return len(hits), [self._row(r) for r in hits[offset:offset + limit]]

    def call(self, path, method="GET", body=None, headers=None):
        if method != "GET" or body is not None:
            self.writes.append((method, path))
            raise AssertionError("an export tried to write: %s %s" % (method, path))
        self.gets.append(path)
        base = path.split("?")[0]
        if base == "/api/internal/list/":
            return {"results": [{"uuid": u, "title": t} for t, u in LISTS.items()]}
        if base == "/api/internal/tag/":
            return {"results": [{"uuid": u, "title": t} for t, u in TAGS]}
        if base == "/api/internal/filter-preset/":
            return {"results": json.loads(json.dumps(PRESETS))}
        if base == "/api/internal/global-status/":
            return []
        for r in self.recs:
            if base == "/api/internal/property/%s/" % r["uuid"]:
                if r["uuid"] in self.missing:
                    raise FakeApiError(404, '{"detail": "Not found."}')
                return {"uuid": r["uuid"], "address": {"street": r["street"]},
                        "lists": [{"title": "Probate"}] + ([{"title": "Foreclosure"}] if len(r["lists"]) > 1 else []),
                        "tags": [{"title": "FTM"}],
                        "owner": {"uuid": r["owner"], "phones": [{"number": PHONE(r["n"] + 500)}],
                                  "emails": [{"email": "detail%d@example.com" % r["n"]}]}}
        raise AssertionError("unexpected GET %s" % path)


class FakeExecutor(sandbox.Executor):
    """Stands where the Linux executor stands. `script(work)` plays the code: it may read in/ and write
    out/ under the work folder, and returns (exit code, stdout, stderr)."""

    def __init__(self, script=None, outcome=None, ready=(True, ""), mark=True, busy=False):
        self.script, self.outcome, self._ready, self.mark, self.busy = script, outcome, ready, mark, busy
        self.jobs, self.befores, self.afters, self.seen = [], 0, [], {}

    def ready(self):
        return self._ready

    def ids(self):
        return 990, 991

    @contextlib.contextmanager
    def lock(self, wait_s):
        if self.busy:
            raise sandbox.Busy()
        yield

    def before(self):
        self.befores += 1

    def after(self, run_dir):
        self.afters.append(Path(run_dir))
        super().after(run_dir)

    def run(self, job):
        self.jobs.append(job)
        work = Path(job.cwd)
        run_dir = work.parent
        self.seen = {"boot": (run_dir / "boot.py").read_text(encoding="utf-8"),
                     "code": (run_dir / "code.py").read_text(encoding="utf-8"),
                     "in": sorted(p.name for p in (work / "in").iterdir()), "out_exists": (work / "out").is_dir()}
        if self.outcome is not None:
            return self.outcome
        rc, out, err = self.script(work) if self.script else (0, b"", b"")
        head = (sandbox.MARK + "\n").encode() if self.mark else b""
        return sandbox.Outcome(returncode=rc, stdout=out, stderr=head + err, seconds=0.2)


class FakeHost:
    """A stand-in machine for the preflight and harden(): Linux, root, a sandbox user, both tools, and
    every brain folder closed, unless a test says otherwise."""

    def __init__(self, platform="linux", euid=0, users=None, tools=None, missing=(), open_paths=(), fly=(),
                 base_bad="", owners=None):
        self.platform, self.root = platform, euid
        self.users = {"sandbox": (990, 991)} if users is None else users
        self.tools = {"unshare": "/usr/bin/unshare", "setpriv": "/usr/bin/setpriv"} if tools is None else tools
        self.missing, self.open = set(missing), {str(p) for p in open_paths}
        self.fly, self.base_bad, self.owners, self.chmods = list(fly), base_bad, dict(owners or {}), []

    def euid(self):
        return self.root

    def user(self, name):
        return self.users.get(name)

    def which(self, tool):
        return self.tools.get(tool, "")

    def exists(self, path):
        return str(path) not in self.missing

    def owner(self, path):
        return self.owners.get(str(path), 0)

    def chmod(self, path, mode):
        self.chmods.append((str(path), mode))
        self.open.discard(str(path))
        if str(path) == sandbox.FLY_DIR:
            self.fly = []

    def closed(self, path, uid, gid):
        return str(path) not in self.open

    def fly_sockets(self, uid, gid):
        return list(self.fly)

    def base_problem(self, path, uid, gid):
        return self.base_bad


def _task(store, workflow="ask", thread="th-sbx-1", agent="analysis"):
    tid, _ = store.enqueue(agent, workflow, "", subject="a sandbox test", thread_id=thread, mode="agent")
    return store.task(tid)


def _text_of(res) -> str:
    return "".join(c.get("text", "") for c in (res or {}).get("content") or [])


def run(check, tmp: Path) -> None:
    from . import query, store
    saved_cfg = (config.DB_PATH, config.RUNS_DIR, config.WORKSPACES_DIR, config.DATA_DIR, config.PUBLIC_URL)
    saved_q = (query.CRM, query.MAP, query.NOW, query.POLICY)
    saved_env = {k: os.environ.get(k) for k in SETTINGS}
    root = tmp / "sandbox"
    root.mkdir(parents=True, exist_ok=True)
    config.DB_PATH, config.RUNS_DIR = root / "sandbox.db", root / "runs"
    config.WORKSPACES_DIR, config.DATA_DIR = root / "ws", root
    config.PUBLIC_URL = "https://agents.example.test"
    for k in SETTINGS:
        os.environ.pop(k, None)
    os.environ["AGENTS_SANDBOX_DIR"] = str(root / "sbx")
    store.init()
    query.reset()
    query.NOW, query.POLICY = NOW, SimpleNamespace(tz="America/New_York")
    sandbox._RUNS.clear()
    try:
        _command_and_env(check)
        _preflight(check)
        _outcomes(check, root)
        _inputs(check, store, root)
        _outputs(check, root)
        _round_trip(check, store, root)
        _records(check, store, query, root)
        _log_and_tables(check, store, root)
        _tools(check, store, root)
        _hygiene(check)
    finally:
        sandbox.EXECUTOR = None
        sandbox._RUNS.clear()
        config.DB_PATH, config.RUNS_DIR, config.WORKSPACES_DIR, config.DATA_DIR, config.PUBLIC_URL = saved_cfg
        query.CRM, query.MAP, query.NOW, query.POLICY = saved_q
        query.reset()
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ------------------------------------------------------------------------------------- the child
def _command_and_env(check) -> None:
    print("  the command and the environment")
    sb = sandbox
    argv = sb.command(990, 991, "/tmp/sbx/7-1/boot.py", "/tmp/sbx/7-1/code.py", python="/usr/local/bin/python3",
                      unshare="/usr/bin/unshare", setpriv="/usr/bin/setpriv")
    check(argv == ["/usr/bin/unshare", "--net", "--", "/usr/bin/setpriv", "--reuid=990", "--regid=991", "--clear-groups",
                   "--no-new-privs", "--", "/usr/local/bin/python3", "-I", "-B", "/tmp/sbx/7-1/boot.py", "990", "991",
                   "/tmp/sbx/7-1/code.py"],
          "the child's command is unshare --net, then setpriv to the sandbox user with no groups and no_new_privs, "
          "then python3 -I -B on the bootstrap")
    check(argv.index("--net") < argv.index("/usr/bin/setpriv") < argv.index("/usr/local/bin/python3"),
          "the network is cut while the process is still root, before the user changes, before Python starts")
    check(all(isinstance(x, str) for x in argv) and argv[0] != "/bin/sh" and "-c" not in argv,
          "the command is a list of plain arguments, never a shell line")
    check(sb.command(1, 2, "b", "c")[9] == "/usr/local/bin/python3", "the default interpreter is the image's python3")
    with _env(AGENTS_SANDBOX_PYTHON="/opt/example/bin/python3"):
        check(sb.command(1, 2, "b", "c")[9] == "/opt/example/bin/python3", "the interpreter is a setting")

    env = sb.child_env("/tmp/sbx/7-1/work")
    check(set(env) == set(sb.ENV_KEYS) == {"PATH", "HOME", "LANG", "PYTHONIOENCODING", "MPLBACKEND", "TMPDIR"},
          "the child's environment is exactly PATH, HOME, LANG, PYTHONIOENCODING, MPLBACKEND and TMPDIR")
    check(env["HOME"] == env["TMPDIR"] == "/tmp/sbx/7-1/work" and env["PATH"] == "/usr/local/bin:/usr/bin:/bin"
          and env["LANG"] == "C.UTF-8" and env["MPLBACKEND"] == "Agg" and env["PYTHONIOENCODING"] == "utf-8",
          "HOME and TMPDIR are the work folder, and PATH, LANG, PYTHONIOENCODING and MPLBACKEND are fixed values")
    keys = {"REISIFT_API_KEY": "ds-invented-key-0001", "ANTHROPIC_API_KEY": "sk-invented-key-0002",
            "SLACK_BOT_TOKEN": "xoxb-invented-key-0003"}
    with _env(**keys):
        env2 = sb.child_env("/w")
    check(not (set(keys) & set(env2)) and not any(v in json.dumps(env2) for v in keys.values()),
          "a key in the brain's own environment never reaches the child's: it is built from nothing")

    allowed = "ALLOWED = {%s}" % ", ".join(repr(k) for k in sorted(sb.ENV_KEYS))
    check(allowed in sb.BOOT, "the bootstrap checks the child's environment against the same six names")
    for needle, rule in (("NoNewPrivs", "no_new_privs"), ("if_nameindex", "the network"), ("getgroups", "the groups"),
                         ("CapEff", "the capabilities"), ("os.geteuid()", "the user")):
        check(needle in sb.BOOT, "the bootstrap checks %s from inside the child before the code starts" % rule)
    i_net = sb.BOOT.index('refuse("the network is not cut off")')
    i_mark = sb.BOOT.index("sys.stderr.write(%r" % sb.MARK)
    i_exec = sb.BOOT.index('os.execv(sys.executable, [sys.executable, "-I", "-B", code])')
    check(i_net < i_mark < i_exec, "the bootstrap writes its mark only after every check, and starts the code "
                                   "(python3 -I -B code.py) only after the mark")
    check("os._exit(%d)" % sb.REFUSED_EXIT in sb.BOOT, "a refusal inside the child ends it with the bootstrap's own "
                                                       "exit code, before any of the code runs")
    try:
        compile(sb.BOOT, "boot.py", "exec")
        compile(sb._probe_code(), "probe.py", "exec")
        ok = True
    except SyntaxError:
        ok = False
    check(ok, "the bootstrap and the box check's probe are valid Python")

    lim = sb.Limits.from_env()
    check((lim.cpu_s, lim.wall_s, lim.mem_mb, lim.file_mb, lim.open_files, lim.procs) == (120, 180, 2048, 200, 256, 64),
          "the limits default to 120 s of CPU, a 180 s wall clock, 2 GB, 200 MB files, 256 open files, 64 processes")
    check(lim.rlimits() == [("RLIMIT_CPU", 120, 125), ("RLIMIT_AS", 2048 * MB, 2048 * MB),
                            ("RLIMIT_FSIZE", 200 * MB, 200 * MB), ("RLIMIT_NOFILE", 256, 256),
                            ("RLIMIT_NPROC", 64, 64), ("RLIMIT_CORE", 0, 0)],
          "the rlimits set before exec: CPU (soft 120, hard 125), memory, file size, open files, processes, no core files")
    with _env(AGENTS_SANDBOX_CPU_S="30", AGENTS_SANDBOX_WALL_S="45", AGENTS_SANDBOX_PROCS="2"):
        l2 = sb.Limits.from_env()
    check(l2.cpu_s == 30 and l2.wall_s == 45 and l2.procs == 64,
          "each limit is a setting, and a value below its floor falls back to the default")
    with _env(AGENTS_SANDBOX_CPU_S="lots", AGENTS_SANDBOX_MEM_MB="-5"):
        l3 = sb.Limits.from_env()
    check(l3.cpu_s == 120 and l3.mem_mb == 2048, "an unreadable or negative setting falls back to the default")
    check("120 s of CPU" in lim.words() and "180 s wall clock" in lim.words() and "2 GB" in lim.words(),
          "the limits read as plain words for the tool description")


# ------------------------------------------------------------------------------------- fail closed
def _preflight(check) -> None:
    print("  the preflight fails closed")
    sb = sandbox
    data = str(sb.brain_paths()[0])
    ok, why, info = sb.preflight(FakeHost())
    check(ok and why == "" and info == {"uid": 990, "gid": 991, "unshare": "/usr/bin/unshare",
                                        "setpriv": "/usr/bin/setpriv"},
          "on Linux, as root, with the user, both tools and every brain folder closed, the sandbox is ready")
    cases = (
        (FakeHost(platform="win32"), "Linux", "off Linux it refuses"),
        (FakeHost(euid=1000), "not running as root", "a brain not running as root refuses"),
        (FakeHost(users={}), "no 'sandbox' user", "a machine without the sandbox user refuses"),
        (FakeHost(users={"sandbox": (0, 991)}), "root", "a sandbox user that is root refuses"),
        (FakeHost(users={"sandbox": (990, 0)}), "root group", "a sandbox user in the root group refuses"),
        (FakeHost(tools={"setpriv": "/usr/bin/setpriv"}), "unshare is not installed", "no unshare refuses"),
        (FakeHost(tools={"unshare": "/usr/bin/unshare"}), "setpriv is not installed", "no setpriv refuses"),
        (FakeHost(missing={sb.python_path()}), "Python", "a missing interpreter refuses"),
        (FakeHost(open_paths={data}), "%s is open to the sandbox user" % data,
         "a brain folder open to the sandbox user refuses, naming it and the fix (harden)"),
        (FakeHost(fly=["/.fly/api"]), "/.fly/api", "a Fly API socket the sandbox user could open refuses"),
        (FakeHost(base_bad="the sandbox folder /tmp/sbx is not owned by root"), "not owned by root",
         "a sandbox folder the brain cannot trust refuses"),
    )
    for host, needle, rule in cases:
        ok, why, info = sb.preflight(host)
        check(not ok and needle in why and info == {}, rule + ": " + why)
    ok, why, _ = sb.preflight(FakeHost(open_paths={data}))
    check("harden" in why, "the refusal for an open folder names what closes it")
    with _env(AGENTS_SANDBOX_USER="sbxrunner"):
        check(sb.preflight(FakeHost(users={"sbxrunner": (995, 996)}))[0], "the sandbox user's name is a setting")
    with _env(CLAUDE_CONFIG_DIR="/data/claude"):
        check(any(str(p).replace("\\", "/").endswith("/data/claude") for p in sb.brain_paths()),
              "the Claude sessions folder is one of the brain folders the sandbox user must not reach")

    host = FakeHost(open_paths={data})
    said = sb.harden(host)
    check(host.chmods == [(data, 0o700)] and said and data in said[0] and sb.preflight(host)[0],
          "harden closes an open brain folder to 0700 at startup, says so, and the sandbox is ready after it")
    host = FakeHost()
    check(sb.harden(host) == [] and host.chmods == [], "harden changes nothing when every brain folder is closed")
    host = FakeHost(open_paths={data}, owners={data: 1000})
    check(sb.harden(host) == [] and host.chmods == [], "harden never changes a folder root does not own")
    for host, rule in ((FakeHost(open_paths={data}, euid=1000), "not as root"),
                       (FakeHost(open_paths={data}, platform="win32"), "off Linux")):
        check(sb.harden(host) == [] and host.chmods == [], "harden changes nothing %s" % rule)
    host = FakeHost(fly=["/.fly/api"])
    said = sb.harden(host)
    check(host.chmods == [(sb.FLY_DIR, 0o700)] and "/.fly" in said[-1],
          "harden closes the Fly folder only when the sandbox user could open a socket in it")
    host = FakeHost(users={}, open_paths={data})
    check(sb.harden(host) == ["sandbox: closed %s to root only (mode 0700)" % data],
          "harden closes the brain's folder even before the sandbox user exists")

    st = SimpleNamespace
    check(sb._bits(st(st_uid=990, st_gid=5, st_mode=0o40750), 990, 991) == 7 and
          sb._bits(st(st_uid=0, st_gid=991, st_mode=0o40750), 990, 991) == 5 and
          sb._bits(st(st_uid=0, st_gid=0, st_mode=0o40750), 990, 991) == 0 and
          sb._bits(st(st_uid=0, st_gid=0, st_mode=0o40711), 990, 991) == 1,
          "the permission the sandbox user gets is the owner, group or other bits, in that order")
    gone = Path(config.DATA_DIR) / "not-made-yet" / "child"
    check(sb.closed_to(gone, 990, 991) is False if not sb.closed_to(Path(config.DATA_DIR), 990, 991) else True,
          "a folder that does not exist yet counts as open unless a folder above it is closed")
    check(sb.fly_sockets(990, 991, root=str(Path(config.DATA_DIR) / "no-such-fly")) == [],
          "a machine without a Fly folder has no Fly socket to open")


def _outcomes(check, root: Path) -> None:
    print("  a run that never shows the mark is refused")
    sb = sandbox
    lim = sb.Limits.from_env()
    dest = root / "dest-outcomes"
    base = sb.base_dir()

    never = FakeExecutor(ready=(False, "there is no 'sandbox' user on this machine"))
    r = sb.execute("print(1)\n", [], dest, lim, "t1", executor=never)
    check(not r.started and "no 'sandbox' user" in r.why and never.jobs == [] and never.befores == 0,
          "when the preflight fails nothing is staged or run")

    def writes(work):
        (work / "out" / "made.csv").write_text("a\n1\n", encoding="utf-8")
        return 1, b"", b"unshare: unshare failed: Operation not permitted\n"
    ex = FakeExecutor(script=writes, mark=False)
    r = sb.execute("print(1)\n", [], dest, lim, "t2", executor=ex)
    check(not r.started and r.why == "the isolation step failed (unshare: unshare failed: Operation not permitted)",
          "a run whose bootstrap mark never appears is refused with the first thing the isolation said")
    check(not (dest / "made.csv").exists(), "nothing a run without the mark wrote is copied out")
    ex = FakeExecutor(script=lambda w: (97, b"", b"sandbox refused to start: the network is not cut off\n"), mark=False)
    r = sb.execute("print(1)\n", [], dest, lim, "t3", executor=ex)
    check(not r.started and r.why == "its own check inside the sandbox failed (the network is not cut off)",
          "the bootstrap's own refusal comes back as one plain reason")
    ex = FakeExecutor(outcome=sb.Outcome(error="the code could not be started (no such file)"))
    r = sb.execute("print(1)\n", [], dest, lim, "t4", executor=ex)
    check(not r.started and "could not be started" in r.why, "a process that cannot even start is refused")
    ex = FakeExecutor(outcome=sb.Outcome(returncode=None, killed_for="wall", stderr=b""))
    r = sb.execute("print(1)\n", [], dest, lim, "t5", executor=ex)
    check(not r.started and "did not start" in r.why, "a run stopped before its mark is refused, not reported as run")
    ex = FakeExecutor(busy=True)
    r = sb.execute("print(1)\n", [], dest, lim, "t6", executor=ex)
    check(not r.started and "another code run" in r.why and ex.jobs == [],
          "a run while another holds the machine waits its turn, then says so; it never runs beside it")

    ex = FakeExecutor(script=lambda w: (0, b"hello\n", b"a warning\n"))
    r = sb.execute("print('hello')\n", [], dest, lim, "t7", executor=ex)
    job = ex.jobs[0]
    run_dir = Path(job.cwd).parent
    check(r.started and r.stdout == "hello\n" and r.stderr == "a warning\n" and sb.MARK not in r.stderr,
          "with the mark the code ran, and the mark line is taken out of its stderr")
    check(job.argv == sb.command(990, 991, run_dir / "boot.py", run_dir / "code.py") and
          job.env == sb.child_env(Path(job.cwd)) and job.cwd == str(run_dir / "work"),
          "the executor is handed exactly the command and the environment above, in the run's work folder")
    check(ex.seen["boot"] == sb.BOOT and ex.seen["code"] == "print('hello')\n" and ex.seen["out_exists"],
          "the bootstrap and the code are staged beside work/, and out/ exists when the code starts")
    check(run_dir.parent == base and not run_dir.exists() and ex.afters == [run_dir] and ex.befores == 1,
          "the run folder lives under the sandbox folder, never the volume, and is deleted after the run")
    check(job.uid is None, "the stand-in executor is not real, so nothing is chowned and no process is swept")

    for oc, needle, rule in (
            (sb.Outcome(returncode=-24), "used its 120 s of CPU", "the CPU limit"),
            (sb.Outcome(returncode=-25), "passed the 200 MB limit", "the file size limit"),
            (sb.Outcome(returncode=-9), "stopped by the system", "a kill by the system"),
            (sb.Outcome(returncode=-9, killed_for="wall"), "180 s wall clock", "the wall clock"),
            (sb.Outcome(returncode=-9, killed_for="size"), "more than 1024 MB", "the write allowance"),
            (sb.Outcome(returncode=-9, killed_for="files"), "more than 20000 files", "the file count"),
            (sb.Outcome(returncode=3), "exit code 3", "an exit code"),
            (sb.Outcome(returncode=0), "finished", "a clean finish")):
        oc.stderr = (sb.MARK + "\n").encode()
        r = sb.execute("print(1)\n", [], None, lim, "t8", executor=FakeExecutor(outcome=oc))
        check(r.started and needle in r.ended, "%s is said in plain words: %s" % (rule, r.ended))

    def leave(work):
        (work / "out" / "kept.csv").write_text("a\n", encoding="utf-8")
        return 0, b"", b""
    ex = FakeExecutor(script=leave)
    orig = ex.run

    def straggler(job):
        oc = orig(job)
        oc.left_running = 1
        return oc
    ex.run = straggler
    r = sb.execute("print(1)\n", [], dest, lim, "t9", executor=ex)
    check(r.started and not (dest / "kept.csv").exists() and r.skipped and "could not be stopped" in r.skipped[0]["why"],
          "when a process the code started could not be stopped, nothing is copied out")
    folder = root / "not-a-file"
    folder.mkdir(exist_ok=True)
    ex = FakeExecutor()
    r = sb.execute("print(1)\n", [("x.csv", folder)], dest, lim, "t10", executor=ex)
    check(not r.started and ("not a plain file" in r.why or "could not be opened" in r.why) and ex.jobs == [],
          "an input that is not a plain file stops the run before it starts: %s" % r.why)
    check([p.name for p in base.iterdir()] == [], "every run left the sandbox folder empty")


# ------------------------------------------------------------------------------------- inputs
def _inputs(check, store, root: Path) -> None:
    print("  the files an employee may give the code")
    sb = sandbox
    ws = Path(config.WORKSPACES_DIR) / "analysis"
    inbox = ws / "inbox" / "C1-1700000000.000100"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "01-data.csv").write_text("street,n\n1 Invented Way,2\n", encoding="utf-8")
    other = Path(config.WORKSPACES_DIR) / "research" / "inbox"
    other.mkdir(parents=True, exist_ok=True)
    (other / "secret.csv").write_text("x\n", encoding="utf-8")
    early = _task(store, thread="th-inputs")
    now = _task(store, thread="th-inputs")
    (sb.run_dir_for(early) / "exports").mkdir(parents=True, exist_ok=True)
    (sb.run_dir_for(early) / "exports" / "old.csv").write_text("a\n", encoding="utf-8")
    (sb.run_dir_for(now) / "exports").mkdir(parents=True, exist_ok=True)
    (sb.run_dir_for(now) / "exports" / "fresh.csv").write_text("b\n", encoding="utf-8")
    (sb.run_dir_for(now) / "files").mkdir(parents=True, exist_ok=True)
    (sb.run_dir_for(now) / "files" / "chart.png").write_bytes(b"\x89PNG fake")
    wf, _ = store.enqueue("sales", "offer_sheet", "", subject="an offer")
    wf_dir = Path(config.RUNS_DIR) / ("offer_sheet-%d" % wf)
    (wf_dir / "files").mkdir(parents=True, exist_ok=True)
    (wf_dir / "files" / "Offer.xlsx").write_bytes(b"PK fake workbook")
    (wf_dir / "research").mkdir(parents=True, exist_ok=True)
    (wf_dir / "research" / "raw.json").write_text("{}", encoding="utf-8")
    store.finish(wf, "done", result={"run_dir": str(wf_dir)})

    def ok(given, want: Path, rule):
        try:
            got = sb.resolve_input(given, now, "analysis")
        except sb.Refused as exc:
            check(False, "%s: refused (%s)" % (rule, exc))
            return
        check(got == want.resolve(), rule)

    def refused(given, needle, rule):
        try:
            sb.resolve_input(given, now, "analysis")
            check(False, "%s: it was accepted" % rule)
        except sb.Refused as exc:
            check(needle in str(exc), "%s: %s" % (rule, exc))

    ok("inbox/C1-1700000000.000100/01-data.csv", inbox / "01-data.csv", "a Slack file saved in the employee's own "
                                                                       "workspace is an input")
    ok("exports/fresh.csv", sb.run_dir_for(now) / "exports" / "fresh.csv", "this turn's export is an input")
    ok("fresh.csv", sb.run_dir_for(now) / "exports" / "fresh.csv", "an export is found by its bare name too")
    ok("old.csv", sb.run_dir_for(early) / "exports" / "old.csv",
       "an export an earlier turn on the same thread made is found after an ask-back")
    ok("chart.png", sb.run_dir_for(now) / "files" / "chart.png", "an earlier run's output on this turn is an input")
    ok("task:%d/Offer.xlsx" % wf, wf_dir / "files" / "Offer.xlsx", "a task's shared file is an input by task:<id>/<name>")
    ok("https://agents.example.test/files/%d/0123456789abcdef0123456789abcdef/Offer.xlsx" % wf,
       wf_dir / "files" / "Offer.xlsx", "a task's file is an input by the signed link the operator saw in Slack")
    ok(str(inbox / "01-data.csv"), inbox / "01-data.csv", "an absolute path inside the employee's workspace is an input")
    ok(str(wf_dir / "files" / "Offer.xlsx"), wf_dir / "files" / "Offer.xlsx",
       "an absolute path to a run folder's shared file is an input")
    refused("../research/inbox/secret.csv", "climbs out", "a path that climbs out with .. is refused")
    refused("inbox/../../research/inbox/secret.csv", "climbs out", "a .. anywhere in the path is refused")
    refused(str(other / "secret.csv"), "outside the files you may give the code",
            "another employee's workspace is refused")
    refused(str(config.DB_PATH), "outside the files you may give the code", "the brain's database is refused")
    refused(str(wf_dir / "research" / "raw.json"), "outside the files you may give the code",
            "a run folder's working files (not its shared files) are refused")
    refused("task:%d/missing.xlsx" % wf, "has no file", "a task's file that does not exist is refused")
    refused("task:99999/Offer.xlsx", "There is no task", "a task that does not exist is refused")
    refused("task:%d/.." % wf, "file name alone", "a task's file must be named by its file name alone")
    refused("nothing-here.csv", "There is no file", "a file that does not exist anywhere allowed is refused")
    refused("   ", "empty name", "an empty name is refused")
    link = ws / "inbox" / "db-link.csv"
    try:
        os.symlink(str(config.DB_PATH if Path(config.DB_PATH).exists() else other / "secret.csv"), str(link))
        made = True
    except (OSError, NotImplementedError):
        made = False
    if made:
        refused("inbox/db-link.csv", "leads outside", "a link in the workspace that leads outside is refused, "
                                                      "never followed")
    else:
        check(not sb._inside(ws, other / "secret.csv"), "a path that resolves outside the workspace is not inside it "
                                                        "(this machine cannot make links; the box check covers them)")

    got = sb.stage_inputs(now, "analysis", ["inbox/C1-1700000000.000100/01-data.csv", "fresh.csv"])
    check([g["as"] for g in got] == ["01-data.csv", "fresh.csv"] and got[0]["bytes"] > 0,
          "staged inputs keep their file names and sizes")
    (sb.run_dir_for(now) / "exports" / "01-data.csv").write_text("c\n", encoding="utf-8")
    got = sb.stage_inputs(now, "analysis", ["inbox/C1-1700000000.000100/01-data.csv", "exports/01-data.csv"])
    check([g["as"] for g in got] == ["01-data.csv", "01-data-2.csv"], "two inputs with one name get two names in in/")
    check([g["as"] for g in sb.stage_inputs(now, "analysis", "fresh.csv")] == ["fresh.csv"],
          "a single file name is taken as a list of one")
    check(sb.stage_inputs(now, "analysis", None) == [] and sb.stage_inputs(now, "analysis", []) == [],
          "no files means no inputs")
    for files, env, needle, rule in (
            ({"a": 1}, {}, "list of file names", "files that are not a list are refused"),
            (["fresh.csv", "old.csv", "chart.png"], {"AGENTS_SANDBOX_IN_FILES": "2"}, "At most 2 files",
             "more files than one run takes are refused"),
            (["big.csv"], {"AGENTS_SANDBOX_IN_MB": "1"}, "more than the 1 MB", "inputs past the size cap are refused")):
        if files == ["big.csv"]:
            (sb.run_dir_for(now) / "exports" / "big.csv").write_bytes(b"x" * (MB + MB // 2))
        try:
            with _env(**env):
                sb.stage_inputs(now, "analysis", files)
            check(False, rule)
        except sb.Refused as exc:
            check(needle in str(exc), "%s: %s" % (rule, exc))


# ------------------------------------------------------------------------------------- outputs
def _outputs(check, root: Path) -> None:
    print("  what comes back out of out/")
    sb = sandbox
    out = root / "collect-out"
    out.mkdir(parents=True, exist_ok=True)
    (out / "a.csv").write_text("a\n1\n", encoding="utf-8")
    (out / "chart.png").write_bytes(b"\x89PNG fake")
    (out / "My Report (final).XLSX").write_bytes(b"PK fake")
    (out / "run.exe").write_bytes(b"MZ")
    (out / "notes").write_text("no extension", encoding="utf-8")
    (out / "sub").mkdir(exist_ok=True)
    (out / "sub" / "deep.csv").write_text("x\n", encoding="utf-8")
    (out / "h1.txt").write_text("one file, two names", encoding="utf-8")
    try:
        os.link(str(out / "h1.txt"), str(out / "h2.txt"))
        hard = True
    except OSError:
        hard = False
    dest = root / "collect-dest"
    files, skipped = sb.collect(out, dest)
    got = {f["name"]: f for f in files}
    why = {s["name"]: s["why"] for s in skipped}
    check(set(got) == {"a.csv", "chart.png", "My_Report_final.xlsx"} and (dest / "a.csv").read_text() == "a\n1\n",
          "regular files of the kinds the brain serves come back, copied byte for byte")
    check(got["My_Report_final.xlsx"]["was"] == "My Report (final).XLSX",
          "a file name a link cannot carry is made safe, and the original name is kept beside it")
    check("not kept" in why.get("run.exe", "") and "not kept" in why.get("notes", ""),
          "a kind the brain does not serve (an executable, no extension) is skipped with the reason")
    check("folder" in why.get("sub", "") and not (dest / "deep.csv").exists(), "a folder in out/ is skipped, not walked")
    if hard:
        check("hard link" in why.get("h1.txt", "") and "hard link" in why.get("h2.txt", ""),
              "a file with a second name (a hard link) is never copied")
    try:
        os.symlink(str(config.DB_PATH), str(out / "leak.json"))
        sym = True
    except (OSError, NotImplementedError):
        sym = False
    if sym:
        files, skipped = sb.collect(out, root / "collect-dest-2")
        check("leak.json" not in {f["name"] for f in files} and
              "link" in {s["name"]: s["why"] for s in skipped}.get("leak.json", ""),
              "a link the code planted in out/ is skipped, never followed")
    else:
        check(True, "this machine cannot make links; the box check proves a planted link is never followed")
    files, skipped = sb.collect(out, root / "collect-dest-3", uid=12345)
    check(files == [] and all(s["why"] == "not written by the code" for s in skipped
                              if s["name"] in ("a.csv", "chart.png")),
          "a file the sandbox user does not own is never copied, even with an allowed name")
    files, skipped = sb.collect(out, root / "collect-dest-4", max_files=1)
    check(len(files) == 1 and any("past the 1 files" in s["why"] for s in skipped), "past the file count, the rest "
                                                                                    "are skipped and said")
    (out / "z.csv").write_bytes(b"z" * 4096)
    files, skipped = sb.collect(out, root / "collect-dest-5", max_mb=0.002)
    check("z.csv" not in {f["name"] for f in files} and any("past the" in s["why"] and "MB" in s["why"]
                                                           for s in skipped), "past the size cap a file is skipped")
    twin = root / "collect-twin"
    twin.mkdir(exist_ok=True)
    (twin / "a b.csv").write_text("1", encoding="utf-8")
    (twin / "a_b.csv").write_text("2", encoding="utf-8")
    files, _ = sb.collect(twin, root / "collect-dest-6")
    check(sorted(f["name"] for f in files) == ["a_b-2.csv", "a_b.csv"], "two names that come out the same are kept apart")
    files, skipped = sb.collect(root / "no-such-out", root / "collect-dest-7")
    check(files == [] and "removed or replaced out/" in skipped[0]["why"], "a run that removed out/ copies nothing")


# ------------------------------------------------------------------------------------- run_python
def _round_trip(check, store, root: Path) -> None:
    print("  run_python, end to end with a stand-in executor")
    sb = sandbox
    from . import slack
    ws = Path(config.WORKSPACES_DIR) / "analysis" / "inbox" / "C2-1"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "01-list.csv").write_text("street,n\n1 Invented Way,2\n3 Invented Way,5\n", encoding="utf-8")
    task = _task(store, thread="th-rt")

    def code(work):
        rows = (work / "in" / "01-list.csv").read_text(encoding="utf-8").splitlines()[1:]
        total = sum(int(r.split(",")[1]) for r in rows)
        (work / "out" / "summary.csv").write_text("total\n%d\n" % total, encoding="utf-8")
        (work / "out" / "chart.png").write_bytes(b"\x89PNG fake chart")
        return 0, (("x" * 30000) + "\ntotal %d\n" % total).encode(), b"a warning\n"
    ex = FakeExecutor(script=code)
    sb.EXECUTOR = ex
    res = sb.run_code(task, "analysis", "print('total')\n", files=["inbox/C2-1/01-list.csv"])
    files_dir = sb.run_dir_for(task) / "files"
    check(res["ran"] and res["run"] == 1 and res["exit_code"] == 0 and res["ended"] == "finished",
          "a run that finished says so, with its number and exit code")
    check(res["inputs"] == [{"given": "inbox/C2-1/01-list.csv", "as": "in/01-list.csv", "bytes": 46}] and
          ex.seen["in"] == ["01-list.csv"], "the file it was given is in in/ under its own name")
    check((files_dir / "summary.csv").read_text() == "total\n7\n" and (files_dir / "chart.png").is_file(),
          "what the code wrote to out/ is kept in the turn's run folder (files/)")
    links = {f["name"]: f["link"] for f in res["files"]}
    check(links == {"chart.png": slack.file_url(task["id"], "chart.png"),
                    "summary.csv": slack.file_url(task["id"], "summary.csv")} and all(links.values()),
          "each kept file comes back with the brain's signed link for this turn")
    check(len(res["stdout"]) + len(res["stderr"]) <= 20000 and "run-1-output.txt" in res["stdout"] and
          res["stdout"].endswith("total 7\n") and res["stderr"] == "a warning\n",
          "the model gets at most 20,000 characters, with the end kept and the cut named")
    log_text = (files_dir / "run-1-output.txt").read_text(encoding="utf-8")
    check("x" * 30000 in log_text and "a warning" in log_text and res["output_log"]["name"] == "run-1-output.txt",
          "the whole output is kept as a file in the run folder")
    check(any("20000 characters" in n for n in res["notes"]) and "120 s of CPU" in res["limits"],
          "the answer says the output was cut and which limits applied")
    check(sb.turn_result(task) == {"run_dir": str(sb.run_dir_for(task))},
          "the turn's result can carry its run folder, which is how the brain's /files route serves the links")
    check((Path(sb.turn_result(task)["run_dir"]) / "files" / "summary.csv").is_file(),
          "the route's lookup (run_dir, then files/<name>) finds the kept file")
    bare = _task(store, thread="th-rt-2")
    check(sb.turn_result(bare) == {}, "a turn whose code kept nothing adds nothing to its result")
    job = ex.jobs[0]
    check(job.env == sb.child_env(Path(job.cwd)) and job.limits.wall_s == 180 and not Path(job.cwd).exists(),
          "the run used the sandbox's environment and limits, and its work folder is gone")

    ex.script = lambda work: ((work / "out" / "summary.csv").write_text("total\n8\n", encoding="utf-8") and 0, b"ok\n", b"")
    res = sb.run_code(task, "analysis", "print('again')\n", timeout_s=5)
    check(res["run"] == 2 and (files_dir / "run-2-output.txt").is_file() and
          (files_dir / "summary.csv").read_text() == "total\n8\n",
          "a second run on the same turn is run 2, and a file of the same name is the newer one")
    check(ex.jobs[-1].limits.wall_s == 5, "timeout_s shortens the wall clock")
    sb.run_code(task, "analysis", "print(1)\n", timeout_s=99999)
    check(ex.jobs[-1].limits.wall_s == 180, "timeout_s never raises the wall clock past the setting")
    for code_text, kw, env, needle, rule in (
            ("   ", {}, {}, "Give run_python the code", "empty code is refused"),
            ("print(1)\n", {"timeout_s": "soon"}, {}, "number of seconds", "a timeout that is not a number is refused"),
            ("print(1)\n", {"timeout_s": -1}, {}, "above 0", "a timeout of zero or less is refused"),
            ("x = 1\n" * 400, {}, {"AGENTS_SANDBOX_CODE_KB": "1"}, "longer than 1 KB", "code past the size cap is refused")):
        try:
            with _env(**env):
                sb.run_code(task, "analysis", code_text, **kw)
            check(False, rule)
        except sb.Refused as exc:
            check(needle in str(exc), "%s: %s" % (rule, exc))
    sb.EXECUTOR = FakeExecutor(outcome=sb.Outcome(returncode=0, stdout=b"the first part\n", stdout_total=9 * MB,
                                                  stderr=(sb.MARK + "\n").encode(), stderr_total=len(sb.MARK) + 1))
    res = sb.run_code(task, "analysis", "print(1)\n")
    log_text = (files_dir / res["output_log"]["name"]).read_text(encoding="utf-8")
    check(log_text.startswith("The code wrote more stdout than this file keeps (the first 5 MB of each)") and
          "the first part" in log_text, "when the code printed more than the log keeps, the log says so at the top")
    sb.EXECUTOR = FakeExecutor(ready=(False, "it runs only on the brain's Linux machine, and this one is win32"))
    try:
        sb.run_code(task, "analysis", "print(1)\n")
        check(False, "an unavailable sandbox refuses")
    except sb.Refused as exc:
        check(str(exc) == "The code sandbox is not available on this machine: it runs only on the brain's Linux "
                          "machine, and this one is win32. Nothing ran.", "an unavailable sandbox answers one plain "
                                                                         "sentence saying why, and runs nothing")
    sb.EXECUTOR = FakeExecutor(script=lambda work: ((work / "out" / "a.csv").write_text("a", encoding="utf-8") and 0,
                                                    b"", b""))
    saved = config.PUBLIC_URL
    config.PUBLIC_URL = ""
    try:
        res = sb.run_code(task, "analysis", "print(1)\n")
    finally:
        config.PUBLIC_URL = saved
    check(res["files"][0]["link"] == "" and any("no public address" in n for n in res["notes"]),
          "without a public address the files are kept and the answer says they have no links")
    sb.EXECUTOR = None


# ------------------------------------------------------------------------------------- exports
def _csv_rows(path: Path) -> list[dict]:
    import csv
    with open(str(path), encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _records(check, store, query, root: Path) -> None:
    print("  export_data: records")
    sb = sandbox
    fake = FakeCRM()
    query.CRM = fake
    query.reset()
    task = _task(store, thread="th-export")
    for i in range(3):
        with store._LOCK, store._db() as c:
            c.execute("INSERT INTO ap_log (id, at, event_type, resource, resource_type, source, author, data) "
                      "VALUES (?,?,?,?,?,?,?,?)", ("created-%d" % i, "2026-11-01T15:00:00+00:00", "property.created",
                                                   U(1001 + i), "property", "internal-api", "system", "{}"))
    res = sb.export(task, {"source": "records", "lists": ["Probate"], "max_rows": 520, "name": "probate list!"})
    path = sb.run_dir_for(task) / "exports" / "probate_list.csv"
    rows = _csv_rows(path)
    text = path.read_text(encoding="utf-8")
    check(res["file"] == "exports/probate_list.csv" and res["rows"] == 520 and res["count"] == 600 and len(rows) == 520,
          "a records export writes the rows asked for and says how many match")
    check(list(rows[0]) == sb.RECORD_COLUMNS, "the export's columns are the fixed record columns")
    check([(s["limit"], s["offset"]) for s in fake.searches] == [(250, 0), (250, 250), (20, 500)],
          "the export pages the records search 250 at a time and stops at the rows asked for")
    check(all(s["must"].get("property_type") == "clean" and s["must"].get("any_lists") == [LISTS["Probate"]]
              for s in fake.searches), "every page carries the same filter, built by query.py's helpers")
    leaked = [x for x in [PHONE(i) for i in range(1, 601)] + [PHONE(i)[2:] for i in range(1, 601)] +
              [EMAIL(i) for i in range(1, 601)] if x in text]
    check(not leaked and "@" not in text.replace("'=HYPERLINK", ""),
          "no phone number and no email from any row reaches the export")
    check(not any("phone" in c and c != "has_phones" for c in rows[0]) and not any("mail" in c and c not in (
        "mail_attempts", "do_not_mail") for c in rows[0]), "no column carries a number or an address for mail")
    check(rows[1]["owner"] == "Quincy Testowner2" and rows[1]["has_phones"] == "True" and rows[0]["has_phones"] == "False",
          "the owner's name and whether the owner has a number are there")
    check(rows[0]["owner"].startswith("'=HYPERLINK"), "a cell that would be a formula in a workbook is made plain text")
    check([r["created"] for r in rows[:4]] == ["2026-11-01", "2026-11-01", "2026-11-01", ""] and
          any("reaches back to 2026-11-01" in n for n in res["notes"]),
          "created comes from the brain's log copy, blank where it does not reach, and the note says how far back")
    check(any("No phone number or email" in n for n in res["notes"]) and res["requests"] == len(fake.gets) +
          len(fake.searches), "the answer says what is never exported and counts every request it made")
    check(fake.writes == [], "an export never writes to DataSift")
    check(sb.resolve_input("exports/probate_list.csv", task, "analysis") == path.resolve(),
          "the export's file name is what run_python takes")

    fake.searches.clear()
    res = sb.export(task, {"source": "records", "preset": "Probate - No Phone", "lists": ["Foreclosure"]})
    want = [r for r in fake.recs if not r["phones"] and LISTS["Foreclosure"] in r["lists"]]
    must = fake.searches[0]["must"]
    check(res["count"] == len(want) and res["rows"] == len(want) and must.get("all_lists") == [LISTS["Foreclosure"]]
          and must.get("phone") == 0 and res["applied"]["preset"] == "Probate - No Phone",
          "a preset and a filter together export the records that meet both")
    with _env(AGENTS_EXPORT_MAX_ROWS="100"):
        res = sb.export(task, {"source": "records", "lists": ["Probate"], "max_rows": 500})
    check(res["rows"] == 100 and any("at most 100" in n for n in res["notes"]),
          "max_rows past the setting is cut to it, and the answer says so")
    fake.gets.clear()
    fake.missing = {U(1003)}
    with _env(AGENTS_EXPORT_DETAIL_MAX="5"):
        res = sb.export(task, {"source": "records", "lists": ["Probate"], "max_rows": 10, "with_titles": True,
                               "name": "titled"})
    rows = _csv_rows(sb.run_dir_for(task) / "exports" / "titled.csv")
    text = (sb.run_dir_for(task) / "exports" / "titled.csv").read_text(encoding="utf-8")
    details = [g for g in fake.gets if g.startswith("/api/internal/property/")]
    check(len(details) == 5 and rows[0]["lists"] == "Probate" and rows[2]["lists"] == "" and rows[3]["tags"] == "FTM"
          and rows[5]["lists"] == "", "with_titles reads list and tag titles for the first rows only, and a record "
                                      "gone since the search is a blank, not a failure")
    check("detail" not in text and PHONE(501)[2:] not in text, "a record's detail gives titles only, never its phones "
                                                               "or emails")
    fake.missing = set()
    for args, needle, rule in (
            ({"source": "records", "zip": "37990"}, "no ZIP, city or county filter", "a filter the search cannot "
                                                                                    "express is refused with query.py's hint"),
            ({"source": "records", "what": "calls"}, "has no input what", "an input of another source is refused"),
            ({"source": "people"}, "needs source", "an unknown source is refused"),
            ({}, "needs source", "an export without a source is refused"),
            ({"source": "records", "max_rows": 0}, "at least 1", "max_rows of 0 is refused"),
            ({"source": "records", "max_rows": "many"}, "whole number", "max_rows that is not a number is refused"),
            ({"source": "records", "lists": ["No Such List"]}, "No list in this account", "an unknown list is refused")):
        try:
            sb.export(task, args)
            check(False, rule)
        except sb.Refused as exc:
            check(needle in str(exc), "%s: %s" % (rule, exc))
    check(sb.export(task, {"source": "records", "lists": ["Probate"], "max_rows": 3, "with_titles": False})["rows"] == 3,
          "with_titles false is simply the default")
    fake.fail = FakeApiError(500, "upstream down")
    try:
        sb.export(task, {"source": "records", "lists": ["Probate"]})
        check(False, "a DataSift failure is refused")
    except sb.Refused as exc:
        check("DataSift refused the request (HTTP 500)" in str(exc), "a DataSift failure comes back as one sentence: "
                                                                     "%s" % exc)
    fake.fail = None
    fake.searches.clear()
    with _env(AGENTS_QUERY_TOOLS="0"):
        try:
            sb.export(task, {"source": "records", "lists": ["Probate"]})
            check(False, "records exports are off with the query tools")
        except sb.Refused as exc:
            check("off on this install" in str(exc) and fake.searches == [], "with the query tools off, a records "
                                                                             "export is refused before any request")
    reads = sb._export_reads(query, fake, cap=1)
    reads.page({"any_lists": [LISTS["Probate"]]}, 1, 0)
    try:
        reads.page({"any_lists": [LISTS["Probate"]]}, 1, 0)
        check(False, "the per-export request cap holds")
    except query.Refused:
        check(True, "one export stops at its own request cap, counted by query.py's reader")


def _log_and_tables(check, store, root: Path) -> None:
    print("  export_data: the log copy and the brain's tables")
    sb = sandbox
    from .autopilot import ledger
    task = _task(store, thread="th-tables")
    owner = U(6001)
    events = [
        ("c1", "2026-11-12T16:00:00+00:00", "owner.call.made", owner, "owner",
         {"number": "8650000150", "line": "8650000999", "direction": "outbound", "duration": 40, "caller": "Adriana",
          "caller_uuid": U(801), "recording": "https://rec.example.test/c1.mp3", "status": ""}),
        ("c2", "2026-11-13T03:30:00+00:00", "owner.call.answered", owner, "owner",
         {"number": "8650000150", "direction": "outbound", "duration": 95, "caller": "Adriana",
          "recording": "https://rec.example.test/c2.mp3"}),
        ("s1", "2026-11-12T17:00:00+00:00", "property.status.updated", U(1001), "property",
         {"from": "", "to": "new_lead", "by": "Call New Lead"}),
        ("p1", "2026-11-12T18:00:00+00:00", "owner.phone.status.updated", owner, "owner",
         {"number": "8650000150", "from": "UNKNOWN", "to": "CORRECT"}),
        ("t1", "2026-11-12T19:00:00+00:00", "task.created", U(1001), "task",
         {"task": U(7001), "title": "Call 865-000-0151 back", "kind": "task", "due": "2026-11-14"}),
    ]
    with store._LOCK, store._db() as c:
        for eid, at, et, res_id, rtype, data in events:
            c.execute("INSERT INTO ap_log (id, at, event_type, resource, resource_type, source, author, data) "
                      "VALUES (?,?,?,?,?,?,?,?)", (eid, at, et, res_id, rtype, "kixie", "system", json.dumps(data)))
    res = sb.export(task, {"source": "log", "what": "calls", "since": "7-days", "name": "calls"})
    path = sb.run_dir_for(task) / "exports" / "calls.csv"
    rows, text = _csv_rows(path), path.read_text(encoding="utf-8")
    check(res["rows"] == 2 and {r["event_type"] for r in rows} == {"owner.call.made", "owner.call.answered"},
          "a log export of calls holds the calls of the range, from the brain's own copy")
    check("8650000150" not in text and "8650000999" not in text and "rec.example.test" not in text and
          "line" not in rows[0] and "recording" not in rows[0] and "number" not in rows[0],
          "no phone number, no line dialed from and no recording leaves the log copy")
    check(rows[0]["caller"] == "Adriana" and rows[0]["direction"] == "outbound" and rows[0]["duration"] == "40",
          "who dialed, the direction and the duration are kept")
    check([r["day"] for r in rows] == ["2026-11-12", "2026-11-12"],
          "the day is the business-local day (a call at 03:30 UTC is the evening before in Eastern time)")
    res = sb.export(task, {"source": "log", "since": "7-days", "name": "everything"})
    rows = _csv_rows(sb.run_dir_for(task) / "exports" / "everything.csv")
    titles = [r["title"] for r in rows if r["event_type"] == "task.created"]
    check(res["rows"] == 5 and titles == ["Call [phone] back"], "the default exports every event, with free text "
                                                                 "scrubbed of numbers")
    res = sb.export(task, {"source": "log", "what": "phone_status", "since": "7-days", "name": "ps"})
    rows = _csv_rows(sb.run_dir_for(task) / "exports" / "ps.csv")
    check(res["rows"] == 1 and rows[0]["from"] == "UNKNOWN" and rows[0]["to"] == "CORRECT" and
          "8650000150" not in (sb.run_dir_for(task) / "exports" / "ps.csv").read_text(encoding="utf-8"),
          "a phone status change keeps the statuses and drops the number")
    for args, needle, rule in (({"source": "log", "what": "gossip"}, "what is one of", "an unknown kind of event is "
                                                                                       "refused"),
                               ({"source": "log", "since": "today", "until": "7-days"}, "since is after until",
                                "a range that runs backwards is refused"),
                               ({"source": "log", "since": "the other day"}, "is not a date", "a date that is not a "
                                                                                             "date is refused")):
        try:
            sb.export(task, args)
            check(False, rule)
        except sb.Refused as exc:
            check(needle in str(exc), "%s: %s" % (rule, exc))

    t2, _ = store.enqueue("ops", "kpi_report", "", subject="Call Pat at 865-000-0199",
                          payload={"details": "keep 865-000-0188 and pat.payload@example.com private"})
    store.finish(t2, "failed", result={"secret": "result 865-000-0177"}, error="failed for pat@example.com")
    store.add_spend(t2, 0.25, "claude test")
    res = sb.export(task, {"source": "tasks", "since": "7-days", "status": "failed", "name": "tasks"})
    path = sb.run_dir_for(task) / "exports" / "tasks.csv"
    rows, text = _csv_rows(path), path.read_text(encoding="utf-8")
    mine = [r for r in rows if r["id"] == str(t2)]
    check(res["rows"] >= 1 and all(r["status"] == "failed" for r in rows) and mine and mine[0]["usd"] == "0.25",
          "a tasks export filters by status and carries what each task cost")
    check(mine[0]["subject"] == "Call Pat at [phone]" and mine[0]["error"] == "failed for [email]",
          "a task's free text is scrubbed of numbers and emails")
    check("865-000-0188" not in text and "865-000-0177" not in text and "payload" not in text,
          "a task's message and result are never exported")
    ledger.event("run-1", "ftm_texts", U(1002), "text",
                 {"numbers": ["8650000177", "8650000178"], "note": "texted 865-000-0179", "owner": U(5002),
                  "series": "ftm", "phone": "8650000176"}, usd=0.0, now=NOW)
    res = sb.export(task, {"source": "stage_events", "since": "7-days", "stage": "ftm_texts", "name": "events"})
    path = sb.run_dir_for(task) / "exports" / "events.csv"
    rows, text = _csv_rows(path), path.read_text(encoding="utf-8")
    detail = json.loads(rows[0]["detail"])
    check(res["rows"] == 1 and detail.get("numbers") == 2 and detail.get("phone") == "[removed]" and
          detail.get("note") == "texted [phone]" and detail.get("owner") == U(5002),
          "a stage event's numbers are counted, a phone is removed, free text is scrubbed and ids stay whole")
    check("8650000177" not in text and "8650000176" not in text, "no number from a stage event reaches the export")
    with store._LOCK, store._db() as c:
        c.execute("INSERT INTO ap_batches (created, stage, kind, status, items, usd) VALUES (?,?,?,?,?,?)",
                  (NOW.isoformat(timespec="seconds"), "ftm_texts", "text", "staged",
                   json.dumps([{"record": U(1001), "number": "8650000166", "body": "hi"},
                               {"record": U(1002), "number": "8650000167", "body": "hi"}]), 0.0))
    res = sb.export(task, {"source": "batches", "since": "7-days", "name": "batches"})
    path = sb.run_dir_for(task) / "exports" / "batches.csv"
    rows, text = _csv_rows(path), path.read_text(encoding="utf-8")
    check(res["rows"] == 1 and rows[0]["items"] == "2" and "8650000166" not in text and "body" not in text,
          "a staged batch shows how many items it holds and never the items")
    try:
        sb.export(task, {"source": "batches", "workflow": "ask"})
        check(False, "a table export refuses another table's input")
    except sb.Refused as exc:
        check("has no input workflow" in str(exc), "a table export refuses another table's input: %s" % exc)

    check(sb._scrub("(865) 000-0123, 865.000.0124 and +1 865 000 0125 or pat@example.com") ==
          "[phone], [phone] and [phone] or [email]", "every common way of writing a number, and an email, is scrubbed")
    check(sb._scrub_value(U(42)) == U(42) and sb._scrub_value("0123456789abcdef0123456789abcdef") ==
          "0123456789abcdef0123456789abcdef", "an id is never scrubbed, even when it holds ten digits in a row")
    check(sb._clean({"a": {"b": [{"mobile": "8650000101", "count": 3}]}, "numbers": 4, "email": 8650000102}) ==
          {"a": {"b": [{"mobile": "[removed]", "count": 3}]}, "numbers": 4, "email": "[removed]"},
          "a sensitive key is emptied at any depth; a small count under it stays, a number-sized value goes")


# ------------------------------------------------------------------------------------- the tools
def _tools(check, store, root: Path) -> None:
    print("  the two tools")
    sb = sandbox
    task = _task(store, thread="th-tools")
    ctx = SimpleNamespace(task=task, employee=SimpleNamespace(id="analysis", display_name="Atlas"))
    tools = sb.sandbox_tools(ctx)
    by = {t.name: t for t in tools}
    check(list(by) == ["run_python", "export_data"], "every employee gets run_python and export_data")
    rp, ed = by["run_python"], by["export_data"]
    check(rp.schema["required"] == ["code"] and rp.schema["additionalProperties"] is False and
          set(rp.schema["properties"]) == {"code", "files", "timeout_s"}, "run_python takes code, files and timeout_s")
    props = set(ed.schema["properties"])
    check(ed.schema["required"] == ["source"] and {"preset", "lists", "has_phone", "max_rows", "with_titles", "what",
                                                   "since"} <= props and "rows" not in props,
          "export_data takes the source, records_query's own filters (not its rows) and the log and table inputs")
    for t, needles in ((rp, ("no network", "no key", "120 s of CPU", "180 s wall clock", "2 GB")),
                       (ed, ("2000 rows", "never a phone number or email"))):
        sentences = [s for s in t.description.replace("e.g.", "eg").split(". ") if s.strip()]
        check(all(n in t.description for n in needles) and len(sentences) <= 2,
              "%s says its limits in its description, in two sentences" % t.name)
    check(rp.read_only is False and ed.read_only is True, "export_data changes nothing in the account; run_python "
                                                          "writes files, so it is not marked read-only")
    with _env(AGENTS_SANDBOX="0"):
        check(sb.sandbox_tools(ctx) == [], "AGENTS_SANDBOX=0 takes both tools off every employee")

    sb.EXECUTOR = FakeExecutor(script=lambda work: (0, b"42\n", b""))
    out = asyncio.run(rp.handler({"code": "print(42)\n"}))
    body = json.loads(_text_of(out))
    check(not out.get("is_error") and body["ran"] and body["stdout"] == "42\n", "run_python answers inside the turn "
                                                                                "with the code's output")
    sb.EXECUTOR = FakeExecutor(ready=(False, "there is no 'sandbox' user on this machine (the image adds it with "
                                             "useradd)"))
    out = asyncio.run(rp.handler({"code": "print(42)\n"}))
    check(out.get("is_error") and _text_of(out).startswith("The code sandbox is not available on this machine: there "
                                                          "is no 'sandbox' user"),
          "on a machine without the isolation, run_python answers one sentence and nothing runs")
    out = asyncio.run(rp.handler({"code": "print(1)\n", "files": ["../../etc/passwd"]}))
    check(out.get("is_error") and "climbs out" in _text_of(out), "a refused input comes back as the tool's error")
    sb.EXECUTOR = None
    out = asyncio.run(ed.handler({"source": "tasks", "since": "7-days"}))
    body = json.loads(_text_of(out))
    check(not out.get("is_error") and body["file"] == "exports/tasks.csv" and "in/tasks.csv" in body["use"],
          "export_data answers with the file name to hand run_python and where the code finds it")
    out = asyncio.run(ed.handler({"source": "nowhere"}))
    check(out.get("is_error") and "needs source" in _text_of(out), "a refused export comes back as the tool's error")


def _hygiene(check) -> None:
    here = Path(__file__).resolve().parent
    for name in ("sandbox.py", "selftest_sandbox.py"):
        raw = (here / name).read_bytes()
        text = raw.decode("utf-8")
        check(chr(0x2014) not in text and chr(0x2013) not in text and chr(8) not in text and max(raw) < 128,
              "%s is plain ASCII: no em or en dash and no backspace byte" % name)

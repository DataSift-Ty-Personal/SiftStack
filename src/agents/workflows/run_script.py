"""run_script: one SiftStack command line, run on the brain or staged for a person to release.

Quinn owns it (team/roster.yaml); every employee reaches it through the run_script tool (tools.py),
which checks the command against the catalog (agents/commands.py) and queues this task on the same
thread. The task checks the command again here, because the tool's check rested on what a model
sent and this one rests on the source tree as it is now:

  run       python src/<script>.py <args>, as a list (no shell), from the repo root (/app on the
            box), stdin closed, a time limit (AGENTS_SCRIPT_TIMEOUT_MIN, 20), and an environment
            with no paid vendor key, no Slack token and no outreach credential in it, in dry mode
            (child_env). Output goes to a log file in the run folder; the last lines are posted,
            with phone numbers, emails and any secret value masked, and every file the script left
            under output/ is copied into the run folder and linked.
  stage     a command that writes or pays becomes a batch (ap_batches, kind "command") holding the
            exact command line. For a paid script with a vetted free dry run, that dry run runs
            first, with the keys removed, and its price rides on the batch.
  release   a named person's `release batch N` then `confirm N` (agents/release.py) calls
            release() below, which queues the command as its own task here; that task runs it once
            with the full environment, on a live brain only.

Why the process is started by hand instead of subprocess.run: run() kills only the child on a
timeout, so a script that started children of its own (a browser, a worker pool) would leave them
running past the limit. Popen in a new session lets the whole group be stopped.

The brain has ONE worker thread. A script holds it until it ends, so every other task (a Slack
ask, the clock's stages, a webhook's deep prospect) waits behind it. Hence one script at a time,
the time limit, and the tool asking for long runs only when someone needs them.
"""
from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from .. import commands, config, slack, store

NAME = commands.WORKFLOW
KIND = commands.BATCH_KIND
RUNNER = None        # the selftest injects a fake: (argv, cwd, env, timeout_s, log_path) -> {"rc", "timed_out", "seconds"}

# Environment names a run that is not released never carries: paid vendors, outreach, Slack, the
# password path, other accounts' tokens. If the catalog missed a paid path, the vendor call fails.
SCRUB = ("TRESTLE", "SMARTSKIP", "TRACERFY", "ENFORMION", "ENDATO", "SCRAPFLY", "OPENWEBNINJA", "ANTHROPIC",
         "OPENROUTER", "OPENAI", "SERPAPI", "SERPER", "FIRECRAWL", "CAPTCHA", "APIFY", "SLACK", "SMRTPHONE",
         "SMARTER_CONTACT", "SMARTY", "DROPBOX", "GOOGLE", "ANCESTRY", "PAXSUB", "TNPN", "VISUAL_DISTRESS",
         "DATASIFT_EMAIL", "DATASIFT_PASSWORD", "REISIFT_STAFF_JWT", "REISIFT_TARGET_JWT", "SIFTMAP_BEARER",
         "AGENTS_WEBHOOK_SECRET", "FTM_HEALTH_WEBHOOK", "FLY_API", "GITHUB", "GH_TOKEN", "AWS_", "TWILIO",
         "TELNYX", "KIXIE", "DEEPGRAM")
SECRET_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|PASSWD|JWT|AUTH|CREDENTIAL", re.I)
PHONE_RX = re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)")
EMAIL_RX = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
QUOTE_RX = re.compile(r"(?i)(?:est(?:imate[ds]?)?|would (?:cost|spend|pay)|cost|quote[d]?|ceiling|spend)"
                      r"[^$\n]{0,40}\$\s?([0-9][0-9,]*(?:\.[0-9]{1,2})?)")
# The vetted free dry runs (commands.FREE_DRY_RUN) whose price is a count times a known unit price.
QUOTE_UNITS = {"obituary_owner_phones": (re.compile(r"--commit to score (\d+) numbers"), 0.015)}
SNAP_MAX = 50000


def _n(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


def slack_chars() -> int:
    return int(_n("AGENTS_SCRIPT_SLACK_CHARS", 2500))


def log_max_bytes() -> int:
    return int(_n("AGENTS_SCRIPT_LOG_MB", 5.0) * 1024 * 1024)


def max_files() -> int:
    return int(_n("AGENTS_SCRIPT_MAX_FILES", 20))


def file_max_bytes() -> int:
    return int(_n("AGENTS_SCRIPT_FILE_MB", 25.0) * 1024 * 1024)


# ------------------------------------------------------------------------------------ the process

def child_env(scrub: bool) -> dict:
    """The script's environment. Not released: no vendor key, no Slack, no outreach, dry mode. A
    released run keeps the brain's own environment, because a person said yes to exactly that."""
    env = dict(os.environ)
    if scrub:
        for k in list(env):
            up = k.upper()
            if any(s in up for s in SCRUB):
                env.pop(k, None)
        env["AGENTS_MODE"] = "dry"
        env["SMS_AGENT_DRY_RUN"] = "1"
    src = str(commands.script_root() / "src")
    env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _kill(proc) -> None:
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _run_process(argv: list[str], cwd: Path, env: dict, timeout: float, log_path: Path) -> dict:
    t0 = time.monotonic()
    kw: dict = {"cwd": str(cwd), "env": env, "stdin": subprocess.DEVNULL, "stderr": subprocess.STDOUT, "shell": False}
    if os.name == "posix":
        kw["start_new_session"] = True
    else:
        kw["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    timed_out = False
    with open(log_path, "wb") as fh:
        proc = subprocess.Popen(argv, stdout=fh, **kw)
        try:
            rc = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill(proc)
            try:
                rc = proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                rc = -9
    return {"rc": rc, "timed_out": timed_out, "seconds": round(time.monotonic() - t0, 1)}


# ------------------------------------------------------------------------------------ what comes back

def _secrets() -> list[str]:
    vals = []
    for k, v in os.environ.items():
        if v and len(v) >= 8 and SECRET_NAME.search(k):
            vals.append(v)
    return sorted(set(vals), key=len, reverse=True)


def redact(text: str, people: bool = True) -> str:
    """Secrets always; phone numbers and emails too when the text goes to Slack or a model."""
    out = text or ""
    for v in _secrets():
        out = out.replace(v, "[secret]")
    if people:
        out = PHONE_RX.sub("[phone]", out)
        out = EMAIL_RX.sub("[email]", out)
    return out


def _cap_log(p: Path) -> bool:
    """Keep a runaway log readable: the first fifth and the last part, with a line between. Secret
    values are taken out of the file the link serves."""
    try:
        raw = p.read_bytes()
    except OSError:
        return False
    cut = len(raw) > log_max_bytes()
    if cut:
        head, tail = log_max_bytes() // 5, log_max_bytes() - log_max_bytes() // 5
        raw = raw[:head] + b"\n... (%d bytes cut) ...\n" % (len(raw) - head - tail) + raw[-tail:]
    text = raw.decode("utf-8", errors="replace")
    p.write_text(redact(text, people=False), encoding="utf-8", newline="")   # the script's own line ends, kept
    return cut


def _tail(text: str, chars: int) -> str:
    t = (text or "").rstrip()
    if len(t) <= chars:
        return t
    t = t[-chars:]
    return t.split("\n", 1)[-1] if "\n" in t else t


def _excluded(root: Path) -> list[Path]:
    out = []
    for p in (config.DATA_DIR, config.RUNS_DIR):
        try:
            out.append(Path(p).resolve())
        except OSError:
            pass
    return out


def output_dirs() -> list[Path]:
    dirs = [commands.script_root() / "output"]
    extra = os.environ.get("SIFTSTACK_OUTPUT_DIR") or ""
    if extra:
        dirs.append(Path(extra))
    seen, out = set(), []
    for d in dirs:
        try:
            r = d.resolve()
        except OSError:
            continue
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


def snapshot(dirs: list[Path]) -> tuple[dict, bool]:
    """(path -> (mtime_ns, size), complete) for every file under the output folders, the brain's own
    data folder left out (on a workstation it sits inside output/)."""
    skip = _excluded(commands.script_root())
    out, n = {}, 0
    for d in dirs:
        if not d.is_dir():
            continue
        for base, subdirs, files in os.walk(d):
            b = Path(base)
            subdirs[:] = [s for s in subdirs if s != "__pycache__" and not any(
                (b / s).resolve() == x or x in (b / s).resolve().parents for x in skip)]
            for f in files:
                n += 1
                if n > SNAP_MAX:
                    return out, False
                p = b / f
                try:
                    st = p.stat()
                except OSError:
                    continue
                out[str(p)] = (st.st_mtime_ns, st.st_size)
    return out, True


def _flat(rel: str, taken: set[str]) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", rel.replace("\\", "/").replace("/", "__")).strip("._") or "file"
    if len(name) > 150:
        stem, dot, ext = name.rpartition(".")
        name = (stem[:140] + "." + ext) if dot and len(ext) <= 8 else name[:150]
    base, i = name, 2
    while name in taken:
        stem, dot, ext = base.rpartition(".")
        name = ("%s-%d.%s" % (stem, i, ext)) if dot else "%s-%d" % (base, i)
        i += 1
    taken.add(name)
    return name


def collect(before: dict, after: dict, dirs: list[Path], files_dir: Path, taken: set[str]) -> tuple[list[str], list[str]]:
    """Copy each file the run created or changed into the run folder. (copied names, skipped notes)."""
    changed = sorted(p for p, sig in after.items() if before.get(p) != sig)
    copied, skipped = [], []
    for p in changed:
        src = Path(p)
        rel = None
        for d in dirs:
            try:
                rel = src.relative_to(d).as_posix()
                break
            except ValueError:
                continue
        rel = rel or src.name
        size = after[p][1]
        if len(copied) >= max_files():
            skipped.append("%s (over the %d-file limit)" % (rel, max_files()))
            continue
        if size > file_max_bytes():
            skipped.append("%s (%.0f MB, over the limit)" % (rel, size / 1048576))
            continue
        name = _flat(rel, taken)
        try:
            shutil.copy2(src, files_dir / name)
            copied.append(name)
        except OSError as exc:
            skipped.append("%s (%s)" % (rel, str(exc)[:60]))
    return copied, skipped


def _folder(task: dict) -> Path:
    d = config.RUNS_DIR / ("%s-%d" % (NAME, int(task["id"])))
    (d / "files").mkdir(parents=True, exist_ok=True)
    return d


def execute(task: dict, d: commands.Decision, *, scrub: bool, tag: str = "") -> dict:
    """Run one validated command. Never raises for the script's own failure: the exit code says it."""
    return _execute(task, d, scrub=scrub, tag=tag)[0]


def _execute(task: dict, d: commands.Decision, *, scrub: bool, tag: str = "") -> tuple[dict, str]:
    """(result, the log's text). The text never goes into the result: the result is stored on the
    task, and the thread history hands a stored result's head to the next employee turn."""
    folder = _folder(task)
    files_dir = folder / "files"
    taken = {p.name for p in files_dir.iterdir()} if files_dir.is_dir() else set()
    log_name = _flat("%s%s-log.txt" % (d.script, ("-" + tag) if tag else ""), taken)
    log_path = files_dir / log_name
    dirs = output_dirs()
    before, complete = snapshot(dirs)
    argv = [sys.executable, "src/%s.py" % d.script] + list(d.argv)
    runner = RUNNER or _run_process
    out = runner(argv, commands.script_root(), child_env(scrub), commands.timeout_s(), log_path)
    cut = _cap_log(log_path)
    after, complete2 = snapshot(dirs)
    copied, skipped = (collect(before, after, dirs, files_dir, taken) if complete and complete2
                       else ([], ["the output folder holds more than %d files, so new ones were not looked for" % SNAP_MAX]))
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    rc, timed_out = out.get("rc"), bool(out.get("timed_out"))
    last = next((ln.strip() for ln in reversed(text.splitlines()) if ln.strip()), "")
    state = ("stopped at the %d-minute limit" % round(commands.timeout_s() / 60) if timed_out
             else "exit %s in %s s" % (rc, out.get("seconds")))
    return {"run_dir": str(folder), "script": d.script, "command": d.line, "argv": list(d.argv), "exit": rc,
            "timed_out": timed_out, "seconds": out.get("seconds"), "log": log_name, "log_cut": cut,
            "files": copied, "skipped_files": skipped, "scrubbed": scrub,
            "tail": redact(_tail(text, slack_chars())),
            "summary": redact("%s: %s%s. Last line: %s" % (d.line, state, (", %d file(s)" % len(copied)) if copied else "",
                                                              last[:200]))[:500]}, text


# ------------------------------------------------------------------------------------ stage and release

def quote(script: str, text: str) -> float | None:
    unit = QUOTE_UNITS.get(script)
    if unit:
        hits = unit[0].findall(text or "")
        return round(int(hits[-1]) * unit[1], 2) if hits else None
    vals = [float(v.replace(",", "")) for v in QUOTE_RX.findall(text or "")]
    return vals[-1] if vals else None


def stage(task: dict, d: commands.Decision) -> dict:
    from ..autopilot import ledger
    p = store.payload_of(task)
    q_usd, q_note, q = None, "", {}
    if d.quote is not None:
        q, q_text = _execute(task, commands.Decision("run", d.script, argv=d.quote), scrub=True, tag="dry-run")
        if q.get("exit") == 0 and not q.get("timed_out"):
            q_usd = quote(d.script, q_text)
            q_note = ("its dry run prices it at about $%.2f" % q_usd) if q_usd is not None else \
                "its dry run named no price"
        else:
            q_note = "its dry run did not finish (%s), so the price is not known" % (
                "time limit" if q.get("timed_out") else "exit %s" % q.get("exit"))
    item = {"script": d.script, "args": list(d.argv), "command": d.line, "why": d.say, "paid": list(d.paid),
            "asked_by": p.get("requested_by") or "", "details": (p.get("details") or "")[:300],
            "thread_id": task.get("thread_id") or "", "slack_channel": task.get("slack_channel") or "",
            "slack_thread": task.get("slack_thread") or "", "task": int(task["id"]), "quote_usd": q_usd,
            "quote_note": q_note}
    bid = ledger.batch_create(NAME, KIND, [item], float(q_usd or 0))
    out = {"staged": bid, "script": d.script, "command": d.line, "why": d.say, "quote_usd": q_usd, "quote_note": q_note,
           "summary": redact("%s staged as batch %d: %s." % (d.line, bid, d.say))[:500]}
    if q:
        out.update({"run_dir": q["run_dir"], "files": [q["log"]] + list(q.get("files") or [])})
    return out


def release(batch_id: int, *, commit: bool = False, **kw) -> dict:
    """agents/release.py calls this for a command batch. It queues the command as its own task (so the
    run has its own folder, its own time limit and its served files); that task runs it once."""
    from ..autopilot import ledger
    b = ledger.batch(batch_id)
    if not b:
        raise SystemExit("no batch %d" % batch_id)
    if b["kind"] != KIND:
        raise SystemExit("batch %d is a %s batch" % (batch_id, b["kind"]))
    if b["status"] != "staged":
        raise SystemExit("batch %d is %s, not staged" % (batch_id, b["status"]))
    items = b.get("items") or []
    if len(items) != 1:
        raise SystemExit("batch %d holds %d commands; a command batch holds one" % (batch_id, len(items)))
    it = items[0]
    d = commands.decide(it.get("script") or "", it.get("args") or [])
    if d.kind == "refuse":
        if commit:
            ledger.batch_set(batch_id, "refused", {"problems": [d.say]})
        return {"batch": batch_id, "results": {"command refused at release": 1}, "problems": [d.say]}
    if not commit:
        return {"batch": batch_id, "dry": True, "command": d.line, "why": it.get("why") or d.say}
    if not config.live():
        return {"batch": batch_id, "results": {"command not run, because the brain is in dry mode": 1}, "problems": []}
    tid, _ = store.enqueue(_owner(), NAME, "", subject=d.line[:120], payload={"action": "release_run", "batch": batch_id},
                           mode="direct", source="release", thread_id=it.get("thread_id") or "",
                           slack_channel=it.get("slack_channel") or "", slack_thread=it.get("slack_thread") or "")
    ledger.batch_set(batch_id, "released", {"task": tid})
    return {"batch": batch_id, "task": tid, "problems": [],
            "results": {"command queued as task %d; its output posts in the thread that asked for it" % tid: 1}}


def _run_released(task: dict, p: dict) -> dict:
    from ..autopilot import ledger
    try:
        bid = int(p.get("batch") or 0)
    except (TypeError, ValueError):
        bid = 0
    b = ledger.batch(bid) if bid else None
    if not b or b["kind"] != KIND:
        return {"refused": "there is no command batch %s" % (bid or "")}
    if b["status"] != "released" or int((b.get("result") or {}).get("task") or 0) != int(task["id"]):
        return {"refused": "batch %d is %s. A released command runs once, from the task its release queued; if the "
                           "brain restarted while it ran, check what it did and stage it again" % (bid, b["status"]),
                "batch": bid}
    it = (b.get("items") or [{}])[0]
    if not config.live():
        ledger.batch_set(bid, "staged", {})
        return {"refused": "the brain went back to dry mode after the release, so batch %d is staged again" % bid,
                "batch": bid}
    ledger.batch_set(bid, "running", {"task": int(task["id"])})
    d = commands.decide(it.get("script") or "", it.get("args") or [])
    if d.kind == "refuse":
        ledger.batch_set(bid, "refused", {"task": int(task["id"]), "problems": [d.say]})
        return {"refused": d.say, "batch": bid}
    res = execute(task, d, scrub=False)
    ok = res.get("exit") == 0 and not res.get("timed_out")
    ledger.batch_set(bid, "done" if ok else "failed", {"task": int(task["id"]), "exit": res.get("exit"),
                                                       "seconds": res.get("seconds"), "timed_out": res.get("timed_out")})
    res["batch"] = bid
    return res


def _owner() -> str:
    try:
        from .. import team
        wf = team.load().workflows.get(NAME)
        return wf.owner if wf else commands.DEFAULT_OWNER
    except Exception:  # noqa: BLE001  (a broken roster: the release still lands with Quinn)
        return commands.DEFAULT_OWNER


# ------------------------------------------------------------------------------------ the workflow

def run(task: dict) -> dict:
    p = store.payload_of(task)
    if p.get("action") == "release_run":
        return _run_released(task, p)
    script = str(p.get("script") or "")
    if not script:
        return {"refused": "no script was named"}
    d = commands.decide(script, p.get("args") or [])
    if d.kind == "refuse":
        return {"refused": d.say, "command": "python src/%s.py" % d.script if d.script else "",
                "summary": ("did not run %s: %s" % (d.script, d.say))[:500]}
    if d.kind == "stage":
        return stage(task, d)
    return execute(task, d, scrub=True)


def _link(task: dict, name: str) -> str:
    url = slack.file_url(int(task["id"]), name)
    return "<%s|%s>" % (url, name) if url else name


def render(task: dict, result: dict) -> str:
    if result.get("refused"):
        cmd = result.get("command") or ""
        return ":no_entry: Did not run%s: %s" % ((" `%s`" % cmd) if cmd else "", result["refused"])
    if result.get("staged"):
        bid = result["staged"]
        cost = result.get("quote_note") or ""
        if not cost and "it pays" in (result.get("why") or ""):
            cost = "the price is not known before it runs; the script's own caps apply"
        why = "It needs a person because %s." % result.get("why")
        if cost:
            why += " %s%s." % (cost[0].upper(), cost[1:])
        lines = [":inbox_tray: Staged as batch %d: `%s`" % (bid, result.get("command")), why,
                 "Nothing runs until a named releaser types `release batch %d`, then `confirm %d`, on a live brain." % (bid, bid)]
        if result.get("files"):
            lines.append("Dry run log: %s" % _link(task, result["files"][0]))
        return "\n".join(lines)
    head = ":hourglass: `%s` stopped at the time limit (%s minutes)." % (
        result.get("command"), round(commands.timeout_s() / 60)) if result.get("timed_out") else (
        "%s `%s` %s in %s s." % (":white_check_mark:" if result.get("exit") == 0 else ":x:", result.get("command"),
                                  "finished" if result.get("exit") == 0 else "exited %s" % result.get("exit"),
                                  result.get("seconds")))
    if result.get("batch"):
        head += " (released batch %s)" % result["batch"]
    lines = [head]
    tail = (result.get("tail") or "").replace("```", "'''")
    lines.append("```\n%s\n```" % tail if tail else "_It printed nothing._")
    links = [_link(task, result["log"])] if result.get("log") else []
    if links:
        lines.append("Full log: %s%s" % (links[0], " (the middle was cut to keep it readable)" if result.get("log_cut") else ""))
    if result.get("files"):
        shown = result["files"][:10]
        lines.append("Files: %s%s" % (", ".join(_link(task, n) for n in shown),
                                      " and %d more in the run folder" % (len(result["files"]) - 10)
                                      if len(result["files"]) > 10 else ""))
    if result.get("skipped_files"):
        lines.append("Not copied: %s" % "; ".join(result["skipped_files"][:3]))
    return "\n".join(lines)

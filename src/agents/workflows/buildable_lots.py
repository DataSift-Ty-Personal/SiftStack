"""buildable_lots: the buildable vacant lots in a county, screened, priced and ranked, asked for in Slack.

Atlas's workflow (team/roster.yaml). It runs src/vacant_land.py, the buildable lot finder, on the box:

  quick   once a county has a run: hydrate, score and export. hydrate checks which lots are DataSift
          records now (one ids-typed SiftMap search per 100 lots) and reads their owners from the
          records, so asking again after a pull is how the owners come in. A few minutes.
  full    refresh=yes, or a county with no run yet: every phase from the county's own GIS, SiftMap's
          land rows, USGS elevation, the screen, then the quick three. About 30 minutes here.

Both run in the background, niced, so the team's other work never waits behind them: the ask is
answered at once with what started, and when the run ends it queues this workflow again on the same
thread, which posts the workbook, the mail list and the pull list. One run at a time on the box (the
screen alone peaks near 720 MB), and a full run waits for enough free memory.

Nothing here makes a SiftMap detail read (DataSift's acceptable use forbids them in volume without
adding the records), writes to DataSift, texts, mails or pays. Every DataSift and SiftMap request is
counted against the box's shared budget (VACANT_LAND_BUDGET=agents). Bringing the lots into the
account is list_pull's job: it reads the "DataSift id" column of the pull list, quotes the add, and
pulls only on the operator's approved count on a live box.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from .. import config, slack, store

SRC = Path(__file__).resolve().parents[2]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import vacant_land as vl  # noqa: E402  (stdlib only at import; shapely loads inside the phases)

NAME = "buildable_lots"
QUICK = ("hydrate", "score", "export")
FULL = ("county", "sweep", "topo", "screen", "hydrate", "score", "export")
# The selftest injects fakes for every edge that would start a process or read the machine.
LAUNCHER = None     # (argv, env, log_path) -> pid of the background run
RUNNER = None       # (argv, env, log_path, timeout_s) -> exit code of one phase
ALIVE = None        # pid -> bool
MEMORY = None       # () -> MB available, or None when unknown


def _setting(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _folder(task: dict) -> Path:
    d = config.RUNS_DIR / ("%s-%d" % (NAME, int(task["id"])))
    (d / "files").mkdir(parents=True, exist_ok=True)
    return d


def data_root() -> Path:
    """Where the county runs live: VACANT_LAND_DIR, else the volume's vacant_land folder."""
    return Path(os.environ.get("VACANT_LAND_DIR") or Path(config.DATA_DIR) / "vacant_land")


def roots() -> list[Path]:
    """The data root, then the repo's own output (a workstation run that was never copied)."""
    first = data_root()
    own = Path(vl.OUT_ROOT)
    return [first] + ([own] if own.resolve() != first.resolve() else [])


def county_dir(c: dict) -> Path:
    """The folder of this county's run: the first root that holds one, else the data root."""
    for r in roots():
        if (r / c["slug"] / "screen.json").is_file():
            return r / c["slug"]
    return data_root() / c["slug"]


def configured() -> str:
    return "; ".join("%s County %s (%s)" % (c["name"], c["state"], f) for f, c in sorted(vl.COUNTIES.items()))


STATES = {"alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA", "colorado": "CO",
          "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
          "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS", "kentucky": "KY", "louisiana": "LA",
          "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
          "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
          "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY", "north carolina": "NC",
          "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
          "rhode island": "RI", "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX",
          "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
          "wisconsin": "WI", "wyoming": "WY"}


def _state_named(text: str) -> str:
    """The state a county request ends with ("Union County, TN", "union tennessee"), or ""."""
    low = " ".join(re.split(r"[^a-z]+", text.lower())).strip()
    for name, code in STATES.items():
        if low == name or low.endswith(" " + name):
            return code
    last = low.rsplit(" ", 1)[-1] if " " in low else ""
    return last.upper() if last.upper() in STATES.values() else ""


def resolve_county(text: str) -> tuple[dict | None, str]:
    """(county config, "") or (None, why). A FIPS, or a county name with or without its state; left
    out, the one county set up when there is only one. A request that ends in another state is not
    this county (there is a Union County in Tennessee too)."""
    raw = " ".join(str(text or "").split())
    if not raw:
        if len(vl.COUNTIES) == 1:
            return next(iter(vl.COUNTIES.values())), ""
        return None, "name the county: %s" % configured()
    digits = "".join(ch for ch in raw if ch.isdigit())
    if digits:
        c = vl.COUNTIES.get(digits.zfill(5))
        return (c, "") if c else (None, "no county with FIPS %s is set up. Set up: %s" % (digits, configured()))
    words = set(re.split(r"[^a-z]+", raw.lower()))
    state = _state_named(raw)
    for c in vl.COUNTIES.values():
        name = [w for w in re.split(r"[^a-z]+", c["name"].lower()) if w]
        if name and all(w in words for w in name) and state in ("", c["state"]):
            return c, ""
    return None, ("%s is not set up. Set up: %s. A new county is a COUNTIES entry in src/vacant_land.py (its "
                  "parcel, road, flood, zoning, plat, sale, dwelling and permit layers and their field names)"
                  % (raw, configured()))


def _share(v) -> float | None:
    """0.55, "55%" or "55" -> 0.55; None when nothing usable was given."""
    if v in (None, ""):
        return None
    try:
        x = float(str(v).strip().rstrip("%"))
    except ValueError:
        return None
    x = x / 100.0 if x > 1 else x
    return x if 0 < x < 1 else None


def _yes(v) -> bool:
    return str(v or "").strip().lower() in ("yes", "y", "true", "1", "refresh", "full", "rerun", "re-run")


# ------------------------------------------------------------------------------------ status

def status_path(c: dict) -> Path:
    return county_dir(c) / "run_status.json"


def read_status(c: dict) -> dict:
    try:
        return json.loads(status_path(c).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_status(c: dict, st: dict) -> None:
    p = status_path(c)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=1), encoding="utf-8")
    tmp.replace(p)


def alive(pid) -> bool:
    """True while the background run with this pid is still going. A pid number alone could be
    reused after a restart, so on Linux the process's own command line must be this workflow's."""
    if ALIVE is not None:
        return bool(ALIVE(pid))
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    proc = Path("/proc") / str(pid)
    if Path("/proc").is_dir():
        try:
            cmd = (proc / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
            state = (proc / "stat").read_text().rsplit(")", 1)[1].split()[0]
        except OSError:
            return False
        return "buildable_lots" in cmd and "--background" in cmd and state != "Z"
    try:
        import psutil  # noqa: PLC0415
        p = psutil.Process(pid)
        return p.is_running() and p.status() != psutil.STATUS_ZOMBIE and "buildable_lots" in " ".join(p.cmdline())
    except Exception:  # noqa: BLE001
        return False


def _age_s(stamp: str) -> float:
    try:
        return (datetime.now() - datetime.fromisoformat(stamp)).total_seconds()
    except (TypeError, ValueError):
        return 1e9


def running() -> list[tuple[dict, dict]]:
    """(county, status) for every county whose background run is still going. A run just launched
    counts for two minutes before its own process has written its pid."""
    out = []
    for c in vl.COUNTIES.values():
        st = read_status(c)
        if (st.get("state") == "running" and alive(st.get("pid"))) or \
                (st.get("state") == "starting" and _age_s(st.get("started") or "") < 120):
            out.append((c, st))
    return out


def free_mb() -> float | None:
    if MEMORY is not None:
        return MEMORY()
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 1024.0
    except (OSError, ValueError, IndexError):
        return None
    return None


# ------------------------------------------------------------------------------------ the background run

def _child_env() -> dict:
    """The run's environment: no vendor key, no Slack, dry (run_script's own scrub), plus the volume
    folder and the shared request budget. The Open API key stays: SiftMap search and record reads need it."""
    from . import run_script  # noqa: PLC0415
    env = run_script.child_env(scrub=True)
    env["VACANT_LAND_DIR"] = str(data_root())
    env["VACANT_LAND_BUDGET"] = "agents"
    return env


def _launch(argv: list[str], env: dict, log_path: Path) -> int:
    if LAUNCHER is not None:
        return LAUNCHER(argv, env, log_path)
    kw: dict = {"cwd": str(SRC.parent), "env": env, "stdin": subprocess.DEVNULL, "stderr": subprocess.STDOUT}
    if os.name == "posix":
        kw["start_new_session"] = True
    else:
        kw["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    fh = open(log_path, "ab")                       # noqa: SIM115  (handed to the child; closed by the reaper)
    proc = subprocess.Popen(argv, stdout=fh, **kw)
    try:
        # The brain's own threads come first; the phases the run starts inherit this. Set from here, not with
        # preexec_fn, which Python warns can deadlock a child forked from a process that runs threads.
        os.setpriority(os.PRIO_PROCESS, proc.pid, 10)
    except (AttributeError, OSError):
        pass

    def reap():                                     # a child nobody waits on stays a zombie and reads as alive
        proc.wait()
        fh.close()
    threading.Thread(target=reap, daemon=True, name="buildable-lots-reaper").start()
    return proc.pid


def _run_phase(argv: list[str], env: dict, log_path: Path, timeout_s: float) -> int:
    if RUNNER is not None:
        return RUNNER(argv, env, log_path, timeout_s)
    with open(log_path, "ab") as fh:
        proc = subprocess.Popen(argv, cwd=str(SRC.parent), env=env, stdin=subprocess.DEVNULL, stdout=fh,
                                stderr=subprocess.STDOUT)
        try:
            return proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            fh.write(b"\nSTOPPED: the phase ran past its time limit\n")
            return -9


def background(fips: str, phases: list[str], origin_id: int, refresh: bool = False,
               offer_low: float | None = None, offer_high: float | None = None) -> int:
    """The run itself, in its own process: each phase of vacant_land.py in turn, the status written
    after each, then this workflow queued again on the asker's thread to post what came out."""
    c = vl.COUNTIES[fips]
    d = county_dir(c)
    d.mkdir(parents=True, exist_ok=True)
    st = dict(read_status(c), state="running", phases=phases, phase="", pid=os.getpid(), origin=origin_id,
              started=_now(), finished="", rc=None, failed_phase="")
    write_status(c, st)
    env = dict(os.environ)
    limit = _setting("BUILDABLE_LOTS_PHASE_MIN", 45) * 60
    for p in phases:
        st.update(phase=p, phase_started=_now())
        write_status(c, st)
        argv = [sys.executable, str(SRC / "vacant_land.py"), "--phase", p, "--fips", fips, "--owners", "account"]
        if refresh and p == "county":
            argv.append("--refresh")
        if p == "export":
            argv += ["--offer-low", str(offer_low if offer_low else 0.50),
                     "--offer-high", str(offer_high if offer_high else 0.65)]
        log_path = d / ("run_%s.log" % p)
        log_path.write_bytes(b"")
        rc = _run_phase(argv, env, log_path, limit)
        if rc:
            st.update(state="failed", rc=rc, failed_phase=p)
            break
    else:
        st.update(state="done", rc=0)
    st.update(finished=_now(), phase="")
    write_status(c, st)
    origin = store.task(origin_id) or {}
    payload = store.payload_of(origin) if origin else {}
    store.enqueue(origin.get("agent") or "analysis", NAME, subject="buildable lots, %s" % c["label"],
                  payload={"inputs": payload.get("inputs") or {"county": fips},
                           "details": payload.get("details") or "",
                           "after_run": {"fips": fips, "state": st["state"], "origin": origin_id}},
                  thread_id=origin.get("thread_id") or "", parent_id=origin_id, hop=int(origin.get("hop") or 0),
                  source=origin.get("source") or "", mode="direct",
                  slack_channel=origin.get("slack_channel") or "", slack_thread=origin.get("slack_thread") or "")
    return 0 if st["state"] == "done" else 1


# ------------------------------------------------------------------------------------ the workflow

def _tail(path: Path, lines: int = 6) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    keep = [x for x in text.splitlines() if x.strip()][-lines:]
    return " | ".join(x.strip()[:200] for x in keep)


def _collect(c: dict, folder: Path) -> list[str]:
    """The run's deliverables copied into this task's folder, the CSVs named for the county so list_pull
    finds the pull list by that name (it takes the newest file of a name)."""
    d = county_dir(c)
    made = []
    books = sorted(d.glob("*_Buildable_Lots_*.xlsx"), key=lambda p: p.stat().st_mtime)
    if books:
        shutil.copy2(books[-1], folder / "files" / books[-1].name)
        made.append(books[-1].name)
    for src, dest in (("mail_list.csv", "%s_mail_list.csv"), ("pull_list.csv", "%s_pull_list.csv")):
        if (d / src).is_file():
            name = dest % c["slug"]
            shutil.copy2(d / src, folder / "files" / name)
            made.append(name)
    return made


def _top(ex: dict, n: int = 3) -> list[dict]:
    """The export's own top lots: the offers are priced in export, so scored.json never carries them."""
    return [{"tier": t.get("tier"), "address": t.get("address"), "city": t.get("city"), "acres": t.get("acres"),
             "segment": t.get("segment"), "offer": [t.get("offer_low"), t.get("offer_high")]}
            for t in (ex.get("top") or [])[:n]]


def _finished(task: dict, c: dict, after: dict, base: dict) -> dict:
    folder = Path(base["run_dir"])
    st = read_status(c)
    if (after.get("state") or st.get("state")) != "done":
        failed = st.get("failed_phase") or "?"
        return dict(base, ok=False, mode="failed", phase=failed, rc=st.get("rc"),
                    log=_tail(county_dir(c) / ("run_%s.log" % failed)),
                    problem="the %s step stopped (exit %s)" % (failed, st.get("rc")))
    try:
        ex = json.loads((county_dir(c) / "export.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return dict(base, ok=False, mode="failed", problem="the run finished but left no export.json")
    return dict(base, ok=True, mode="done", export=ex, files=_collect(c, folder), top=_top(ex),
                started=st.get("started"), finished=st.get("finished"), phases=st.get("phases") or [])


def run(task: dict) -> dict:
    payload = store.payload_of(task)
    inputs = payload.get("inputs") or {}
    folder = _folder(task)
    after = payload.get("after_run") or {}
    c, why = resolve_county(after.get("fips") or inputs.get("county") or "")
    base = {"run_dir": str(folder), "county": (c or {}).get("label") or str(inputs.get("county") or ""),
            "at": _now()}
    if not c:
        return dict(base, ok=False, mode="refused", problem=why)
    if after:
        return _finished(task, c, after, base)

    busy = running()
    mine = [st for cc, st in busy if cc["fips"] == c["fips"]]
    if mine:
        return dict(base, ok=True, mode="running", phase=mine[0].get("phase") or "starting",
                    started=mine[0].get("started"), phases=mine[0].get("phases") or [])
    if busy:
        cc, st = busy[0]
        return dict(base, ok=False, mode="busy", other=cc["label"], phase=st.get("phase") or "",
                    problem="the box is running %s right now (%s step); one county at a time, ask again when "
                            "that run posts" % (cc["label"], st.get("phase") or "a"))
    refresh = _yes(inputs.get("refresh"))
    has_run = (county_dir(c) / "screen.json").is_file()
    phases = list(FULL if (refresh or not has_run) else QUICK)
    # Measured on Union OH: the screen peaks near 720 MB, score and export near 390 MB.
    need = _setting("BUILDABLE_LOTS_MIN_FREE_MB", 900) if "screen" in phases else \
        _setting("BUILDABLE_LOTS_QUICK_FREE_MB", 500)
    free = free_mb()
    if free is not None and free < need:
        return dict(base, ok=False, mode="refused",
                    problem="the box has %.0f MB of memory free and this run needs about %.0f; ask again in a few "
                            "minutes" % (free, need))
    lo, hi = _share(inputs.get("offer_low")), _share(inputs.get("offer_high"))
    if lo and hi and lo > hi:
        lo, hi = hi, lo
    argv = [sys.executable, "-m", "agents.workflows.buildable_lots", "--background", "--fips", c["fips"],
            "--phases", ",".join(phases), "--origin", str(int(task["id"]))]
    if refresh:
        argv.append("--refresh")
    if lo:
        argv += ["--offer-low", str(lo)]
    if hi:
        argv += ["--offer-high", str(hi)]
    d = county_dir(c)
    d.mkdir(parents=True, exist_ok=True)
    # Written before the start so a second ask in the first seconds sees it; the run then writes its own.
    write_status(c, {"state": "starting", "pid": None, "origin": int(task["id"]), "phases": phases, "phase": "",
                     "started": _now(), "refresh": refresh})
    pid = _launch(argv, _child_env(), d / "run_background.log")
    return dict(base, ok=True, mode="started", phases=phases, refresh=refresh, pid=pid,
                offer=[lo or 0.50, hi or 0.65], minutes=30 if "screen" in phases else 5)


# ------------------------------------------------------------------------------------ Slack

STEP_WORDS = {"county": "the county's parcel, road, flood, zoning, plat, sale and permit records",
              "sweep": "SiftMap's land rows", "topo": "slope and low spots from USGS lidar",
              "screen": "the buildability screen", "hydrate": "owners from the account",
              "score": "scores and tiers", "export": "the workbook"}


def _n(x) -> str:
    try:
        return "{:,}".format(int(x))
    except (TypeError, ValueError):
        return "0"


def _money(x) -> str:
    try:
        return "${:,.0f}".format(float(x))
    except (TypeError, ValueError):
        return "no figure"


def _since(stamp: str) -> str:
    try:
        mins = (datetime.now() - datetime.fromisoformat(stamp)).total_seconds() / 60
    except (TypeError, ValueError):
        return ""
    return " (started %d minutes ago)" % mins if mins >= 1 else " (started just now)"


def render(task: dict, result: dict) -> str:
    mode = result.get("mode")
    county = result.get("county") or "that county"
    if mode in ("refused", "busy"):
        return "Buildable lots for %s did not start: %s." % (county, str(result.get("problem") or "").rstrip("."))
    if mode == "running":
        return ("Buildable lots for %s is already running%s, now on %s. I'll post the files here when it ends."
                % (county, _since(result.get("started") or ""),
                   STEP_WORDS.get(result.get("phase"), result.get("phase") or "its first step")))
    if mode == "started":
        steps = ", ".join(STEP_WORDS[p] for p in result.get("phases") or [])
        return ("Started buildable lots for %s in the background: %s. About %d minutes; I'll post the files "
                "here when it ends." % (county, steps, result.get("minutes") or 5))
    if mode == "failed":
        log = (" Last lines: %s" % result["log"]) if result.get("log") else ""
        return ("Buildable lots for %s stopped at %s: %s. Every step that finished is saved, so asking again "
                "continues from there.%s" % (county, STEP_WORDS.get(result.get("phase"), result.get("phase") or
                                                                      "a step"), result.get("problem"), log))
    ex = result.get("export") or {}
    v = ex.get("verdicts") or {}
    tiers = ex.get("tiers") or {}
    owners = ex.get("owners") or {}
    run_day = str(ex.get("run") or "")[:10]
    lines = ["Buildable lots, %s (run %s): %s vacant lots in the window, %s can carry a house, %s mailable now "
             "(A %s, B %s, C %s)." % (county, run_day, _n(ex.get("in_window")), _n(ex.get("marketable")),
                                      _n(ex.get("mailable")), _n(tiers.get("A")), _n(tiers.get("B")),
                                      _n(tiers.get("C")))]
    out = [(v.get("BUILT SINCE REAPPRAISAL"), "already built on"), (v.get("NOT BUILDABLE"), "cannot carry a house"),
           (v.get("PUBLIC OR COMMON"), "public or common land"), (v.get("BUILDER INVENTORY"), "builder inventory")]
    said = ["%s %s" % (_n(k), w) for k, w in out if k]
    if said:
        lines.append("Screened out: %s." % ", ".join(said))
    lines.append("Owners: %s read from DataSift records, %s from an earlier read; %s lots show no owner until "
                 "they are records." % (_n(owners.get("account")), _n(owners.get("siftmap cache")),
                                        _n(ex.get("owner_unknown"))))
    band = ex.get("offer_band") or [0.5, 0.65]
    lines.append("Offers run %d%% to %d%% of the lot comp; the county's median lot sale is %s over %s sales."
                 % (round(band[0] * 100), round(band[1] * 100), _money(ex.get("comp_median")), _n(ex.get("comps"))))
    for t in result.get("top") or []:
        lo, hi = (list(t.get("offer") or []) + [None, None])[:2]
        offer = ", offer %s to %s" % (_money(lo), _money(hi)) if lo and hi else ", not priced (no lot comp)"
        lines.append("Top lot: %s, %s, %s ac, tier %s, %s%s." % (
            t.get("address"), t.get("city"), t.get("acres"), t.get("tier"), str(t.get("segment") or "").lower(),
            offer))
    for name in result.get("files") or []:
        u = slack.file_url(task["id"], name)
        lines.append("%s: %s" % (name, u) if u else name)
    pulls = int(ex.get("to_pull") or 0)
    if pulls:
        pull_name = next((n for n in result.get("files") or [] if n.endswith("_pull_list.csv")), "the pull list")
        lines.append("%s lots are not in DataSift yet. To bring them into the account this box runs on, ask me to "
                     "pull %s onto the list \"%s\": I quote it first, and nothing is pulled without your approved "
                     "count." % (_n(pulls), pull_name, ex.get("list") or "%s - Buildable Lots" % county))
    return "\n".join(lines)


# ------------------------------------------------------------------------------------ entry point

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="the buildable lots background run (started by the workflow)")
    ap.add_argument("--background", action="store_true", required=True)
    ap.add_argument("--fips", required=True, choices=sorted(vl.COUNTIES))
    ap.add_argument("--phases", required=True)
    ap.add_argument("--origin", type=int, required=True)
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--offer-low", type=float)
    ap.add_argument("--offer-high", type=float)
    a = ap.parse_args(argv)
    phases = [p for p in a.phases.split(",") if p in FULL]
    return background(a.fips, phases, a.origin, refresh=a.refresh, offer_low=a.offer_low, offer_high=a.offer_high)


if __name__ == "__main__":
    raise SystemExit(main())

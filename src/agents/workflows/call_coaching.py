"""call_coaching: the picked calls graded, with the notes going to the owner first (build plan 6.7).

The teaching's review rhythm is small and strict: each week the two best and two worst calls of
every caller (every day in a new hire's first week), one thing to fix or to keep from each, and the
notes go to the owner first, who touches them up before a caller sees them. The autopilot's
coaching_picks stage chooses the calls from the account's own call log and queues them here; an
employee asked in Slack queues the latest picks the same way. This workflow listens, grades and
writes the notes. It does not choose.

Why it is built this way:
  * THE RECORDING COMES FROM THE LINK THE CALL LOG CARRIES. The old puller read one dialer's call
    list through a saved browser session, so a call made through the other dialer was never
    reviewed, and the brain holds no browser session at all. The log's link opens without a login.
    When one does not (a 401, a login page), the run says so and stops for every call on that
    host. It never tries a cookie or a guess.
  * NO TRANSCRIPT, NO GRADE. A voicemail, a wrong number or an empty transcript is kept as not
    gradeable; a report without its SCORES JSON is kept as ungraded. A grade is never invented.
  * MONEY ONLY ON A LIVE BOX, INSIDE TWO CAPS. Transcription (OpenRouter audio) and grading
    (Claude) are paid. In dry mode the run does every free step (it opens each recording link to
    prove it holds audio) and says what the paid steps would cost. Live, each paid step asks this
    run's cap (AGENTS_COACH_RUN_CAP_USD) and the brain's daily cap first, and is recorded after.
  * A CALL IS PAID FOR ONCE. Every result is kept in wf_coach_grades with its transcript, and a call
    asked about again is read from there; an ungraded one is graded again without paying for the
    transcription twice.
  * THE RECORDING LINK GOES NOWHERE. Not in the channel, not in the files, not to the grader.

The rubric is the coach skills' own file (skills/<coach>/references/rubric.md, shipped in the
brain's image), chosen by the pipeline the triage names; AGENTS_COACH_ROLES pins a caller to one.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import urllib.parse
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import config, slack, store

SRC = Path(__file__).resolve().parents[2]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

log = logging.getLogger(__name__)

WORKFLOW = "call_coaching"
TZ = "America/New_York"
SCHEMA = """
CREATE TABLE IF NOT EXISTS wf_coach_grades (
    call_id TEXT PRIMARY KEY, period TEXT, caller TEXT, kind TEXT, at TEXT, seconds INTEGER,
    status TEXT, why TEXT, pipeline TEXT, call_type TEXT, total REAL, band TEXT, auto_fail TEXT,
    criterion TEXT, note TEXT, report TEXT, transcript TEXT, problems TEXT, usd REAL,
    task_id INTEGER, graded_at TEXT);
"""
# A call in one of these states is never paid for again; the others (ungraded, error) may be retried.
FINAL = ("graded", "not_scored", "not_gradeable", "no_transcript")
LATEST = ("", "latest", "last", "newest", "the latest", "this week", "last week", "all")

# Stand-ins the selftest sets, so no test can reach a dialer, OpenRouter or Claude. None = the real one.
FETCH = None        # (url, probe: bool, max_bytes: int) -> dict shaped like transcribe.fetch_recording
TRANSCRIBE = None   # (audio: bytes, fmt: str, caller: str) -> (text, usage)
CLASSIFY = None     # (text) -> (fields, usage)
CLAUDE = None       # an Anthropic client for grade.grade
NOW = None          # a fixed "now" for the selftest


# ---------------------------------------------------------------------- settings
def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


def _roles(raw: str) -> dict:
    """AGENTS_COACH_ROLES="Ada=cold_call, Ben=lead_management": a caller whose calls are always one
    pipeline. A dedicated cold caller's follow-up-sounding call is still a cold call."""
    out = {}
    for part in (raw or "").replace(";", ",").split(","):
        name, _, pipe = part.partition("=")
        if name.strip() and pipe.strip():
            out[name.strip().lower()] = pipe.strip().lower()
    return out


def settings() -> dict:
    return {"run_cap": _f("AGENTS_COACH_RUN_CAP_USD", 4.0),
            "max_calls": int(_f("AGENTS_COACH_MAX_CALLS", 16)),
            "audio_per_min": _f("AGENTS_COACH_AUDIO_USD_PER_MIN", 0.003),
            "classify_usd": 0.001,
            "grade_usd": _f("AGENTS_COACH_GRADE_EST_USD", 0.30),
            "max_minutes": _f("AGENTS_COACH_MAX_MINUTES", 30),
            "max_bytes": int(_f("AGENTS_COACH_MAX_MB", 40) * 1_000_000),
            "roles": _roles(os.environ.get("AGENTS_COACH_ROLES") or "")}


def _now() -> datetime:
    return NOW or datetime.now(timezone.utc)


def _tz() -> str:
    try:
        from ..autopilot import policy
        pol = policy.load()
        return pol.tz if pol else TZ
    except Exception:  # noqa: BLE001  (a playbook that does not load must not stop the coaching)
        return TZ


# ---------------------------------------------------------------------- the stored results
_READY: set = set()


def ensure() -> None:
    key = str(config.DB_PATH)
    if key in _READY:
        return
    with store._LOCK, store._db() as c:
        c.executescript(SCHEMA)
    _READY.add(key)


def stored(call_id: str) -> dict | None:
    ensure()
    with store._LOCK, store._db() as c:
        r = c.execute("SELECT * FROM wf_coach_grades WHERE call_id=?", (str(call_id),)).fetchone()
    return dict(r) if r else None


def keep(row: dict, task_id: int) -> None:
    ensure()
    cols = ("call_id", "period", "caller", "kind", "at", "seconds", "status", "why", "pipeline", "call_type", "total",
            "band", "auto_fail", "criterion", "note", "report", "transcript", "problems", "usd", "task_id", "graded_at")
    vals = dict(row, task_id=task_id, graded_at=store.now())
    vals["note"] = json.dumps(row.get("note") or {})
    vals["problems"] = json.dumps(row.get("problems") or [])
    with store._LOCK, store._db() as c:
        c.execute("INSERT OR REPLACE INTO wf_coach_grades (%s) VALUES (%s)" % (",".join(cols), ",".join("?" * len(cols))),
                  [vals.get(k) for k in cols])


def _from_store(r: dict, pick: dict) -> dict:
    row = dict(r)
    for k in ("note", "problems"):
        try:
            row[k] = json.loads(row.get(k) or ("{}" if k == "note" else "[]"))
        except ValueError:
            row[k] = {} if k == "note" else []
    row["reused"] = True
    row.update({k: pick[k] for k in ("kind", "why", "period") if pick.get(k)})
    return row


# ---------------------------------------------------------------------- which calls
def _pick(c: dict, period: str = "") -> dict:
    return {"call": str(c.get("call") or c.get("call_id") or ""), "caller": str(c.get("caller") or "").strip(),
            "kind": str(c.get("kind") or "").strip().lower(), "at": str(c.get("at") or ""),
            "seconds": int(c.get("seconds") or 0), "recording": str(c.get("recording") or ""),
            "why": str(c.get("why") or ""), "period": str(c.get("period") or period or "")}


def picks_for(scope: str) -> list[dict]:
    """The picked calls an ask in Slack means. "latest" (the default): every pick of the most
    recent picking day. A caller's first name: that caller's newest picks. A period as the stage
    keeps it ("week:2026-11-13", "day:2026-11-13:Ada"): exactly that period."""
    from ..autopilot import coachpick
    rows = coachpick.picks()
    if not rows:
        return []
    s = " ".join(str(scope or "").split())
    if s.split(":", 1)[0] in ("week", "day") and ":" in s:
        chosen = [r for r in rows if r["period"] == s]
    else:
        pool = rows if s.lower() in LATEST else [r for r in rows if str(r["caller"]).lower() == s.lower()]
        if not pool:
            return []
        newest = max(str(r.get("picked_at") or "")[:10] for r in pool)
        chosen = [r for r in pool if str(r.get("picked_at") or "")[:10] == newest]
    return [_pick({"call": r["call_id"], "caller": r["caller"], "kind": r["kind"], "at": r["at"], "seconds": r["seconds"],
                   "recording": r["recording"], "why": r["why"]}, r["period"]) for r in chosen]


def label_of(periods: list[str]) -> str:
    """How a pack is named in the channel: the week ending Nov 13, or Ada on Nov 13."""
    ps = sorted({p for p in periods if p})
    if not ps:
        return "the picked calls"
    weeks = [p for p in ps if p.startswith("week:")]
    p = weeks[-1] if weeks else ps[-1]
    bits = p.split(":")
    try:
        d = date.fromisoformat(bits[1])
        day = "%s %d" % (d.strftime("%b"), d.day)
    except (IndexError, ValueError):
        return p
    if bits[0] == "week":
        return "the week ending %s" % day
    who = bits[2] if len(bits) > 2 else "a new hire"
    return "%s on %s" % (who, day) if len(ps) == 1 else "the picks of %s" % day


# ---------------------------------------------------------------------- money
class Budget:
    """The pattern the deep prospect uses: ask before a paid step, record after it. This run's cap
    and the brain's daily cap (everything that is not research) must both have room."""

    def __init__(self, task_id: int, run_cap: float):
        self.task_id, self.run_cap = int(task_id or 0), float(run_cap)

    def allows(self, usd: float) -> str:
        """"" when there is room for about `usd` more, else why not, in plain words."""
        run_total = store.spent(task_id=self.task_id) + usd
        day_total = store.spent(day=store.today(), research=False) + usd
        if run_total > self.run_cap:
            return "about $%.2f more would pass this run's cap of $%.2f (AGENTS_COACH_RUN_CAP_USD)" % (usd, self.run_cap)
        if day_total > config.DAILY_CAP_USD:
            return "about $%.2f more would pass the brain's daily cap of $%.2f (AGENTS_DAILY_CAP_USD)" % (usd, config.DAILY_CAP_USD)
        return ""

    def charge(self, usd: float, what: str) -> None:
        store.add_spend(self.task_id, usd, "call coaching: %s" % what)


def estimate(pick: dict, st: dict) -> dict:
    minutes = max(1.0, pick["seconds"] / 60.0)
    t = round(minutes * st["audio_per_min"], 4)
    return {"transcribe": t, "classify": st["classify_usd"], "grade": st["grade_usd"],
            "total": round(t + st["classify_usd"] + st["grade_usd"], 4)}


# ---------------------------------------------------------------------- the engine's pieces
def _engine():
    from call_coaching import grade, transcribe
    return grade, transcribe


def _fetch(url: str, probe: bool, max_bytes: int) -> dict:
    if FETCH is not None:
        return FETCH(url, probe, max_bytes)
    _, transcribe = _engine()
    return transcribe.fetch_recording(url, max_bytes=max_bytes, probe=probe)


def _transcribe(audio: bytes, fmt: str, caller: str) -> tuple[str, dict]:
    if TRANSCRIBE is not None:
        return TRANSCRIBE(audio, fmt, caller)
    _, transcribe = _engine()
    return transcribe.transcribe_bytes(audio, fmt, caller or None)


def _classify(text: str) -> tuple[dict, dict]:
    if CLASSIFY is not None:
        return CLASSIFY(text)
    _, transcribe = _engine()
    return transcribe.classify_text(text)


def _billed(usage: dict) -> float:
    _, transcribe = _engine()
    return transcribe.billed(usage)


def _keys_missing() -> str:
    """Why a live run cannot pay for anything, or "". Checked before the first paid step: paying
    to transcribe calls that can then not be graded is money for nothing."""
    if TRANSCRIBE is None:
        try:
            _engine()[1]._env("OPENROUTER_API_KEY")
        except KeyError:
            return "OPENROUTER_API_KEY is not set on the brain, so no call can be transcribed"
    if CLAUDE is None and not (os.environ.get("ANTHROPIC_API_KEY") or config.ANTHROPIC_API_KEY):
        return "ANTHROPIC_API_KEY is not set on the brain, so no call can be graded"
    return ""


def _when(at: str, tz: str) -> str:
    try:
        d = datetime.fromisoformat(str(at).replace("Z", "+00:00")).astimezone(ZoneInfo(tz))
    except (TypeError, ValueError):
        return ""
    return "%s %s %d, %s" % (d.strftime("%a"), d.strftime("%b"), d.day, d.strftime("%I:%M%p").lstrip("0").lower())


def _length(seconds: int) -> str:
    return "%d min" % max(1, round((seconds or 0) / 60.0))


# ---------------------------------------------------------------------- one call
def _row(pick: dict) -> dict:
    return {"call_id": pick["call"], "period": pick["period"], "caller": pick["caller"], "kind": pick["kind"],
            "at": pick["at"], "seconds": pick["seconds"], "status": "", "why": "", "pipeline": "", "call_type": "",
            "total": None, "band": "", "auto_fail": "", "criterion": "", "note": {}, "report": "", "transcript": "",
            "problems": [], "usd": 0.0}


def grade_one(pick: dict, row: dict, budget: Budget, st: dict, tz: str, prior: dict | None = None) -> dict:
    """Transcribe, triage and grade one call on a live box, inside the caps. The row says what
    happened; nothing is raised for an ordinary miss."""
    grade, _ = _engine()
    est = estimate(pick, st)
    text = (prior or {}).get("transcript") or ""
    cls: dict = {}
    if not text:
        why = budget.allows(est["transcribe"] + est["classify"])
        if why:
            return dict(row, status="left", why="not transcribed: %s" % why)
        rec = _fetch(pick["recording"], False, st["max_bytes"])
        if not rec.get("ok"):
            return dict(row, status="stopped" if rec.get("code") == "login" else "error", why=rec.get("why") or "no audio")
        try:
            text, usage = _transcribe(rec["audio"], rec.get("fmt") or "mp3", pick["caller"])
        except Exception as exc:  # noqa: BLE001  (OpenRouter down: say it for this call, try the next)
            return dict(row, status="error", why="transcription failed (%s)" % str(exc)[:120])
        cost = _billed(usage) or est["transcribe"]
        budget.charge(cost, "transcribed %s's call of %s" % (pick["caller"] or "a caller", _when(pick["at"], tz)))
        row["usd"] += cost
        text = grade.clean(text or "")
        row["transcript"] = text
        if not text.strip():
            return dict(row, status="no_transcript", why="the transcription came back empty: no transcript, no grade")
    row["transcript"] = text
    if prior and prior.get("status") == "ungraded" and prior.get("pipeline"):
        # Triaged already and found worth grading; only the grade itself was missing.
        cls = {"call_type": prior.get("call_type") or "conversation", "pipeline": prior["pipeline"], "worth_grading": True}
    else:
        try:
            cls, usage = _classify(text)
        except Exception as exc:  # noqa: BLE001
            return dict(row, status="error", why="the triage failed (%s)" % str(exc)[:120])
        cost = _billed(usage) or est["classify"]
        budget.charge(cost, "triaged %s's call" % (pick["caller"] or "a caller"))
        row["usd"] += cost
    row["call_type"] = str(cls.get("call_type") or "unknown")
    pinned = st["roles"].get(pick["caller"].lower())
    pipeline = pinned or str(cls.get("pipeline") or "").strip().lower()
    row["pipeline"] = pipeline
    if not cls.get("worth_grading"):
        return dict(row, status="not_gradeable", why="the triage read it as %s, which is never scored" % row["call_type"].replace("_", " "))
    if pipeline not in grade.PIPELINES:
        return dict(row, status="not_gradeable", why="the triage named no pipeline a rubric covers (%r)" % pipeline)
    why = budget.allows(est["grade"])
    if why:
        return dict(row, status="ungraded", why="transcribed, not graded: %s" % why)
    meta = {"caller": pick["caller"], "when": _when(pick["at"], tz), "length": _length(pick["seconds"]),
            "kind": pick["kind"], "why": pick["why"], "labels_swapped": bool(cls.get("labels_swapped"))}
    try:
        g = grade.grade(text, meta, pipeline, client=CLAUDE)
    except FileNotFoundError:
        return dict(row, status="ungraded", why="the %s rubric is not on this box" % pipeline.replace("_", " "))
    except Exception as exc:  # noqa: BLE001  (an API error costs this call its grade, not the run)
        return dict(row, status="ungraded", why="grading failed (%s)" % str(exc)[:120])
    budget.charge(float(g.get("usd") or 0), "graded %s's call" % (pick["caller"] or "a caller"))
    row["usd"] += float(g.get("usd") or 0)
    row["report"], row["problems"], row["note"] = g.get("report") or "", list(g.get("problems") or []), g.get("note") or {}
    sc = g.get("scores")
    if sc is None:
        return dict(row, status="ungraded", why=(row["problems"] or ["no grade came back"])[0])
    row.update(call_type=sc.get("call_type") or row["call_type"], total=sc.get("total"), band=sc.get("band") or "",
               auto_fail=str(sc.get("auto_fail") or ""), criterion=str((row["note"] or {}).get("criterion") or ""))
    if sc.get("call_type") == "not_scored":
        return dict(row, status="not_scored", why="the rubric's gate does not score this call")
    return dict(row, status="graded", why="")


# ---------------------------------------------------------------------- the run
def _calls_from(payload: dict) -> list[dict]:
    inputs = payload.get("inputs") or {}
    calls = inputs.get("calls")
    if isinstance(calls, list) and calls:
        return [_pick(c, str(inputs.get("period") or "")) for c in calls if isinstance(c, dict)]
    scope = inputs.get("scope") or inputs.get("period") or ""
    return picks_for(str(scope))


def run(task: dict) -> dict:
    ensure()
    st = settings()
    tz = _tz()
    payload = store.payload_of(task)
    live = config.live()
    picks, seen = [], set()
    for p in _calls_from(payload):
        if p["call"] and p["call"] not in seen:
            seen.add(p["call"])
            picks.append(p)
    label = label_of([p["period"] for p in picks])
    out = {"workflow": WORKFLOW, "live": live, "label": label, "picked": len(picks), "calls": [], "usd": 0.0,
           "would_usd": 0.0, "problems": [], "files": [], "run_dir": ""}
    if not picks:
        out["nothing"] = ("No calls are picked to review yet. The coaching picks stage chooses each caller's two best and "
                          "two worst calls from the call log each week, and every day for a new hire.")
        return out
    budget = Budget(task.get("id") or 0, st["run_cap"])
    blocked: dict[str, str] = {}            # recording host -> why it needs a login
    missing = _keys_missing() if live else ""
    if missing:
        out["problems"].append(missing + "; nothing was paid for")
    rows, worked = [], 0
    for pick in picks:
        row = _row(pick)
        prior = stored(pick["call"])
        if prior and prior.get("status") in FINAL:
            rows.append(_from_store(prior, pick))
            continue
        if worked >= st["max_calls"]:
            rows.append(dict(row, status="left", why="past this run's %d calls (AGENTS_COACH_MAX_CALLS)" % st["max_calls"]))
            continue
        worked += 1
        if not pick["recording"]:
            rows.append(dict(row, status="stopped", why="the call log carries no recording for this call"))
            continue
        host = urllib.parse.urlsplit(pick["recording"]).hostname or ""
        if host in blocked:
            rows.append(dict(row, status="stopped", why=blocked[host]))
            continue
        if pick["seconds"] > st["max_minutes"] * 60:
            rows.append(dict(row, status="stopped", why="longer than %d minutes, too long to send in one piece" % st["max_minutes"]))
            continue
        if not live or missing:
            have_text = bool(prior and prior.get("transcript"))
            if not have_text:
                probe = _fetch(pick["recording"], True, st["max_bytes"])
                if not probe.get("ok"):
                    if probe.get("code") == "login":
                        blocked[host] = probe.get("why") or "the recording link needs a login the brain does not have"
                    rows.append(dict(row, status="stopped" if probe.get("code") == "login" else "error",
                                     why=probe.get("why") or "the recording did not open"))
                    continue
            est = estimate(pick, st)
            cost = est["grade"] + (0 if have_text else est["transcribe"] + est["classify"])
            out["would_usd"] += cost
            rows.append(dict(row, status="would_grade", why="the recording opens and holds audio", usd=0.0, est=round(cost, 4)))
            continue
        r = grade_one(pick, row, budget, st, tz, prior)
        if r["status"] == "stopped" and "login" in r["why"]:
            blocked[host] = r["why"]
        if r["status"] not in ("left",):
            keep(r, task.get("id") or 0)
        rows.append(r)
    out["calls"] = [_public(r, tz) for r in rows]
    out["usd"] = round(sum(float(r.get("usd") or 0) for r in rows if not r.get("reused")), 4)
    out["would_usd"] = round(out["would_usd"], 2)
    if blocked:
        out["problems"].append("%d call(s) not graded: %s" % (sum(1 for r in rows if r["status"] == "stopped" and
                                                                  r["why"] in blocked.values()), sorted(blocked.values())[0]))
    if any(r["status"] in ("graded", "not_scored") for r in rows):
        files = _write_files(task, rows, label, tz)
        out["files"], out["run_dir"] = [p.name for p in files], str(files[0].parent.parent) if files else ""
    return out


def _public(r: dict, tz: str) -> dict:
    """One call as the task result keeps it: no recording link, no transcript, no report text."""
    note = r.get("note") or {}
    return {"call": r.get("call_id"), "caller": r.get("caller"), "kind": r.get("kind"), "when": _when(r.get("at"), tz),
            "seconds": r.get("seconds"), "status": r.get("status"), "why": r.get("why"), "pipeline": r.get("pipeline"),
            "call_type": r.get("call_type"), "total": r.get("total"), "band": r.get("band"), "auto_fail": r.get("auto_fail"),
            "criterion": r.get("criterion") or note.get("criterion") or "", "focus": note.get("focus") or "",
            "usd": round(float(r.get("usd") or 0), 4), "est": r.get("est"), "reused": bool(r.get("reused")),
            "problems": list(r.get("problems") or [])[:3]}


# ---------------------------------------------------------------------- the files
def _files_dir(task: dict) -> Path:
    d = Path(config.RUNS_DIR) / ("%s-%d" % (WORKFLOW, int(task.get("id") or 0))) / "files"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _slug(label: str) -> str:
    return "_".join("".join(ch if ch.isalnum() else " " for ch in label.lower()).split())[:60] or "calls"


def _score_text(r: dict) -> str:
    if r.get("status") == "not_scored":
        return "not scored by the rubric's gate"
    if r.get("call_type") == "short":
        return "short call (its own scale)"
    if r.get("total") is None:
        return "no total"
    return "%s (%s)" % (_fmt_total(r.get("total")), r.get("band") or "")


def _fmt_total(v) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return ""
    return str(int(f)) if abs(f - round(f)) < 0.05 else "%.1f" % f


def _note_line(r: dict, names: dict) -> str:
    n = r.get("note") or {}
    crit = str(n.get("criterion") or r.get("criterion") or "")
    name = ("%s %s" % (crit, names.get(crit, ""))).strip()
    if n.get("focus") == "reinforce":
        head = "Keep doing (%s)" % name if name else "Keep doing"
    else:
        head = "Fix first (%s)" % name if name else "Fix first"
    bits = [head + ":"]
    if n.get("said"):
        bits.append('they said "%s".' % n["said"])
    if n.get("instead"):
        bits.append('Use instead: "%s".' % n["instead"])
    if n.get("why"):
        bits.append(n["why"])
    return " ".join(bits)


def _names(pipeline: str) -> dict:
    try:
        grade, _ = _engine()
        return grade.criterion_names(grade.rubric(pipeline))
    except Exception:  # noqa: BLE001
        return {}


def _write_files(task: dict, rows: list[dict], label: str, tz: str) -> list[Path]:
    from .kpi_report import write_workbook
    grade, _ = _engine()
    d = _files_dir(task)
    slug = _slug(label)
    names = {p: _names(p) for p in {r.get("pipeline") for r in rows if r.get("pipeline")}}
    done = [r for r in rows if r.get("status") in ("graded", "not_scored")]
    md = ["# Call review, %s" % label, "",
          "For the owner first: read these notes and touch them up before a caller sees them. Scores follow the team's "
          "rubrics (skills/*-coach/references/rubric.md); totals within 5 points of each other are the same score.", ""]
    by_caller: dict[str, list] = defaultdict(list)
    for r in done:
        by_caller[r.get("caller") or "a caller the log does not name"].append(r)
    for who, rs in sorted(by_caller.items()):
        md += ["## %s" % who, ""]
        for r in sorted(rs, key=lambda x: (x.get("kind") != "best", x.get("at") or "")):
            md.append("### %s call, %s, %s: %s" % ((r.get("kind") or "picked").capitalize(), _when(r.get("at"), tz),
                                                    _length(r.get("seconds")), _score_text(r)))
            md.append("")
            md.append(_note_line(r, names.get(r.get("pipeline"), {})))
            for p in r.get("problems") or []:
                md.append("")
                md.append("Check: %s." % p)
            md += ["", "<details><summary>The full report</summary>", "", r.get("report") or "", "", "</details>", ""]
    left = [r for r in rows if r.get("status") not in ("graded", "not_scored")]
    if left:
        md += ["## Not graded", ""]
        for r in left:
            md.append("- %s, %s call of %s: %s" % (r.get("caller") or "a caller", r.get("kind") or "picked",
                                                  _when(r.get("at"), tz), r.get("why") or r.get("status")))
        md.append("")
    md_path = d / ("call_review_%s.md" % slug)
    md_path.write_text(grade.clean("\n".join(md)), encoding="utf-8")

    calls = [[r.get("caller") or "", r.get("kind") or "", _when(r.get("at"), tz), _length(r.get("seconds")),
              (r.get("pipeline") or "").replace("_", " "), r.get("call_type") or "", r.get("total"), r.get("band") or "",
              r.get("auto_fail") or "", r.get("status") or "", r.get("why") or ""] for r in rows]
    notes = [[r.get("caller") or "", r.get("kind") or "", _when(r.get("at"), tz), _note_line(r, names.get(r.get("pipeline"), {})),
              "; ".join(r.get("problems") or [])] for r in done]
    usd = sum(float(r.get("usd") or 0) for r in rows if not r.get("reused"))
    sources = [["What", "This review"], ["Calls", "picked by the coaching_picks stage from the account's call log"],
               ["Transcription", "an audio model through OpenRouter, with delivery notes"],
               ["Grading", "Claude (%s), against the coach skills' rubrics" % grade.model_of()],
               ["Paid this run", "$%.2f" % usd],
               ["Reliability", "independent graders agree within about 3 points; treat totals within 5 as the same"]]
    xlsx = write_workbook(d / ("call_review_%s.xlsx" % slug), [
        ("Calls", ["Caller", "Pick", "When", "Length", "Pipeline", "Call type", "Total", "Band", "Auto-fail", "Status", "Why"], calls),
        ("Notes for the owner", ["Caller", "Pick", "When", "Note", "Check"], notes),
        ("Sources", sources[0], sources[1:])])
    return [md_path, xlsx]


# ---------------------------------------------------------------------- the channel
def _caller_line(who: str, rs: list[dict], names_for) -> str:
    parts = []
    for kind in ("best", "worst"):
        got = [r for r in rs if r.get("kind") == kind]
        if not got:
            continue
        vals = [_fmt_total(r["total"]) if r.get("total") is not None else
                ("short call" if r.get("call_type") == "short" else "not scored") for r in got]
        parts.append("%s %s" % (kind, " and ".join(vals)))
    worst = [r for r in rs if r.get("kind") == "worst" and r.get("criterion")]
    fix = ""
    if worst:
        crit = worst[0]["criterion"]
        nm = names_for(worst[0].get("pipeline"), crit)
        fix = "; fix first: %s%s" % (crit, (" " + nm) if nm else "")
    return "%s: %s%s." % (who, ", ".join(parts) or "no grade", fix)


def render(task: dict, result: dict) -> str:
    calls = result.get("calls") or []
    label = result.get("label") or "the picked calls"
    if result.get("nothing"):
        return result["nothing"]
    probs = result.get("problems") or []
    tail = [":small_orange_diamond: %s" % probs[0]] if probs else []
    if not result.get("live") or any(c.get("status") == "would_grade" for c in calls):
        opened = sum(1 for c in calls if c.get("status") == "would_grade")
        mode = "dry: nothing transcribed or graded" if not result.get("live") else "nothing paid"
        head = ":clipboard: *Call review, %s* _(%s)_: %d call(s) picked, %d recording(s) open." % (
            label, mode, len(calls), opened)
        if opened:
            head += " Grading them would cost about $%.2f." % float(result.get("would_usd") or 0)
        return "\n".join([head] + tail)
    graded = [c for c in calls if c.get("status") in ("graded", "not_scored")]
    head = ":clipboard: *Call review, %s*: %d of %d call(s) graded, $%.2f." % (label, len(graded), len(calls),
                                                                            float(result.get("usd") or 0))
    if not graded:
        return "\n".join([head + " Nothing to read yet."] + (tail or [":small_orange_diamond: %s" % (
            Counter(c.get("why") for c in calls).most_common(1)[0][0] if calls else "no call was graded")]))
    head += " For you first: touch up the notes before a caller sees them."
    cache: dict = {}

    def names_for(pipeline, crit):
        if pipeline not in cache:
            cache[pipeline] = _names(pipeline) if pipeline else {}
        return cache[pipeline].get(crit, "")
    by: dict[str, list] = defaultdict(list)
    for c in graded:
        by[c.get("caller") or "unnamed"].append(c)
    lines = [head]
    for who, rs in sorted(by.items())[:3]:
        lines.append(_caller_line(who, rs, names_for))
    if len(by) > 3:
        lines.append("And %d more caller(s) in the notes." % (len(by) - 3))
    links = []
    for name in result.get("files") or []:
        url = slack.file_url(task.get("id") or 0, name)
        if url:
            links.append("%s: %s" % ("Notes" if name.endswith(".md") else "Scores", url))
    if links:
        lines.append("  ".join(links))
    elif result.get("files"):
        lines.append("The notes are on the brain under %s (no public link: AGENTS_PUBLIC_URL is not set)." % (
            Path(result.get("run_dir") or "").name or "its run folder"))
    return "\n".join(lines + tail)

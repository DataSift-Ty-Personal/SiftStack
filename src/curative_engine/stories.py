"""The heir research: for each top-ranked house, who owns it now (every heir and each share), how sure we are, how
to reach each heir, and what could stop a deal, written as heirs.json and a one-page story.md.

    select     pick the houses (the top of the ranking, likely vacant lots left out) -> stories/queue.json
    prepare    one input.json per house (stories/houses/<slug>/), from the ranking, the family tree, the obituary
               index and the probate index
    run        each house as its own headless Claude Code session, in a clean folder outside the repo, following
               heir_method.md; at most --concurrency at once, --house-usd each and --daily-usd a day
    summarize  each house's verdict -> stories/stories_results.json (what the loader reads) and stories_table.md

The research runs on the user's own Claude Code sign-in. A house can also be researched by hand in any Claude Code
session: give it the house folder's input.json and heir_method.md, and have it write heirs.json and story.md into
that folder; summarize reads them the same way.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

from . import common as C
from .counties import county_for

METHOD = Path(__file__).with_name("heir_method.md")
SHARE_FLOOR = 50000
TIMEOUT_S = 80 * 60
IS_WIN = os.name == "nt"
NO_WINDOW = 0x08000000 if IS_WIN else 0
SECRET_RX = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|PASS$|AUTH|COOKIE|CREDENTIAL|SESSION|PRIVATE|WEBHOOK)", re.I)
KEEP_ENV = {"PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "TEMP", "TMP", "HOME", "USERPROFILE",
            "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "HOMEDRIVE", "HOMEPATH",
            "USERNAME", "USER", "LANG", "LC_ALL", "TERM", "SHELL", "TZ", "CLAUDE_CONFIG_DIR", "XDG_CONFIG_HOME",
            "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS", "COMPUTERNAME"}
AUTH_FAIL = ("invalid api key", "please run /login", "not logged in", "authentication_error", "oauth token",
             "credit balance is too low", "/login")
LIMIT_HIT = ("usage limit", "limit reached", "hit your limit", "rate limit", "overloaded")
PATHS = {
    "A": ("one owner on the deed, believed dead; the skip trace found children.", 35, 20),
    "B": ("two names on the deed. Settle both deaths and their order first, then follow the house to the right "
          "estate.", 45, 25),
    "D": ("one owner on the deed, believed dead; the skip trace found no children. Look for a spouse and children "
          "first, then brothers, sisters, nieces and nephews (step 5 of the method).", 45, 25),
    "E": ("one owner who died before 2010. Some heirs may have died since, which sends their shares on to their own "
          "heirs, so follow each share to the living people who hold it now.", 45, 25),
    "X": ("no SmartSkip family tree in the input, so you build the family from scratch.", 45, 25),
}
KEEP = ["street", "city", "zip", "state", "county", "account", "parcel", "assessor_url", "avm", "appraised", "equity",
        "mortgage", "debt", "free_clear", "year_built", "sqft", "beds", "baths", "tax_status", "tax_years", "tax_amount",
        "tax_current_due", "city_years", "city_amount", "city_suit", "turned_over", "last_payor", "payor_kind",
        "last_pay_date", "lien_amount", "lien_flags", "fc_ahead", "in_fc", "sale_day", "deed", "deed_count",
        "roll_owner", "roll_people", "roll_mail", "roll_mail_here", "life_estate", "trust", "estate_named", "last_sale",
        "owner_age", "vacant", "absentee", "oos", "mail_state", "decedents", "death_day2", "months_since_death2",
        "conf_txt", "evidence", "index_checks", "obit_url", "probate_open", "probate_month", "research_status",
        "listed", "mls", "sold_after", "play", "tier", "score", "why", "flags", "verify"]
CRM_ONLY = ["dms", "decision_maker", "dec_record", "status", "lists", "pr"]
ORDER = {"Work now": 0, "Work now, no confirmed number yet": 1, "Research more": 2, "Will or probate on file": 3,
         "Under $50K a share": 4, "Not a house (appraisal far under the estimate)": 5, "Not curative (owner alive)": 6}
NONE_RX = re.compile(r"\s*(none|no\b|not found|nothing|unknown|not checked)", re.I)


# ----------------------------------------------------------------------------------- select
def likely_lot(r: dict) -> bool:
    a, v = r.get("appraised") or 0, r.get("avm") or 0
    return bool(a and v and a < 0.10 * v)


def ranked(scored: list[dict]) -> list[dict]:
    """The houses in play, best first: not ruled out, not a probate deal, not a foreclosure with no death."""
    return [r for r in scored if r.get("tier") != "Out"
            and not str(r.get("play")).startswith(("Probate open: a probate deal", "No death signal"))]


def research_path(r: dict, fam: dict | None) -> str:
    year = int((r.get("death_day2") or "2099")[:4])
    kids = (fam or {}).get("children")
    nk = kids if isinstance(kids, int) else len(kids or [])
    if (r.get("n_owners") or 1) >= 2:
        return "B"
    if not fam:
        return "X"
    if year < 2010:
        return "E"
    return "A" if nk else "D"


def select(ws, top: int = 25, only=(), redo: bool = False, out=print) -> list[dict]:
    scored = ws.read("scored.json", []) or []
    fam = ws.read("family.json", {}) or {}
    pool = ranked(scored)
    queue, seen, skipped = [], set(), []
    for i, r in enumerate(pool, 1):
        if only:
            if C.slug(r["street"]) not in only and r["key"] not in only:
                continue
        elif i > top:
            break
        if likely_lot(r):
            skipped.append({"rank": i, "street": r["street"], "why": "county appraisal %s against a %s estimate"
                            % (C.dollars(r.get("appraised")), C.dollars(r.get("avm")))})
            continue
        s = C.slug(r["street"])
        if s in seen:
            s = "%s-%s" % (s, r.get("zip") or i)
        seen.add(s)
        if not redo and (ws.houses / s / "heirs.json").exists():
            continue
        queue.append({"slug": s, "street": r["street"], "city": r.get("city"), "state": r.get("state"), "key": r["key"],
                      "rank": i, "tier": r["tier"], "path": research_path(r, fam.get(r["key"])),
                      "sale_day": r.get("sale_day") if r.get("fc_ahead") else None, "death_conf": r.get("conf2"),
                      "equity": r.get("equity"), "score": r.get("score")})
    C.write_json(ws.stories / "queue.json", queue)
    C.write_json(ws.stories / "queue_skipped.json", skipped)
    out("heir research queue: %d house(s)%s" % (len(queue), (", %d likely vacant lots left out" % len(skipped)) if skipped else ""))
    return queue


# ----------------------------------------------------------------------------------- inputs
def probate_matches(r: dict, index) -> dict | None:
    if index is None or not index.available():
        return None
    index._load()
    died = C.parse_day(r.get("death_day2"))
    matches = []
    for person in list(r.get("deed") or [])[:3] + list(r.get("decedents") or [])[:2]:
        cases = index.lookup(person, died) if died else []
        if cases and not any(C.same_person(person, m["person"]) for m in matches):
            matches.append({"person": person, "died": C.iso(died), "cases": cases[:4]})
    cty = county_for(fips=r.get("fips") or "")
    return {"source": cty.probate_name or "the probate index",
            "covers_to": C.iso(index.covers_to), "matches": matches}


def build_input(r: dict, fam: dict | None, obits: dict, index=None) -> dict:
    d = {k: r.get(k) for k in KEEP if k in r}
    d["n_owners"] = r.get("n_owners")
    d["family_tree"] = ({"source": "SmartSkip owner trace", "heir_order_guess": fam.get("heir_order"),
                         "subjects": fam.get("subjects"), "relatives": fam.get("relatives")} if fam else None)
    d["crm_says"] = {k: r.get(k) for k in CRM_ONLY if r.get(k)} or None
    rows = {}
    for person in list(r.get("deed") or [])[:3] + list(r.get("decedents") or [])[:3]:
        t = C.name_toks(person)
        if len(t) >= 2:
            k = "%s|%s|%s" % (r.get("fips") or "", t[-1], t[0])
            if k in (obits or {}) and k not in rows:
                rows[k] = obits[k]
    d["obituary_index_rows"] = rows or None
    d["probate_index"] = probate_matches(r, index)
    return d


def prepare(ws, out=print) -> int:
    queue = C.read_json(ws.stories / "queue.json", []) or []
    scored = {r["key"]: r for r in (ws.read("scored.json", []) or [])}
    fam = ws.read("family.json", {}) or {}
    obits = ws.read("obits.json", {}) or {}
    indexes = {}
    n = 0
    for q in queue:
        r = scored.get(q["key"])
        if not r:
            continue
        fips = r.get("fips") or ""
        if fips not in indexes:
            cty = county_for(fips=fips)
            indexes[fips] = cty.probate_index(ws.work) if cty.probate_sheet else None
        d = build_input(r, fam.get(r["key"]), obits, indexes[fips])
        d.update(slug=q["slug"], research_path=q["path"])
        folder = ws.houses / q["slug"]
        (folder / "raw").mkdir(parents=True, exist_ok=True)
        C.write_json(folder / "input.json", d)
        n += 1
    out("inputs written for %d house(s) in %s" % (n, ws.houses))
    return n


# ----------------------------------------------------------------------------------- the brief
def tools_header(ws, slug: str, env: dict) -> str:
    py = "python"
    cli = (C.SRC / "curative_title.py").as_posix()
    lines = ["# This run", "",
             "This folder is your house folder and your working directory. Write heirs.json and story.md here. "
             "The method below is everything you need; read nothing else on this computer.", "", "Tools you may run:"]
    if env.get("ENFORMION_AP_NAME") and env.get("ENFORMION_AP_PASSWORD"):
        lines.append('- People search (ages, addresses, phones, relatives for one person; capped per house): '
                     '`%s "%s" people --dir "%s" --house %s --first <F> --last <L> [--middle <M>] [--street "<line 1>"] '
                     '[--city <C> --state <ST>] [--zip <Z>] [--dob-year <YYYY>]`. A name alone is refused: anchor it with '
                     'a city and state, a street, a zip or a birth year. For a common name add the street line, and check '
                     'the age.' % (py, cli, ws.root.as_posix(), slug))
    else:
        lines.append("- No people-search tool is set up on this machine: use the free sources in the method.")
    if env.get("SCRAPFLY_KEY"):
        lines.append('- A page that refuses a plain read (many funeral home sites): `%s "%s" "<url>"`.'
                     % (py, (C.SRC / "scrapfly_browser.py").as_posix()))
    lines.append("- WebSearch and WebFetch.")
    return "\n".join(lines) + "\n\n"


def brief(item: dict, limits: tuple[int, int, int] | None = None) -> str:
    desc, searches, pages = PATHS.get(item.get("path") or "X", PATHS["X"])
    if limits:
        searches, pages = limits[0] or searches, limits[1] or pages
    where = ", ".join(x for x in (item.get("street"), item.get("city"), item.get("state")) if x)
    parts = ["Research the house below by following the method in CLAUDE.md in this folder exactly.", "",
             "House: %s. Slug: %s. Start with input.json in this folder." % (where, item["slug"]),
             "Research path: " + desc]
    if item.get("sale_day"):
        parts.append("A foreclosure sale is set for %s." % item["sale_day"])
    if (item.get("death_conf") or 0) < 3:
        parts.append("The death is not confirmed yet: settle it first. If the owner is alive, say so with the evidence "
                     "and stop there (write heirs.json with alive \"yes\" and a short story.md).")
    parts += ["Limits: up to %d web searches, %d page reads, 10 people-search lookups. Use the budget before you leave "
              "an heir unproven." % (searches, pages), "",
              "Write heirs.json and story.md in this folder, then reply as the method says (five lines or fewer)."]
    return "\n".join(parts)


def child_env(auth: str = "plan") -> dict:
    """The research session's environment: no secrets at all (the tools read the repo's .env themselves), the
    interpreter running this first on PATH so `python` is this one. With auth "plan" any Anthropic API key is
    left out so Claude Code uses its own sign-in; "api" passes ANTHROPIC_API_KEY through."""
    env = {}
    for k, v in os.environ.items():
        if k.upper() in KEEP_ENV or (k.upper().startswith("LC_")):
            env[k] = v
        elif not SECRET_RX.search(k) and not k.upper().startswith(("ANTHROPIC", "CLAUDECODE", "CLAUDE_CODE")):
            env[k] = v
    if auth == "api" and os.environ.get("ANTHROPIC_API_KEY"):
        env["ANTHROPIC_API_KEY"] = os.environ["ANTHROPIC_API_KEY"]
    py_dir = os.path.dirname(sys.executable)
    env["PATH"] = py_dir + os.pathsep + env.get("PATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def command(claude: str, model: str, house_usd: float) -> list[str]:
    return [claude, "-p", "--model", model, "--output-format", "json", "--max-budget-usd", str(house_usd),
            "--strict-mcp-config", "--allowedTools", "WebSearch", "WebFetch", "Read", "Write", "Edit", "Glob", "Grep",
            "Bash(python:*)"]


# ----------------------------------------------------------------------------------- the runner
class Runner:
    def __init__(self, ws, *, concurrency: int = 3, daily_usd: float = 100.0, house_usd: float = 10.0,
                 model: str = "opus", auth: str = "plan", run_dir=None, claude: str | None = None, out=print,
                 popen=None, now=None):
        self.ws, self.concurrency, self.daily_usd, self.house_usd = ws, max(1, int(concurrency)), float(daily_usd), float(house_usd)
        self.model, self.auth, self.out = model, auth, out
        self.run_dir = Path(run_dir or os.environ.get("CURATIVE_RUN_DIR") or
                            (Path(tempfile.gettempdir()) / "siftstack_curative" / ws.market))
        self.claude = claude or shutil.which("claude") or ""
        self.popen = popen or subprocess.Popen
        self.now = now or dt.datetime.now
        self.state_path = ws.stories / "run_state.json"
        self.log_path = ws.stories / "run_log.txt"
        self.stop_path = ws.stories / "STOP"
        self.env = C.load_env()

    def log(self, msg: str) -> None:
        line = "%s  %s" % (self.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
        self.ws.stories.mkdir(parents=True, exist_ok=True)
        with open(self.log_path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        try:
            self.out(line)
        except (OSError, UnicodeEncodeError, ValueError):
            pass

    def state(self) -> dict:
        return C.read_json(self.state_path, {"houses": {}, "pause_until": None}) or {"houses": {}, "pause_until": None}

    def save(self, st: dict) -> None:
        C.write_json(self.state_path, st)

    def spent_today(self, st: dict) -> float:
        day = self.now().strftime("%Y-%m-%d")
        return sum(h.get("usd") or 0 for h in st["houses"].values() if str(h.get("finished") or "").startswith(day))

    def claude_md(self, slug: str) -> str:
        return tools_header(self.ws, slug, self.env) + METHOD.read_text(encoding="utf-8")

    def prepare_folder(self, item: dict) -> Path:
        d = self.run_dir / item["slug"]
        d.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.ws.houses / item["slug"] / "input.json", d / "input.json")
        (d / "CLAUDE.md").write_text(self.claude_md(item["slug"]), encoding="utf-8")
        for stale in ("heirs.json", "story.md", "result.json", "err.txt"):
            if (d / stale).exists():
                os.replace(d / stale, d / (stale + ".old"))
        return d

    def launch(self, item: dict):
        d = self.prepare_folder(item)
        outf = open(d / "result.json", "w", encoding="utf-8")
        errf = open(d / "err.txt", "w", encoding="utf-8")
        kw = {"cwd": str(d), "env": child_env(self.auth), "stdin": subprocess.PIPE, "stdout": outf, "stderr": errf,
              "text": True, "encoding": "utf-8"}
        if IS_WIN:
            kw["creationflags"] = NO_WINDOW
        p = self.popen(command(self.claude, self.model, self.house_usd), **kw)
        p.stdin.write(brief(item))
        p.stdin.close()
        return p, outf, errf

    def collect(self, item: dict) -> dict:
        d = self.run_dir / item["slug"]
        res = C.read_json(d / "result.json", {}) or {}
        dest = self.ws.houses / item["slug"]
        for name in os.listdir(d):
            if name in ("CLAUDE.md", "input.json") or name.endswith(".old"):
                continue
            src = d / name
            if src.is_dir():
                shutil.copytree(src, dest / name, dirs_exist_ok=True)
            else:
                shutil.copyfile(src, dest / (("session_" + name) if name in ("result.json", "err.txt") else name))
        return res

    def kill(self, p) -> None:
        if IS_WIN:
            subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True)
        else:
            p.kill()

    def run(self, queue: list[dict], limit: int = 0, wait: bool = True) -> dict:
        if not self.claude:
            raise SystemExit("Claude Code (the `claude` command) is not on this machine's PATH. Install it and sign in "
                             "once (run `claude`), or research the houses by hand: `curative_title.py stories prepare` "
                             "writes each house's input.json, and heir_method.md is the method.")
        st = self.state()
        for h in st["houses"].values():
            if h.get("status") == "running":          # a runner that died leaves "running" rows: those start again
                h["status"] = "queued"
        running: dict = {}
        started = 0
        self.log("runner start: %d house(s), %d at a time, $%.0f a day, $%.0f a house, model %s"
                 % (len(queue), self.concurrency, self.daily_usd, self.house_usd, self.model))
        while True:
            for slug in list(running):
                p, outf, errf, item, t0 = running[slug]
                code = p.poll()
                if code is None and time.time() - t0 < TIMEOUT_S:
                    continue
                if code is None:
                    self.kill(p)
                    code = "timeout"
                outf.close()
                errf.close()
                res = self.collect(item)
                err_text = ""
                try:
                    err_text = (self.run_dir / slug / "err.txt").read_text(encoding="utf-8", errors="replace")[-400:]
                except OSError:
                    pass
                h = st["houses"].setdefault(slug, {})
                h.update(finished=self.now().isoformat(timespec="seconds"), usd=round(res.get("total_cost_usd") or 0, 2),
                         minutes=round((time.time() - t0) / 60, 1), turns=res.get("num_turns"), exit=code)
                have = (self.ws.houses / slug / "heirs.json").exists() and (self.run_dir / slug / "heirs.json").exists()
                said = ("%s %s %s" % (res.get("result") or "", res.get("subtype") or "", err_text)).lower()
                if not have and any(s in said for s in AUTH_FAIL):
                    h["status"], h["note"] = "queued", "Claude Code is not signed in"
                    self.save(st)
                    self.log("%s: Claude Code is not signed in on this machine. Run `claude` once and sign in (or pass "
                             "--auth api to use ANTHROPIC_API_KEY), then run this again." % slug)
                    for s2 in list(running):
                        if s2 != slug:
                            self.kill(running[s2][0])
                    return {"stopped": "not signed in", "state": st}
                if not have and any(s in said for s in LIMIT_HIT):
                    h["status"], h["note"] = "queued", "usage limit; tried again later"
                    st["pause_until"] = (self.now() + dt.timedelta(minutes=45)).isoformat(timespec="seconds")
                    self.log("%s: usage limit hit, pausing 45 minutes" % slug)
                elif have:
                    h["status"], h["note"] = "done", ""
                    self.log("%s: done in %.1f min, $%.2f" % (slug, h["minutes"], h["usd"]))
                else:
                    h["status"] = "failed"
                    h["note"] = ((res.get("subtype") or "") + " " + (res.get("result") or err_text or "")).strip()[:160]
                    self.log("%s: FAILED (%s) %s" % (slug, code, h["note"]))
                del running[slug]
                self.save(st)
            stopping = self.stop_path.exists()
            paused = st.get("pause_until") and self.now().isoformat() < st["pause_until"]
            over = self.spent_today(st) + 4.0 * len(running) >= self.daily_usd
            todo = [q for q in queue if st["houses"].get(q["slug"], {}).get("status") not in ("done", "running", "failed")]
            if limit:
                todo = todo[:max(0, limit - started)]
            if not running and (stopping or not todo):
                self.log("runner exit: %s" % ("STOP file" if stopping else "queue finished"))
                break
            if not running and (over or paused) and not wait:
                self.log("runner exit: %s" % ("daily usage cap reached" if over else "paused after a usage limit"))
                break
            while not stopping and not paused and not over and todo and len(running) < self.concurrency:
                item = todo.pop(0)
                try:
                    p, outf, errf = self.launch(item)
                except Exception as e:  # noqa: BLE001
                    st["houses"][item["slug"]] = {"status": "failed", "note": "launch: %s" % str(e)[:150],
                                                  "finished": self.now().isoformat(timespec="seconds")}
                    self.save(st)
                    self.log("%s: launch failed: %s" % (item["slug"], e))
                    continue
                running[item["slug"]] = (p, outf, errf, item, time.time())
                st["houses"][item["slug"]] = {"status": "running", "started": self.now().isoformat(timespec="seconds")}
                started += 1
                self.save(st)
                self.log("%s: started (rank %s, path %s)" % (item["slug"], item.get("rank"), item.get("path")))
                over = self.spent_today(st) + 4.0 * len(running) >= self.daily_usd
            if over and not running:
                self.log("daily usage cap reached ($%.2f today); waiting for tomorrow" % self.spent_today(st))
                time.sleep(600)
                continue
            time.sleep(15 if running else 1)
        return {"state": st}


def status(ws, out=print) -> dict:
    st = C.read_json(ws.stories / "run_state.json", {"houses": {}}) or {"houses": {}}
    q = C.read_json(ws.stories / "queue.json", []) or []
    c = Counter(h.get("status") for h in st["houses"].values())
    done = [h for h in st["houses"].values() if h.get("status") == "done"]
    tot = sum(h.get("usd") or 0 for h in st["houses"].values())
    out("queue %d | %s | not started %d" % (len(q), dict(c), len([x for x in q if x["slug"] not in st["houses"]])))
    out("usage: total $%.2f, per finished house $%.2f (Claude Code's own estimate)" % (tot, tot / len(done) if done else 0))
    for slug, h in sorted(st["houses"].items(), key=lambda kv: kv[1].get("finished") or "")[-6:]:
        out("  %-34s %-8s $%-6s %s min %s" % (slug, h.get("status"), h.get("usd"), h.get("minutes"), h.get("note") or ""))
    return {"counts": dict(c), "usd": round(tot, 2)}


# ----------------------------------------------------------------------------------- the verdict
def share_num(h: dict) -> float | None:
    v = h.get("share_num")
    if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
        return float(v)
    m = re.match(r"\s*(\d+)\s*/\s*(\d+)", str(h.get("share") or ""))
    return int(m.group(1)) / int(m.group(2)) if m and int(m.group(2)) else None


def occupied(d: dict) -> bool:
    occ = str((d or {}).get("occupancy") or "")
    if re.match(r"\s*(unknown|probably nobody|reads vacant|not owner occupied)", occ, re.I):
        return False
    return bool(re.search(r"\b(lives?|living) in the house\b|\bboth live in the house\b|\blists the house as (her|his) "
                          r"current address\b|^\s*occupied\b", occ, re.I))


def index_case(pi: dict) -> str | None:
    """A sure will or open estate in the input's probate index rows, for research that wrote nothing about it."""
    for m in (pi or {}).get("matches") or []:
        for c in m.get("cases") or []:
            if not c.get("sure"):
                continue
            if c.get("kind") in ("will", "open"):
                return "%s %s, %s, opened %s, %s (from the probate index)" % (
                    c.get("docket"), c.get("title"), c.get("type"), c.get("open"), c.get("status"))
    return None


def verdict(d: dict, inp: dict, checked: list | None) -> tuple[str, list, float | None, int | None]:
    """(verdict, notes, equity per heir at the smallest share, heirs with a confirmed number)."""
    holders = d.get("title_holders") or []
    if holders and all(str(t.get("alive", "")).lower() == "yes" for t in holders):
        return "Not curative (owner alive)", [], None, None
    avm, appr, equity = inp.get("avm") or 0, inp.get("appraised") or 0, inp.get("equity") or 0
    if avm and appr and appr < 0.10 * avm:
        return ("Not a house (appraisal far under the estimate)",
                ["county appraisal %s against a %s estimate: likely a vacant lot or a teardown"
                 % (C.dollars(appr), C.dollars(avm))], None, None)
    heirs = [h for h in d.get("heirs") or [] if str(h.get("living", "")).lower() != "no"]
    shares = [s for s in (share_num(h) for h in heirs) if s]
    per = round(equity * min(shares)) if shares and equity else None
    notes = []
    wp = str(d.get("will_or_probate") or "").strip()
    if not wp:
        wp = index_case(inp.get("probate_index") or {}) or ""
    if re.match(r"\s*closed intestate estate", wp, re.I):
        notes.append("closed intestate estate on file: the court file names the heirs")
    elif wp and not NONE_RX.match(wp):
        return "Will or probate on file", ["will or probate: " + wp[:160]], per, None
    conf = str(d.get("confidence") or "").lower()
    weak = [h for h in heirs if str(h.get("confidence", "")).lower() not in ("high", "medium")]
    if occupied(d):
        notes.append("an heir lives in the house: ask what they want before an offer")
    reach = None
    if checked is not None:
        good = {r["heir"] for r in checked if r.get("match") and r.get("grade") in ("A", "B") and not r.get("litigator")}
        reach = len(good)
        notes.append("heirs with a confirmed number: %d of %d" % (reach, len(heirs)))
    if per is not None and per < SHARE_FLOOR:
        v = "Under $50K a share"
    elif conf in ("high", "medium") and heirs and not weak and per:
        v = "Work now" if (reach is None or reach >= 1) else "Work now, no confirmed number yet"
    else:
        v = "Research more"
    return v, notes, per, reach


def collect(ws) -> tuple[list[dict], list[dict]]:
    """Every researched house (a folder with heirs.json or a person's heirs_verified.json) and its verdict;
    the best per street, verdict order first."""
    queue = {q["slug"]: q for q in (C.read_json(ws.stories / "queue.json", []) or [])}
    rows = []
    for folder in sorted(ws.houses.glob("*")) if ws.houses.exists() else []:
        if not folder.is_dir():
            continue
        hv, hj = folder / "heirs_verified.json", folder / "heirs.json"
        path = hv if hv.exists() else hj
        if not path.exists():
            continue
        d = C.read_json(path)
        inp = C.read_json(folder / "input.json")
        if not isinstance(d, dict) or not isinstance(inp, dict):
            continue
        checked = C.read_json(folder / "phones_checked.json")
        v, notes, per, reach = verdict(d, inp, checked if isinstance(checked, list) else None)
        living = [h for h in d.get("heirs") or [] if str(h.get("living", "")).lower() != "no"]
        q = queue.get(folder.name, {})
        res = C.read_json(folder / "session_result.json") or {}
        rows.append({"slug": folder.name, "street": inp.get("street") or q.get("street"), "city": inp.get("city"),
                     "zip": inp.get("zip"), "rank": q.get("rank"),
                     "sale_day": inp.get("sale_day") if inp.get("fc_ahead") else None, "equity": inp.get("equity"),
                     "whose_estate": d.get("whose_estate"), "heirs": len(living),
                     "heir_list": ["%s (%s)" % (h.get("name"), h.get("share")) for h in living],
                     "confidence": d.get("confidence"), "equity_per_heir": per, "reach": reach, "verdict": v,
                     "notes": notes, "open": (d.get("open_questions") or [])[:3], "counts": d.get("counts"),
                     "usage": {"usd": round(res.get("total_cost_usd") or 0, 2),
                               "minutes": round((res.get("duration_ms") or 0) / 60000, 1)} if res else None,
                     "story": str(folder / "story.md"), "verified": hv.exists()})
    best: dict = {}
    for r in rows:
        k = r["street"]
        if k not in best or r["verified"]:
            best[k] = r
    ordered = sorted(best.values(), key=lambda r: (ORDER.get(r["verdict"], 9), -(r["equity_per_heir"] or 0)))
    return rows, ordered


def summarize(ws, out=print) -> list[dict]:
    rows, best = collect(ws)
    C.write_json(ws.stories / "stories_results.json", rows)
    lines = ["| # | House | Rank before | Verdict | Heirs | Equity each (smallest share) | Confidence | Sale | Notes |",
             "|---|---|---|---|---|---|---|---|---|"]
    for i, r in enumerate(best, 1):
        lines.append("| %d | %s | %s | %s | %s | %s | %s | %s | %s |" % (
            i, r["street"], ("#%s" % r["rank"]) if r.get("rank") else "-", r["verdict"], r["heirs"],
            C.dollars(r["equity_per_heir"]) or "-", r["confidence"] or "-", r["sale_day"] or "-", "; ".join(r["notes"])))
    tot = sum((r["usage"] or {}).get("usd", 0) for r in rows)
    (ws.stories / "stories_table.md").write_text("\n".join(lines) + "\n\nResearch runs: %d, usage $%.2f\n" % (len(rows), tot),
                                                 encoding="utf-8", newline="\n")
    out("houses with a story: %d | verdicts %s" % (len(best), dict(Counter(r["verdict"] for r in best))))
    return best


def load(ws) -> dict:
    """street -> the house's best heir story summary, for the score and the workbook."""
    if not ws.houses.exists():
        return {}
    _, best = collect(ws)
    return {r["street"]: r for r in best}


# ----------------------------------------------------------------------------------- people search (Enformion)
def people_search(ws, house: str, first: str, last: str, middle: str = "", street: str = "", city: str = "",
                  state: str = "", zip5: str = "", dob_year: str = "", max_people: int = 3, post=None) -> dict:
    """One Enformion person search for a research session, capped per house and per run, with the raw reply kept
    in the house's raw/ folder. Uses the account's own Enformion access profile (ENFORMION_AP_NAME / _PASSWORD)."""
    if not re.fullmatch(r"[a-z0-9-]+", house or "") or not (ws.houses / house).is_dir():
        return {"error": "unknown house folder: %s" % house}
    if str(C.SRC) not in sys.path:
        sys.path.insert(0, str(C.SRC))
    import enformion_heir as E
    if post is None and not E.is_configured():
        return {"error": "Enformion is not set up on this machine (ENFORMION_AP_NAME / ENFORMION_AP_PASSWORD)"}
    addr = {}
    if street:
        addr["AddressLine1"] = street
    line2 = " ".join(p for p in [(city + ",") if city else "", state, zip5] if p).strip()
    if line2:
        addr["AddressLine2"] = line2
    if not addr and not dob_year:
        return {"error": "anchor the search: a city and state, a street, a zip or a birth year"}
    house_cap = int(os.environ.get("CURATIVE_PEOPLE_HOUSE_CAP", "10"))
    run_cap = int(os.environ.get("CURATIVE_PEOPLE_RUN_CAP", "300"))
    ledger = ws.stories / "vendor" / "people_search_ledger.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    n_house = n_all = 0
    if ledger.exists():
        for ln in ledger.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(ln)
            except ValueError:
                continue
            n_all += 1
            n_house += row.get("house") == house
    if n_house >= house_cap or n_all >= run_cap:
        return {"error": "lookup cap reached", "house_used": n_house, "house_cap": house_cap, "run_used": n_all,
                "run_cap": run_cap, "note": "carry on with the free sources and list every heir who still needs a "
                                            "lookup under open questions"}
    with open(ledger, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": dt.datetime.now().isoformat(timespec="seconds"), "house": house}) + "\n")
    body = {"FirstName": first, "LastName": last, "Page": 1, "ResultsPerPage": max(1, min(int(max_people), 10))}
    if middle:
        body["MiddleName"] = middle
    if addr:
        body["Addresses"] = [addr]
    if dob_year:
        body["Dob"] = dob_year
    try:
        if post is not None:
            data = post(body)
        else:
            import requests
            r = requests.post(E.PERSON_SEARCH_URL, headers=E._headers(), json=body, timeout=45)
            if r.status_code != 200:
                return {"error": "HTTP %s" % r.status_code, "detail": r.text[:300]}
            data = r.json()
    except Exception as exc:  # noqa: BLE001
        return {"error": "request failed: %s" % str(exc)[:200]}
    persons = (data or {}).get("persons") or []
    raw = ws.houses / house / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    tag = "%s_%s" % (re.sub(r"[^a-z]", "", last.lower()), re.sub(r"[^a-z]", "", first.lower()))
    C.write_json(raw / ("people_%s_%s.json" % (dt.datetime.now().strftime("%H%M%S"), tag)), data, indent=None)

    def nm(n):
        if not isinstance(n, dict):
            return str(n or "")
        return " ".join(x for x in (n.get("firstName"), n.get("middleName"), n.get("lastName"), n.get("suffix")) if x)

    def view(p):
        return {"name": nm(p.get("name")) or p.get("fullName") or "",
                "other_names": [nm(n) for n in (p.get("akas") or [])[:6] if nm(n)] or None,
                "age": p.get("age"), "dob": p.get("dob"), "dod": E.extract_dod(p) if post is None else p.get("dod"),
                "addresses": [{"address": a.get("fullAddress"), "first_seen": a.get("firstReportedDate"),
                               "last_seen": a.get("lastReportedDate"), "order": a.get("addressOrder")}
                              for a in (p.get("addresses") or [])[:6] if isinstance(a, dict)],
                "phones": [{"number": C.digits(ph.get("phoneNumber")), "type": ph.get("phoneType"),
                            "connected": ph.get("isConnected"), "last_seen": ph.get("lastReportedDate")}
                           for ph in (p.get("phoneNumbers") or [])[:6] if isinstance(ph, dict) and C.digits(ph.get("phoneNumber"))],
                "relatives": [{"name": nm(r), "type": r.get("relativeType"), "place": ", ".join(
                    x for x in (r.get("city"), r.get("state")) if x) or None, "deceased": r.get("isDeceased")}
                    for r in (p.get("relativesSummary") or [])[:30] if isinstance(r, dict)]}
    return {"people_found": len(persons), "house_lookups_used": n_house + 1, "house_cap": house_cap,
            "people": [view(p) for p in persons[:max(1, min(int(max_people), 10))]]}

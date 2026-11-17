"""
monitor.py - Caller ID Reputation Monitor (Telnyx-only, fused L1+L2).

Keeps the outbound cold-call DIDs (SmrtPhone / Telnyx) out of "Spam Likely / Scam
Likely", detects degradation early, and remediates hands-off. One vendor, one API,
one bill.

Two live signal layers, fused worst-of into one status per DID:
  L1  Telnyx CDR        our own outbound call outcomes (ASR / ALOC / short-call %).
                        Ground truth, free, needs only Telnyx API access.  telnyx_cdr.py
  L2  Telnyx Number     carrier-grade spam_risk + spam_category (Hiya-fed, aggregate).
      Reputation        cached read free; fresh recheck billable on degrade; auto
                        remediation on a flag.                            telnyx_reputation.py

IPQS (the old L3 proxy) is RETIRED - CDR replaces it as the free daily signal and is
strictly better (real call outcomes vs a risk proxy that read "Clean" while a carrier
had flagged a number).

Graceful tiering (so it is safe to run TODAY before creds land):
  no TELNYX_API_KEY          -> idle: rebuild dashboard from state, no API calls.
  TELNYX_API_KEY only        -> L1 (CDR) runs. L2 skipped until reputation is enabled.
  TELNYX_API_KEY + _ENTERPRISE_ID -> full L1 + L2 + remediation.

USAGE
  python monitor.py                    # DRY: no API calls, rebuild dashboard, print plan
  python monitor.py --commit           # LIVE: pull CDR (+reputation if enabled), fuse, alert
  python monitor.py --commit --day 2026-07-05   # pull a specific UTC day of CDRs
  python monitor.py --commit --no-remediate     # do everything except SUBMIT remediation
  python monitor.py --dashboard-only   # just rebuild the HTML from existing state
  python monitor.py --commit --always-notify    # Slack heartbeat even when all clean

Env (.env (next to the scripts) or process env): TELNYX_API_KEY, TELNYX_ENTERPRISE_ID (once L2 is
enabled), SLACK_WEBHOOK_URL. One-time L2 onboarding: telnyx_setup.py.

Exit codes: 0 = ran cleanly; 2 = config pre-flight abort; 1 = scan error (no data).
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import urllib.request
from pathlib import Path

import smrtphone_cdr
import telnyx_cdr
import telnyx_reputation as telrep
import dashboard as dash

# ---- locations ----
SUBDIR = Path(__file__).resolve().parent
CONFIG_DIR = SUBDIR / "config"
STATE_DIR = SUBDIR / "state"
OUTPUT_DIR = SUBDIR / "output"
LOG_DIR = SUBDIR / "logs"
NUMBERS_FILE = CONFIG_DIR / "numbers.json"
THRESHOLDS_FILE = CONFIG_DIR / "thresholds.json"
STATE_FILE = STATE_DIR / "numbers_state.json"
LEDGER_FILE = STATE_DIR / "remediation_ledger.json"
DASHBOARD_FILE = OUTPUT_DIR / "caller_reputation.html"
ACTIVE_POOL_FILE = OUTPUT_DIR / "active_pool.json"
ENV_FILE = Path(os.environ.get("REPUTATION_ENV_FILE", str(SUBDIR / ".env")))

HISTORY_CAP = 60

SEVERITY = {"Pending": -1, "Clean": 0, "Watch": 1, "Flagged": 2}

# ---- logging ----
_LOG_FH = None


def log(msg: str = "") -> None:
    line = f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    if _LOG_FH:
        _LOG_FH.write(line + "\n")
        _LOG_FH.flush()


def open_log() -> None:
    global _LOG_FH
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    _LOG_FH = open(LOG_DIR / f"monitor_{stamp}.log", "a", encoding="utf-8")


# ---- env / io ----
def load_env_file() -> dict:
    env = {}
    if ENV_FILE.exists():
        for raw in ENV_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def load_json(path: Path, default=None):
    if not path.exists():
        return default
    text = path.read_text(encoding="utf-8-sig").strip()
    if not text:
        return default
    return json.loads(text)


def save_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")


def today() -> str:
    return datetime.date.today().isoformat()


def digits(raw: str) -> str:
    d = "".join(ch for ch in str(raw) if ch.isdigit())
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    return d


def e164(num10: str) -> str:
    return "+1" + num10 if len(num10) == 10 else num10


def pretty(num10: str) -> str:
    if len(num10) == 10:
        return f"({num10[:3]}) {num10[3:6]}-{num10[6:]}"
    return num10


# ---- Slack ----
def notify(text: str, env: dict, enabled: bool) -> None:
    if not enabled:
        log("[notify] suppressed (--no-notify)")
        return
    url = env.get("SLACK_WEBHOOK_URL") or os.environ.get("SLACK_WEBHOOK_URL")
    if not url:
        log("[notify] no SLACK_WEBHOOK_URL set; skipping notification")
        return
    try:
        req = urllib.request.Request(
            url, data=json.dumps({"text": text}).encode(),
            headers={"content-type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=10)
        log("[notify] sent")
    except Exception as e:  # noqa: BLE001
        log(f"[notify] FAILED: {e}")


# ---- L1 scoring ----
def classify_cdr(m: dict | None, l1: dict) -> tuple[str, list[str]]:
    """Map per-DID CDR metrics to a status. L1 tops out at Watch (early warning):
    a behavioral breach makes a DID at least Watch, never Flagged on its own."""
    if not m:
        return "Pending", []
    dials = int(m.get("dials") or 0)
    if dials == 0:
        if l1.get("dead_day_is_watch"):
            return "Watch", ["dead day (0 dials)"]
        return "Pending", []
    if dials < int(l1.get("min_dials_for_scoring", 15)):
        return "Pending", []   # sample too small to judge
    reasons = []
    asr, aloc, short = m.get("asr"), m.get("aloc"), m.get("short_pct")
    if asr is not None and asr < l1.get("asr_min_pct", 30):
        reasons.append(f"ASR {asr}% < {l1.get('asr_min_pct', 30)}%")
    if aloc is not None and aloc < l1.get("aloc_min_sec", 30):
        reasons.append(f"ALOC {aloc}s < {l1.get('aloc_min_sec', 30)}s")
    if short is not None and short > l1.get("short_call_max_pct", 15):
        reasons.append(f"short {short}% > {l1.get('short_call_max_pct', 15)}%")
    return ("Watch", reasons) if reasons else ("Clean", [])


# ---- fusion ----
def fuse(statuses: list[str]) -> str:
    """Worst-of across layers; 'Pending' is a non-vote. Carrier-grade L2 can flag even
    when L1 is clean; L1 breach forces at least Watch. Neither can veto the other down."""
    real = [s for s in statuses if s in ("Clean", "Watch", "Flagged")]
    if not real:
        return "Pending"
    return max(real, key=lambda s: SEVERITY[s])


def _rank(status: str) -> int:
    return {"Clean": 0, "Pending": 0, "Watch": 1, "Flagged": 2}.get(status, 0)


def _is_auth_error(err: str) -> bool:
    """True only for a genuine auth/access failure worth alerting: a wrong Telnyx key
    (401/403) or an expired SmrtPhone session. A 500 / timeout / Telnyx 'unexpected
    error' (code 10004) is transient, NOT an auth problem."""
    s = str(err).lower()
    return ("401" in s or "403" in s or "unauthorized" in s or "forbidden" in s
            or "authentication" in s or "invalid api key" in s
            or "session expired" in s or "no smrtphone session" in s)


# ---- lifecycle ----
def warmup_total_days(th: dict) -> int:
    return sum(int(b.get("days", 0)) for b in th.get("lifecycle", {}).get("warmup_ramp", []))


def _initial_state(added: str | None, today_d: datetime.date, warm_total: int) -> str:
    if added:
        try:
            age = (today_d - datetime.date.fromisoformat(added)).days
            return "WARMING" if age < warm_total else "ACTIVE"
        except ValueError:
            pass
    return "ACTIVE"


def ensure_states(state: dict, meta: dict, th: dict, today_d: datetime.date) -> None:
    """Seed a lifecycle state for any record that lacks one (e.g. pre-Telnyx records
    migrated from the old IPQS build), so the dashboard is meaningful before the first
    live commit. Derives WARMING vs ACTIVE from the number's added date. Mutates state."""
    warm_total = warmup_total_days(th)
    for n10, rec in state.items():
        if not rec.get("state"):
            added = rec.get("added") or meta.get(n10, {}).get("added")
            rec["state"] = _initial_state(added, today_d, warm_total)
        if not rec.get("added"):
            rec["added"] = meta.get(n10, {}).get("added", "")


def ramp_cap(added: str | None, th: dict, today_d: datetime.date) -> int:
    """Dials/day cap for a WARMING DID at its current ramp day."""
    lc = th.get("lifecycle", {})
    ramp = lc.get("warmup_ramp", [])
    age = 0
    if added:
        try:
            age = (today_d - datetime.date.fromisoformat(added)).days
        except ValueError:
            age = 0
    acc = 0
    for bucket in ramp:
        acc += int(bucket.get("days", 0))
        if age < acc:
            return int(bucket.get("max_dials_per_day", 0))
    return int(lc.get("active_daily_cap", 75))


def advance_lifecycle(rec: dict, fused: str, th: dict, today_str: str,
                      today_d: datetime.date) -> dict:
    """Advance the DID state machine given its fused status this run. Mutates rec."""
    lc = th.get("lifecycle", {})
    warm_total = warmup_total_days(th)
    prev_status = rec.get("status", "Pending")
    prev_state = rec.get("state") or _initial_state(rec.get("added"), today_d, warm_total)

    consec = int(rec.get("consecutive_flags", 0))
    lifetime = int(rec.get("flag_count", 0))
    new_state = prev_state
    new_flag = False

    if prev_state == "RETIRED":
        new_state = "RETIRED"
    elif fused == "Flagged":
        new_flag = (prev_status != "Flagged")   # a fresh entry into Flagged
        # A carrier label persists for weeks and is re-observed on every daily run while
        # the DID sits benched in RESTING. Counting each re-observation as a fresh strike
        # would retire the DID in `retire_after` days and keep pushing rested_until
        # forward so the 30-day timer never elapses. Count a strike only on a genuinely
        # NEW flag, or when a full rest cycle has elapsed and the flag still survives
        # ("flag survives rest + refile"); otherwise hold RESTING with the original timer.
        ru = rec.get("rested_until")
        rest_elapsed = False
        if prev_state == "RESTING" and ru:
            try:
                rest_elapsed = today_d >= datetime.date.fromisoformat(ru)
            except ValueError:
                rest_elapsed = False
        if new_flag or rest_elapsed:
            consec += 1
            lifetime += 1
            if consec >= int(lc.get("retire_after_consecutive_flags", 3)):
                new_state = "RETIRED"
                rec["retire_candidate"] = True
            else:
                new_state = "RESTING"
                rec["rested_until"] = (
                    today_d + datetime.timedelta(days=int(lc.get("rest_days", 30)))
                ).isoformat()
        else:
            new_state = "RESTING"   # still resting on the same unresolved flag
    elif fused == "Watch":
        consec = 0
        new_state = "WATCH" if prev_state == "ACTIVE" else prev_state
    elif fused == "Clean":
        consec = 0
        rec["retire_candidate"] = False
        if prev_state == "WARMING":
            age_ok = False
            if rec.get("added"):
                try:
                    age_ok = (today_d - datetime.date.fromisoformat(rec["added"])).days >= warm_total
                except ValueError:
                    age_ok = True
            new_state = "ACTIVE" if age_ok else "WARMING"
        elif prev_state == "WATCH":
            new_state = "ACTIVE"
        elif prev_state == "RESTING":
            ru = rec.get("rested_until")
            elapsed = False
            if ru:
                try:
                    elapsed = today_d >= datetime.date.fromisoformat(ru)
                except ValueError:
                    elapsed = False
            if elapsed:
                new_state = "ACTIVE"
                rec["rested_until"] = None
            else:
                new_state = "RESTING"
        else:
            new_state = "ACTIVE"
    else:  # Pending: no live vote this run - keep state, but honor an elapsed rest
        new_state = prev_state
        # A RESTING DID is benched (not dialed), so in the L1-only tier it never produces
        # a fused Clean to un-rest itself. Once its rest window elapses, return it to
        # ACTIVE so it gets re-dialed and re-scored (the state machine's intended recovery).
        if prev_state == "RESTING":
            ru = rec.get("rested_until")
            if ru:
                try:
                    if today_d >= datetime.date.fromisoformat(ru):
                        new_state = "ACTIVE"
                        rec["rested_until"] = None
                except ValueError:
                    pass

    rec["state"] = new_state
    rec["status"] = fused if fused != "Pending" else prev_status
    rec["last_checked"] = today_str
    rec["consecutive_flags"] = consec
    rec["flag_count"] = lifetime

    return {
        "prev_state": prev_state, "new_state": new_state,
        "prev_status": prev_status, "new_status": rec["status"],
        "degraded": _rank(rec["status"]) > _rank(prev_status),
        "recovered": prev_status in ("Watch", "Flagged") and rec["status"] == "Clean",
        "new_flag": new_flag,
        "retired": new_state == "RETIRED" and prev_state != "RETIRED",
    }


# ---- registration re-check ----
def recheck_status(reg: dict, th: dict) -> dict:
    submitted = (reg or {}).get("submitted") or ""
    recheck_by = (reg or {}).get("recheck_by") or (reg or {}).get("renew_by") or ""
    interval = int(th.get("registration_recheck_days", 90))
    if submitted and not recheck_by:
        try:
            d = datetime.date.fromisoformat(submitted)
            recheck_by = (d + datetime.timedelta(days=interval)).isoformat()
        except ValueError:
            recheck_by = ""
    due, days_left = False, None
    if recheck_by:
        try:
            days_left = (datetime.date.fromisoformat(recheck_by) - datetime.date.today()).days
            due = days_left <= int(th.get("recheck_alert_days", 14))
        except ValueError:
            pass
    return {"recheck_by": recheck_by, "days_left": days_left, "due": due}


# ---- remediation ledger ----
def load_ledger() -> list:
    return load_json(LEDGER_FILE, []) or []


def in_cooldown(ledger: list, number_e164: str, cooldown_days: int,
                today_d: datetime.date) -> bool:
    for row in ledger:
        if row.get("number") != number_e164:
            continue
        opened = row.get("opened")
        if not opened:
            continue
        try:
            if (today_d - datetime.date.fromisoformat(opened[:10])).days < cooldown_days:
                return True
        except ValueError:
            continue
    return False


def run_remediation(ledger: list, flagged_dids: list[str], key: str | None,
                    ent: str | None, th: dict, today_str: str,
                    today_d: datetime.date, do_submit: bool,
                    contact_email: str | None) -> dict:
    """Poll open remediation rows and submit new ones for freshly-flagged DIDs.
    Returns a summary for the digest. Mutates ledger in place."""
    rem = th.get("remediation", {})
    cooldown = int(rem.get("cooldown_days", 14))
    call_purpose = rem.get("call_purpose", "Outbound business calls.")
    summary = {"submitted": [], "skipped_cooldown": [], "polled": [],
               "would_submit": [], "errors": []}

    # 1) poll open rows
    if key and ent:
        for row in ledger:
            if row.get("closed") or not row.get("request_id"):
                continue
            res = telrep.poll_remediation(key, ent, row["request_id"])
            if not res.get("ok"):
                summary["errors"].append(f"poll {row['number']}: {res.get('error')}")
                continue
            row["status"] = res.get("status")
            if res.get("results"):
                row["results"] = res["results"]
            if res.get("terminal"):
                row["closed"] = True
                row["closed_on"] = today_str
            summary["polled"].append((row["number"], row.get("status")))

    # 2) submit flagged DIDs. Candidates are every CURRENTLY-Flagged DID (not just this
    # run's transition), so a transient submit failure self-heals next run. Guarded by
    # the open-row check (avoid a 409 on an in-flight request) and the 14-day cooldown
    # (a fresh row keeps a DID out until the dispute window reopens).
    open_numbers = {row.get("number") for row in ledger
                    if not row.get("closed") and row.get("request_id")}
    for did10 in flagged_dids:
        num = e164(did10)
        if num in open_numbers:
            continue
        if in_cooldown(ledger, num, cooldown, today_d):
            summary["skipped_cooldown"].append(num)
            continue
        if not (do_submit and key and ent and rem.get("auto_submit", True)):
            summary["would_submit"].append(num)
            continue
        res = telrep.submit_remediation(key, ent, [num], call_purpose=call_purpose,
                                        contact_email=contact_email)
        if res.get("ok"):
            ledger.append({
                "number": num, "opened": today_str,
                "request_id": res.get("request_id"), "status": res.get("status"),
                "submitted": res.get("submitted"), "ineligible": res.get("ineligible"),
                "closed": False,
            })
            summary["submitted"].append(num)
        elif res.get("conflict"):
            summary["errors"].append(f"submit {num}: 409 in-flight (already open)")
        else:
            summary["errors"].append(f"submit {num}: {res.get('error')}")
    return summary


# ---- pool recommender ----
def build_pool(state: dict, meta: dict, th: dict, today_str: str,
               today_d: datetime.date, roster) -> dict:
    lc = th.get("lifecycle", {})
    roster = set(roster)
    active_cap = int(lc.get("active_daily_cap", 75))
    watch_cap = int(lc.get("watch_daily_cap", max(1, active_cap // 2)))
    active, bench = [], []
    for n10, rec in state.items():
        if n10 not in roster:
            continue   # dropped from cfg / moved to excluded -> not dialable
        st = rec.get("state", "NEW")
        m = meta.get(n10, {})
        entry = {"number": n10, "pretty": pretty(n10),
                 "label": rec.get("label") or m.get("label", ""),
                 "caller": m.get("caller", "")}
        if st == "ACTIVE":
            entry["cap"] = active_cap
            active.append(entry)
        elif st == "WARMING":
            entry["cap"] = ramp_cap(m.get("added") or rec.get("added"), th, today_d)
            entry["state"] = "WARMING"
            active.append(entry)   # warming DIDs are dialable, just capped low
        elif st == "WATCH":
            # keep WATCH dialable at a reduced cap so L1 keeps producing CDR data and
            # the DID can clear itself back to ACTIVE (else it strands off-pool with no
            # traffic to re-score it, especially in the L1-only tier).
            entry["cap"] = watch_cap
            entry["state"] = "WATCH"
            entry["reason"] = ", ".join(rec.get("reasons", []) or []) or "WATCH"
            active.append(entry)
        else:
            entry["state"] = st
            entry["reason"] = ", ".join(rec.get("reasons", []) or []) or st
            entry["rested_until"] = rec.get("rested_until")
            bench.append(entry)
    return {"generated": today_str,
            "active_count": len(active), "bench_count": len(bench),
            "active": active, "bench": bench}


# ---- batch selection (roster) ----
def build_pool_list(cfg: dict) -> tuple[list[str], dict]:
    pool, meta = [], {}
    excluded = {digits(x) for x in cfg.get("excluded", []) or []}
    for entry in cfg.get("numbers", []):
        n10 = digits(entry.get("number", ""))
        if len(n10) == 10 and n10 not in excluded:
            pool.append(n10)
            meta[n10] = {"label": entry.get("label", ""),
                         "caller": entry.get("caller", ""),
                         "added": entry.get("added", "")}
    return sorted(set(pool)), meta


# ---- main ----
def main() -> int:
    ap = argparse.ArgumentParser(description="Caller ID Reputation Monitor (Telnyx-only)")
    ap.add_argument("--commit", action="store_true",
                    help="call Telnyx and update state (default: dry, rebuild dashboard only)")
    ap.add_argument("--day", help="UTC day (YYYY-MM-DD) of CDRs to pull (default: yesterday UTC)")
    ap.add_argument("--dashboard-only", action="store_true",
                    help="just regenerate the HTML from existing state")
    ap.add_argument("--no-remediate", action="store_true",
                    help="do everything except SUBMIT remediation requests (still polls open ones)")
    ap.add_argument("--no-notify", action="store_true", help="skip Slack")
    ap.add_argument("--always-notify", action="store_true",
                    help="post a Slack heartbeat digest even when nothing changed")
    args = ap.parse_args()

    open_log()
    env = load_env_file()
    notify_enabled = not args.no_notify
    today_str = today()
    today_d = datetime.date.today()

    if not NUMBERS_FILE.exists():
        log(f"FATAL: {NUMBERS_FILE} not found. Copy numbers.example.json -> numbers.json.")
        return 2
    cfg = load_json(NUMBERS_FILE, {}) or {}
    th = load_json(THRESHOLDS_FILE, {}) or {}
    state = load_json(STATE_FILE, {}) or {}
    ledger = load_ledger()

    pool, meta = build_pool_list(cfg)
    log(f"Pool: {len(pool)} numbers; state has {len(state)} tracked.")

    # seed skeleton records for any roster number not yet in state, then make sure
    # every record has a lifecycle state (migrates old IPQS-only records too).
    for n10 in pool:
        if n10 not in state:
            state[n10] = {"number": n10, "label": meta[n10].get("label", ""),
                          "added": meta[n10].get("added", ""), "status": "Pending"}
    ensure_states(state, meta, th, today_d)

    reg = cfg.get("registration", {})
    ren = recheck_status(reg, th)

    tx = cfg.get("telnyx", {}) or {}
    key = env.get("TELNYX_API_KEY") or os.environ.get("TELNYX_API_KEY")
    ent = (env.get("TELNYX_ENTERPRISE_ID") or os.environ.get("TELNYX_ENTERPRISE_ID")
           or tx.get("enterprise_id") or "")
    contact_email = tx.get("contact_email") or env.get("TELNYX_CONTACT_EMAIL")

    # L1 source: SmrtPhone's own call log (default, no telecom setup) or Telnyx CDR
    # (optional, requires the numbers ported into a Telnyx account + a key).
    l1_source = (th.get("l1_source") or "smrtphone").lower()
    win_days = int(th.get("smrtphone_cdr_window_days", 7))
    if l1_source == "telnyx":
        l1_ready, l1_missing = bool(key), "TELNYX_API_KEY not set"
    else:
        l1_source = "smrtphone"
        l1_ready = smrtphone_cdr.session_available()
        l1_missing = "no SmrtPhone session (run smrtphone_login.py)"

    # ---- dashboard-only / dry short circuits ----
    if args.dashboard_only or not args.commit:
        pool_plan = build_pool(state, meta, th, today_str, today_d, pool)
        _write_pool(pool_plan)
        _write_dashboard(cfg, state, th, ren, meta, pool_plan, ledger)
        if args.dashboard_only:
            log("Dashboard rebuilt (dashboard-only).")
        else:
            log("DRY RUN (no API calls). Layers that WOULD run this commit:")
            log(f"  L1 [{l1_source}]:  {'YES' if l1_ready else 'idle - ' + l1_missing}")
            log(f"  L2 Telnyx reputation:  {'YES' if (key and ent) else 'off (optional; not configured)'}")
            log("Re-run with --commit to scan.")
        return 0

    # ---- LIVE ----
    if not l1_ready:
        msg = (f"Caller ID Monitor: L1 source '{l1_source}' not ready ({l1_missing}). "
               "Rebuilding dashboard from existing state.")
        log(msg)
        pool_plan = build_pool(state, meta, th, today_str, today_d, pool)
        _write_pool(pool_plan)
        _write_dashboard(cfg, state, th, ren, meta, pool_plan, ledger)
        if args.always_notify:
            notify(msg, env, notify_enabled)
        return 0

    # ---- L1: own-traffic call outcomes ----
    if l1_source == "telnyx":
        day = None
        if args.day:
            try:
                day = datetime.date.fromisoformat(args.day)
            except ValueError:
                log(f"Bad --day {args.day!r}; using yesterday UTC.")
        cdr = telnyx_cdr.pull_day_metrics(key, day=day, pool=pool)
    else:
        cdr = smrtphone_cdr.pull_metrics(days=win_days, pool=pool)
    log(f"L1 [{l1_source}] ({cdr['day']}): {cdr['rows']} calls across {cdr['pages']} pages; "
        f"metrics for {len(cdr['metrics'])} DIDs. errors={len(cdr['errors'])}")
    for e in cdr["errors"][:5]:
        log(f"  L1 error: {e}")
    cdr_metrics = cdr["metrics"]

    # ---- L2: Telnyx reputation (OPTIONAL - only when a Telnyx key + enterprise are set) ----
    rep_by_num, rep_error, l2_on = {}, None, bool(ent and key)
    if l2_on:
        swept = telrep.read_all_cached(key, ent)
        if swept.get("ok"):
            rep_by_num = swept["by_number"]
            log(f"L2 reputation: {swept['count']} cached; {len(swept['unscored'])} not yet scored.")
            # fresh recheck ONLY on degraded (cheaper layer already worried), capped
            l2 = th.get("l2_reputation", {})
            cap = int(l2.get("fresh_recheck_max_per_run", 5))
            degraded = []
            for n10 in pool:
                num = e164(n10)
                rep = rep_by_num.get(num)
                l1s, _ = classify_cdr(cdr_metrics.get(n10), th.get("l1_cdr", {}))
                l2s = telrep.classify_reputation(rep) if rep else "Pending"
                rec_now = state.get(n10, {})
                prior_state = rec_now.get("state")
                # WATCH recovers to ACTIVE the instant it reads Clean -> keep it fresh.
                # A RESTING DID is benched and cannot recover until its window elapses, so
                # only spend a billable fresh check at/after that decision point, not on
                # every resting day (else a 30-day rest = ~30 billable checks that change
                # nothing).
                prior_due = prior_state == "WATCH"
                if prior_state == "RESTING":
                    ru = rec_now.get("rested_until")
                    try:
                        prior_due = (not ru) or today_d >= datetime.date.fromisoformat(ru)
                    except ValueError:
                        prior_due = True
                if (l2s in ("Watch", "Flagged") or l1s in ("Watch", "Flagged")
                        or prior_due) and num in rep_by_num \
                        and num not in swept["unscored"]:
                    degraded.append(num)
            degraded = degraded[:cap]
            if degraded:
                log(f"L2 fresh recheck (billable) on {len(degraded)} degraded DID(s).")
                rr = telrep.refresh_degraded(key, ent, degraded)
                if not rr.get("ok"):
                    rep_error = f"fresh recheck POST failed: {rr.get('error')}"
                    log(f"L2 fresh recheck FAILED: {rep_error}")
                elif rr.get("total_failed"):
                    rep_error = (f"fresh recheck partial: {rr.get('total_successful')}/"
                                 f"{rr.get('total_requested')} landed, "
                                 f"{rr.get('total_failed')} failed")
                    log(f"L2 fresh recheck partial: {rep_error}")
                else:
                    log(f"L2 fresh recheck OK: landed on {len(degraded)} DID(s).")
                reread = telrep.read_all_cached(key, ent)
                if reread.get("ok"):
                    rep_by_num = reread["by_number"]
        else:
            rep_error = swept.get("error")
            log(f"L2 reputation sweep FAILED: {rep_error}")
    else:
        log("L2 reputation: idle (no enterprise_id). Run telnyx_setup.py to enable.")

    # Hard-abort ONLY on a genuine auth/access failure (401/403) - that means the key or
    # account is wrong and every run will fail, so it is worth an alert. A transient 500
    # (Telnyx code 10004 "Unexpected error") or a timeout is NOT fatal and NOT an auth
    # problem: log it, skip the L1 update this run, and let tomorrow retry. Do not cry
    # wolf on the daily cron (especially in the interim before the DIDs are in-account).
    cdr_auth_fail = any(_is_auth_error(e) for e in cdr["errors"])
    cdr_transient = bool(cdr["errors"]) and not cdr_auth_fail
    if cdr_auth_fail:
        fix = ("re-run smrtphone_login.py to refresh the session"
               if l1_source == "smrtphone" else "check TELNYX_API_KEY / account access")
        msg = (f"Caller ID Monitor ABORTED: L1 [{l1_source}] auth/session failure "
               f"({cdr['errors'][0]}). Fix: {fix}.")
        log(msg)
        notify(msg, env, notify_enabled)
        return 1
    if cdr_transient:
        log(f"L1 CDR transient error (non-fatal, will retry next run): {cdr['errors'][0][:160]}")

    # ---- fuse + advance lifecycle per DID ----
    transitions, flagged_dids = [], []
    l1cfg = th.get("l1_cdr", {})
    trend_n = int(th.get("lifecycle", {}).get("trend_escalation_consecutive", 0) or 0)

    for n10 in pool:
        rec = state.get(n10, {})
        rec["number"] = n10
        rec["label"] = meta.get(n10, {}).get("label", rec.get("label", ""))
        rec["added"] = rec.get("added") or meta.get(n10, {}).get("added", "")

        m = cdr_metrics.get(n10)
        l1_status, l1_reasons = classify_cdr(m, l1cfg)
        rep = rep_by_num.get(e164(n10))
        l2_status = telrep.classify_reputation(rep) if rep else "Pending"
        l2_reasons = telrep.reputation_reasons(rep) if rep else []

        fused = fuse([l1_status, l2_status])
        reasons = list(l1_reasons) + list(l2_reasons)

        # trend escalation: N non-clean in a row -> Flagged (slow-degrade catch)
        if trend_n >= 2 and fused == "Watch":
            prior = [h.get("status") for h in rec.get("history", [])[-(trend_n - 1):]]
            if len(prior) >= (trend_n - 1) and all(s in ("Watch", "Flagged") for s in prior):
                fused = "Flagged"
                reasons.append(f"trend: {trend_n} non-clean checks in a row")

        # store per-source snapshots
        rec["sources"] = {
            "cdr": ({**m, "status": l1_status} if m else {"status": l1_status}),
            "telnyx_rep": ({**rep, "status": l2_status} if rep else {"status": l2_status}),
        }
        rec["reasons"] = reasons

        rec.setdefault("history", []).append({
            "date": today_str, "status": fused,
            "l1": l1_status, "l2": l2_status,
            "asr": (m or {}).get("asr"), "aloc": (m or {}).get("aloc"),
            "short_pct": (m or {}).get("short_pct"), "dials": (m or {}).get("dials"),
            "spam_risk": (rep or {}).get("spam_risk"),
            "spam_category": (rep or {}).get("spam_category"),
        })
        rec["history"] = rec["history"][-HISTORY_CAP:]

        tr = advance_lifecycle(rec, fused, th, today_str, today_d)
        state[n10] = rec

        flagline = f" [{', '.join(reasons)}]" if reasons else ""
        log(f"  {pretty(n10)}: {tr['prev_state']}/{tr['prev_status']} -> "
            f"{tr['new_state']}/{tr['new_status']}  L1={l1_status} L2={l2_status}{flagline}")

        # remediation candidates = every currently-Flagged DID (self-heals a transient
        # submit failure next run); run_remediation dedupes via cooldown + open-row guard.
        if rec.get("status") == "Flagged":
            flagged_dids.append(n10)
        if tr["degraded"] or tr["new_flag"] or tr["retired"] or tr["recovered"]:
            transitions.append((n10, rec, tr, reasons))

    # ---- remediation ----
    rem_summary = run_remediation(
        ledger, flagged_dids, key, ent, th, today_str, today_d,
        do_submit=not args.no_remediate, contact_email=contact_email)

    # ---- pool plan ----
    pool_plan = build_pool(state, meta, th, today_str, today_d, pool)

    # ---- persist ----
    save_json(STATE_FILE, state)
    save_json(LEDGER_FILE, ledger)
    _write_pool(pool_plan)
    _write_dashboard(cfg, state, th, ren, meta, pool_plan, ledger)

    # ---- verify data landed (GR #17) ----
    verify_ok, verify_msg = _verify_landed(cdr, rep_by_num, l2_on, state)
    log(f"Verify: {'OK' if verify_ok else 'WARN'} - {verify_msg}")

    # ---- alert ----
    _send_summary(env, notify_enabled, cfg, state, cdr, l2_on, rep_error,
                  transitions, rem_summary, ren, pool_plan, verify_ok, verify_msg,
                  args.always_notify)

    if not verify_ok:
        notify(f":warning: Caller ID Monitor: {verify_msg}", env, notify_enabled)

    return 0


# ---- helpers ----
def _counts(state: dict, roster: set | None = None) -> dict:
    c = {"Clean": 0, "Watch": 0, "Flagged": 0, "Pending": 0}
    for n, r in state.items():
        if roster is not None and n not in roster:
            continue   # skip records for numbers no longer on the roster
        s = r.get("status", "Pending")
        c[s] = c.get(s, 0) + 1
    return c


def _state_counts(state: dict, roster: set | None = None) -> dict:
    c = {}
    for n, r in state.items():
        if roster is not None and n not in roster:
            continue
        s = r.get("state", "NEW")
        c[s] = c.get(s, 0) + 1
    return c


def _write_pool(pool_plan: dict) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    save_json(ACTIVE_POOL_FILE, pool_plan)


def _write_dashboard(cfg, state, th, ren, meta, pool_plan, ledger) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    html = dash.render(cfg, state, th, ren, meta, pool_plan, ledger)
    DASHBOARD_FILE.write_text(html, encoding="utf-8")
    log(f"Dashboard: {DASHBOARD_FILE}")


def _verify_landed(cdr, rep_by_num, l2_on, state) -> tuple[bool, str]:
    """A 'success' that wrote nothing is the real failure mode. Assert the run produced a
    coherent state. A transient L1 error (already classified non-auth upstream; auth
    aborts before we get here) is a note, not a verify failure - it retries next run."""
    if not state:
        return False, "state is empty after run"
    if l2_on and not rep_by_num:
        return False, "L2 enabled but reputation sweep returned nothing"
    l1 = (f"L1 transient error, {cdr.get('rows',0)} rows" if cdr.get("errors")
          else f"L1 {cdr.get('rows', 0)} rows / {len(cdr.get('metrics', {}))} DIDs")
    return True, f"{l1}; L2 {'on' if l2_on else 'off'} ({len(rep_by_num)} scored)"


def _send_summary(env, enabled, cfg, state, cdr, l2_on, rep_error, transitions,
                  rem, ren, pool_plan, verify_ok, verify_msg, always) -> None:
    roster = {a["number"] for a in pool_plan.get("active", [])} | \
             {b["number"] for b in pool_plan.get("bench", [])}
    c = _counts(state, roster)
    sc = _state_counts(state, roster)
    biz = cfg.get("business_name", "Caller ID")
    lines = [f"*{biz} - Caller ID Reputation* (L1 CDR {cdr.get('day','')}, "
             f"L2 {'on' if l2_on else 'off'})"]
    lines.append(
        f"Status: Clean {c.get('Clean',0)} | Watch {c.get('Watch',0)} | "
        f"Flagged {c.get('Flagged',0)} | Pending {c.get('Pending',0)}")
    lines.append(
        f"Pool: ACTIVE {pool_plan.get('active_count',0)} | "
        f"WARMING {sc.get('WARMING',0)} | WATCH {sc.get('WATCH',0)} | "
        f"RESTING {sc.get('RESTING',0)} | RETIRED {sc.get('RETIRED',0)}")

    if transitions:
        lines.append("")
        lines.append(":rotating_light: *Changed this run:*")
        for n, rec, tr, why in transitions:
            extra = f" ({', '.join(why)})" if why else ""
            rest = f"  rest until {rec.get('rested_until')}" if rec.get("rested_until") else ""
            retire = "  *RETIRED*" if tr.get("retired") else ""
            lines.append(f"  - {pretty(n)} {tr['prev_state']}/{tr['prev_status']} -> "
                         f"{tr['new_state']}/{tr['new_status']}{extra}{rest}{retire}")

    subs = rem.get("submitted") or []
    wsubs = rem.get("would_submit") or []
    cds = rem.get("skipped_cooldown") or []
    if subs or wsubs or cds or rem.get("errors"):
        lines.append("")
        lines.append(":wrench: *Remediation:*")
        if subs:
            lines.append(f"  - filed: {', '.join(subs)}")
        if wsubs:
            lines.append(f"  - would file (no-remediate / L2 off): {', '.join(wsubs)}")
        if cds:
            lines.append(f"  - in cooldown, skipped: {', '.join(cds)}")
        for e in (rem.get("errors") or [])[:5]:
            lines.append(f"  - error: {e}")

    # L1 errors here are transient (auth failures abort upstream) -> informational, do
    # not force a notify on their own.
    if cdr.get("errors"):
        lines.append("")
        lines.append(f":information_source: L1 CDR transient error (retries next run): "
                     f"{cdr['errors'][0][:160]}")
    if rep_error:
        lines.append(f":warning: L2 reputation error: {rep_error}")
    if not verify_ok:
        lines.append(f":warning: verify: {verify_msg}")

    if ren.get("due"):
        lines.append("")
        lines.append(
            f":calendar: Registration re-check due in {ren.get('days_left')} day(s) "
            f"(recheck_by {ren.get('recheck_by')}).")

    # Transient L1 CDR errors do NOT force a notify (they self-heal next run); a real
    # degrade, remediation, L2 error, recheck-due, or verify failure does.
    interesting = (transitions or subs or cds or rem.get("errors")
                   or rep_error or ren.get("due") or not verify_ok)
    if interesting:
        notify("\n".join(lines), env, enabled)
    elif always:
        lines.append("")
        lines.append(":white_check_mark: All clean, nothing to action. (heartbeat)")
        notify("\n".join(lines), env, enabled)
    else:
        log("[notify] all clean, nothing to action; staying quiet (use --always-notify).")


if __name__ == "__main__":
    sys.exit(main())

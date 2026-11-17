"""Did the texts actually reach a handset? The connectivity readout.

Built because "connectivity feels bad" was undiagnosable: the receiver logs
every smsDeliveryCallback but nothing aggregated them, so filtering looked
exactly like a quiet audience. This reads the raw event log (never the send
path) and answers three questions per pool and per number: what share of
sends got a final carrier verdict, what the carrier said when it refused,
and whether replies per send are trending down.

Two measurement caveats the output states rather than hides:
- Until the dedupe fix ships, the receiver kept only the FIRST callback per
  smsId, so a "sent" that later became "delivered" or "undelivered" lost its
  final verdict. Every rate here is a floor, not a truth.
- "sent" means handed to the carrier, not delivered. Only "delivered" and
  "undelivered" are verdicts.

    python src/sms_agent/cli.py deliverability                # verdict + pools
    python src/sms_agent/cli.py deliverability --days 30 --full
    python src/sms_agent/cli.py deliverability --db sms_agent_prod.db
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from . import config

# A number is flagged only on enough volume to mean something, and only when
# its reply rate sits far under its own pool, because reply rate varies by
# copy and cohort long before it varies by number health.
MIN_SENDS_TO_JUDGE = 30
BURNED_FACTOR = 0.5

# A spam-flag burst across this many distinct numbers inside one week is
# campaign-level filtering, not a burned DID. That distinction decides the
# fix: rotation cures a number, only registration/content changes cure a
# campaign.
SPAM_BURST_NUMBERS = 3


def _classify(reason: str) -> str:
    low = (reason or "").lower()
    if "a2p" in low or "10dlc" in low:
        return "registration"
    if "objectionable" in low or "spam" in low:
        return "spam_flag"
    if any(s in low for s in ("unknown", "no longer exist", "invalid", "not in service", "unallocated")):
        return "dead_number"
    if "landline" in low or "unable to receive" in low:
        return "landline"
    if "switched off" in low or "unavailable" in low:
        return "handset"
    return "other"


def _pool(caller_id_name: str) -> str:
    name = (caller_id_name or "").strip()
    if "dispo" in name.lower():
        return "Dispo"
    if " - " in name:
        return name.split(" - ")[0].strip() or "unknown"
    return name.split()[0] if name else "unknown"


def _week(iso_ts: str) -> str:
    d = date.fromisoformat(iso_ts[:10])
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def collect(days: int = 60, db_path: Optional[str] = None) -> dict:
    path = db_path or str(config.DB_PATH)
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")

    # One pass over outgoing: smsId -> (week, number), plus per-number tallies.
    sid_meta: dict[str, tuple[str, str]] = {}
    numbers: dict[str, dict] = {}
    weekly: dict[tuple[str, str], dict] = {}

    def num(frm: str, name: str = "") -> dict:
        n = numbers.setdefault(frm, {
            "name": "", "pool": "unknown", "sends": 0, "replies": 0,
            "delivered": 0, "undelivered": 0, "verdictless": 0,
            "fail": {}, "api_rejected": 0,
        })
        if name and not n["name"]:
            n["name"], n["pool"] = name, _pool(name)
        return n

    def wk(week: str, pool: str) -> dict:
        return weekly.setdefault((week, pool), {
            "sends": 0, "replies": 0, "delivered": 0, "undelivered": 0, "fail": {},
        })

    for r in c.execute(
        "SELECT received_at, payload FROM events"
        " WHERE event_type='smsOutgoing' AND received_at>=?", (since,)
    ):
        p = json.loads(r["payload"])
        frm, sms_id = str(p.get("from") or ""), str(p.get("smsId") or "")
        week = _week(r["received_at"])
        n = num(frm, str(p.get("callerIdName") or ""))
        n["sends"] += 1
        sid_meta[sms_id] = (week, frm)
        wk(week, n["pool"])["sends"] += 1

    # Delivery callbacks join back through the smsId. A callback whose send
    # fell outside the window is dropped rather than misattributed.
    verdict_counts = {"sent": 0, "delivered": 0, "undelivered": 0}
    spam_weeks: dict[str, set] = {}
    for r in c.execute(
        "SELECT payload FROM events WHERE event_type='smsDeliveryCallback'"
        " AND received_at>=?", (since,)
    ):
        p = json.loads(r["payload"])
        status = str(p.get("status") or "").lower()
        meta = sid_meta.get(str(p.get("smsId") or ""))
        if status in verdict_counts:
            verdict_counts[status] += 1
        if not meta:
            continue
        week, frm = meta
        n = num(frm)
        if status == "delivered":
            n["delivered"] += 1
            wk(week, n["pool"])["delivered"] += 1
        elif status == "undelivered":
            kind = _classify(str(p.get("failure_reason") or ""))
            n["undelivered"] += 1
            n["fail"][kind] = n["fail"].get(kind, 0) + 1
            w = wk(week, n["pool"])
            w["undelivered"] += 1
            w["fail"][kind] = w["fail"].get(kind, 0) + 1
            if kind == "spam_flag":
                spam_weeks.setdefault(week, set()).add(frm)

    for r in c.execute(
        "SELECT received_at, payload FROM events"
        " WHERE event_type='smsIncoming' AND received_at>=?", (since,)
    ):
        p = json.loads(r["payload"])
        to = str(p.get("to") or "")
        if to in numbers:
            numbers[to]["replies"] += 1
            wk(_week(r["received_at"]), numbers[to]["pool"])["replies"] += 1

    # Sends smrtPhone's own API refused. These never reached a carrier, so
    # they exist only in the outbox, not in the event log.
    for r in c.execute(
        "SELECT from_number, error, COUNT(*) AS n FROM outbox"
        " WHERE status='failed' AND created_at>=? GROUP BY 1, 2", (since,)
    ):
        if "10dlc" in str(r["error"] or "").lower():
            num(str(r["from_number"] or ""))["api_rejected"] += int(r["n"])

    for n in numbers.values():
        n["verdictless"] = max(0, n["sends"] - n["delivered"] - n["undelivered"])

    pools: dict[str, dict] = {}
    for n in numbers.values():
        p = pools.setdefault(n["pool"], {
            "sends": 0, "replies": 0, "delivered": 0, "undelivered": 0,
            "api_rejected": 0, "numbers": 0, "fail": {},
        })
        p["numbers"] += 1
        for k in ("sends", "replies", "delivered", "undelivered", "api_rejected"):
            p[k] += n[k]
        for k, v in n["fail"].items():
            p["fail"][k] = p["fail"].get(k, 0) + v

    findings = _findings(numbers, pools, weekly, verdict_counts, spam_weeks)
    return {
        "window_days": days,
        "db": path,
        "numbers": numbers,
        "pools": pools,
        "weekly": {f"{w}|{p}": v for (w, p), v in sorted(weekly.items())},
        "verdicts": verdict_counts,
        "findings": findings,
    }


def _findings(numbers: dict, pools: dict, weekly: dict,
              verdicts: dict, spam_weeks: dict) -> list[str]:
    out = []
    total_cb = sum(verdicts.values())
    final = verdicts["delivered"] + verdicts["undelivered"]
    if total_cb and final < total_cb * 0.5:
        out.append(
            f"BLIND: only {final} of {total_cb} callbacks carry a final verdict"
            " (delivered/undelivered). The receiver deduped later callbacks per"
            " smsId until the status-aware key shipped, so every rate below is"
            " a floor. Redeploy the receiver to start seeing real verdicts."
        )
    reg = sum(p["fail"].get("registration", 0) for p in pools.values())
    api_rej = sum(p["api_rejected"] for p in pools.values())
    if reg or api_rej:
        out.append(
            f"REGISTRATION: {reg} carrier-refused + {api_rej} smrtPhone-refused"
            " sends cite A2P 10DLC campaign association. 100% fixable: register"
            " the numbers, nothing else cures this."
        )
    for week, nums in sorted(spam_weeks.items()):
        if len(nums) >= SPAM_BURST_NUMBERS:
            out.append(
                f"SPAM BURST {week}: carrier content-flagged sends on"
                f" {len(nums)} distinct numbers. Spread that wide is"
                " campaign/content-level filtering, not a burned DID; rotation"
                " will not fix it."
            )
    for pool, p in sorted(pools.items()):
        rates = [
            (frm, n["replies"] / n["sends"])
            for frm, n in numbers.items()
            if n["pool"] == pool and n["sends"] >= MIN_SENDS_TO_JUDGE
        ]
        if len(rates) < 3:
            continue
        med = sorted(r for _, r in rates)[len(rates) // 2]
        burned = [f for f, r in rates if med and r < med * BURNED_FACTOR]
        if burned:
            out.append(
                f"COLD NUMBERS ({pool}): {', '.join(sorted(burned))} reply at"
                f" under half the pool median ({med:.0%}). Candidates for rest"
                " or replacement; verify against the smrtPhone log first."
            )
    return out


def render(data: dict, full: bool = False) -> str:
    v = data["verdicts"]
    total_sends = sum(p["sends"] for p in data["pools"].values())
    lines = [
        f"*SMS deliverability, last {data['window_days']} day(s)*"
        f" - {total_sends} sends, verdicts: {v['delivered']} delivered,"
        f" {v['undelivered']} undelivered, {v['sent']} carrier-accepted with no"
        " final verdict recorded",
    ]
    for pool, p in sorted(data["pools"].items()):
        if not p["sends"]:
            continue
        fails = ", ".join(f"{k} {n}" for k, n in sorted(p["fail"].items(), key=lambda x: -x[1]))
        lines.append(
            f"{pool}: {p['sends']} sends on {p['numbers']} numbers,"
            f" {p['replies']} replies ({p['replies'] / p['sends']:.0%}),"
            f" {p['undelivered']} refused" + (f" ({fails})" if fails else "")
            + (f", {p['api_rejected']} API-rejected" if p["api_rejected"] else "")
        )
    lines.append("")
    if data["findings"]:
        for f in data["findings"]:
            lines.append(f"! {f}")
    else:
        lines.append("No findings. Verdict visibility, registration, content"
                     " flags and per-number reply rates all read clean.")

    if full:
        lines.append("")
        lines.append("week        pool      sends  replies  rate   undeliv")
        for key, w in data["weekly"].items():
            week, pool = key.split("|")
            rate = f"{w['replies'] / w['sends']:.0%}" if w["sends"] else "-"
            fails = ",".join(f"{k}:{n}" for k, n in sorted(w["fail"].items()))
            lines.append(f"{week}  {pool:9s} {w['sends']:5d}  {w['replies']:6d}  {rate:>4s}   "
                         f"{w['undelivered']}{' (' + fails + ')' if fails else ''}")
        lines.append("")
        lines.append("number         pool      sends  replies  rate   undeliv  no-verdict")
        for frm, n in sorted(data["numbers"].items(), key=lambda x: -x[1]["sends"]):
            rate = f"{n['replies'] / n['sends']:.0%}" if n["sends"] else "-"
            lines.append(f"{frm}  {n['pool']:9s} {n['sends']:5d}  {n['replies']:6d}  {rate:>4s}   "
                         f"{n['undelivered']:7d}  {n['verdictless']}")
        lines.append("")
        lines.append("Reply rate is a proxy: replies lag sends, so the current"
                     " week always reads low. 'sent' only means the carrier"
                     " accepted the hand-off.")
    return "\n".join(lines)


def run(days: int = 60, full: bool = False, db_path: Optional[str] = None) -> str:
    return render(collect(days=days, db_path=db_path), full=full)

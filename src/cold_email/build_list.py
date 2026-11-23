"""Build a cold email list from a DataSift preset: one checked email per owner, never anyone we
should not contact.

    python src/cold_email/build_list.py --preset "Agency - 01 Queue" --target 600            # read only
    python src/cold_email/build_list.py --preset "Agency - 01 Queue" --target 600 --verify   # + ZeroBounce

Per record, in this order (the cheap checks first, so a skipped owner costs nothing):
  1. the search row: status and do-not-mail flags (gate.check_row), entity owners out (people only),
     an owner already on this list or loaded before (ledger.json) out
  2. the full record: the same gate every other channel uses, on its own email channel (do not market,
     litigator, opted out, DNC list, sold, deceased or obituary, a lead, a finished status, listed,
     not single family, do not mail, held for research by its status or by a research tag the
     playbook names, such as Tax Sale Redemption). Returned postal mail does not stop an email; a
     Mail Only tag does not either (it means no calls and no texts).
  3. one address: the first whose name part carries the owner's first or last name. Skip-traced
     emails are a loose list (median 5 per owner, many belong to other people), so an owner with no
     name match is left out rather than guessed.
Then with --verify: ZeroBounce with Verify+ on every address, keep "valid" only; and the owner's
other properties are read, and any that is a lead, opted out, finished, or in an in-house lane
(first to market, Top 1,000, obituary, heirs, hot auction) takes the owner off the list.

Writes lists/<slug>/ in the state folder (coldstate.py: output/cold_email on a workstation, gitignored
because it holds owner emails; the brain's volume on the box). Resumable. Read only against DataSift;
the only spend is ZeroBounce credits under --verify, which runs only on the install home.json names.

Past 10,000 rows: the records search refuses an offset past its 10,000-row window, and the Agency queue
holds about 20,000 records, so a preset that large is read in slices of its own `created` dates (each
under the window, newest first; the search honors `created: [from, to]` in dates or datetimes). The
slices are worked out once per list and kept in its state, so a resumed build reads on where it stopped.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent
ROOT = SRC.parent
for p in (str(SRC), str(HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

from agents.autopilot import gate, lanes as lanes_mod  # noqa: E402
from agents.autopilot.crm import CRM, search_shape  # noqa: E402
import coldstate  # noqa: E402
import deep_prospect_batch  # noqa: E402

_spec = importlib.util.spec_from_file_location("touches", SRC / "sms_agent" / "knowledge" / "touches.py")
touches = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(touches)

LISTS = coldstate.state_dir() / "lists"
LEDGER = coldstate.state_dir() / "ledger.json"
# Records ruled out (gate, no named email, a dead address, another property) are not read again for
# this many days, so the nightly top-up spends its DataSift reads on records it has not judged yet.
REJECTED = coldstate.state_dir() / "rejected.json"
REJECT_DAYS = 30
CHANNEL = "email"                        # the gate's own channel (2026-11-20; it borrowed "mail" before)
IN_HOUSE = {"lead", "heirs", "hot_auction", "ftm", "obituary", "top1000"}
UPPER_KEEP = {"N", "S", "E", "W", "NE", "NW", "SE", "SW", "US", "TN", "II", "III", "IV"}
WINDOW = 10000                           # the records search refuses offset + limit past this
PAGE = 250
CREATED_FLOOR = datetime(2010, 1, 1)     # older than any record in an account
MIN_SLICE = timedelta(minutes=1)         # a bulk pull can land 15,000 records in a day: split down to the minute
# Where "a local buyer in <city>" is true. Set by --states (default TN, Knox and Blount's state).
STATES: set[str] = {"TN"}


def load_json(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(p: Path, data) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    tmp.replace(p)


def tidy_words(s: str) -> str:
    """'123 MAIN ST NW' -> '123 Main St NW', '12 Oak St # A204' -> '12 Oak St #A204', 'Elm Dr Sw' ->
    'Elm Dr SW'. Mixed case is otherwise left alone."""
    s = " ".join((s or "").split())
    s = re.sub(r"#\s+", "#", s)
    if not s:
        return s
    out = []
    for w in s.split():
        if w.upper() in UPPER_KEEP and len(w) <= 3:
            out.append(w.upper())
        elif not s.isupper():
            out.append(w)
        elif re.match(r"^\d", w):
            out.append(w.lower())
        else:
            out.append(w.capitalize())
    return " ".join(out)


def emails_of(rec: dict) -> list[str]:
    out = []
    for x in (rec.get("owner") or {}).get("emails") or []:
        v = x.get("email") if isinstance(x, dict) else x
        if isinstance(v, str) and "@" in v:
            v = v.strip().lower()
            if v not in out:
                out.append(v)
    return out


def pick_email(owner: dict, emails: list[str]) -> str:
    toks = [t for t in re.findall(r"[a-z]+", f"{owner.get('first_name') or ''} {owner.get('last_name') or ''}".lower())
            if len(t) >= 3]
    for e in emails:
        local = e.split("@")[0]
        if any(t in local for t in toks):
            return e
    return ""


PLAYBOOK = ROOT / "team" / "autopilot.yaml"
_GATE: list = []


def gate_cfg() -> gate.GateConfig:
    """The gate's defaults plus the research holds the account's playbook names (gate.research_tags): a
    former owner who can still redeem a tax sale house (Tax Sale Redemption) is held out of every outreach
    channel until a lawyer okays it, an email included (2026-11-20). Only that one setting is taken; the
    rest of this lane runs on the defaults it was measured on. No playbook means no holds of that kind; a
    playbook that cannot be read stops the build rather than email around the hold."""
    if _GATE:
        return _GATE[0]
    cfg = gate.GateConfig()
    if PLAYBOOK.is_file():
        import yaml
        try:
            block = (yaml.safe_load(PLAYBOOK.read_text(encoding="utf-8")) or {}).get("gate") or {}
        except Exception as exc:  # noqa: BLE001
            raise SystemExit("the playbook's gate settings could not be read (%s); no list was built" % exc)
        tags = block.get("research_tags") or []
        if not isinstance(tags, list):
            raise SystemExit("gate.research_tags in the playbook must be a list; no list was built")
        cfg.research_tags = [str(t) for t in tags]
    _GATE.append(cfg)
    return cfg


def email_verdict(rec: dict, row: dict | None = None) -> gate.Verdict:
    return gate.check(rec, CHANNEL, row=row, cfg=gate_cfg())


def _pair(lo: datetime, hi: datetime) -> list[str]:
    """A created window as the search takes it: two dates when both ends fall on midnight, else two
    datetimes (a window is never written half in dates and half in datetimes)."""
    if all((t.hour, t.minute, t.second) == (0, 0, 0) for t in (lo, hi)):
        return [lo.strftime("%Y-%m-%d"), hi.strftime("%Y-%m-%d")]
    return [lo.strftime("%Y-%m-%dT%H:%M:%S"), hi.strftime("%Y-%m-%dT%H:%M:%S")]


def created_slices(crm, must: dict, total: int, now: datetime | None = None) -> list[list]:
    """The preset cut into windows of its records' created dates, each holding no more than the search's
    10,000-row window, newest first: [[from, to, count], ...]. A preset that already fits, or that filters
    on created itself, is one window [None, None, total]. A window that cannot be cut further (one minute)
    is kept and read to its first 10,000 rows."""
    if total <= WINDOW or "created" in (must or {}):
        return [[None, None, total]]
    out: list[list] = []

    def count(lo: datetime, hi: datetime) -> int:
        return crm.search(dict(must, created=_pair(lo, hi)), limit=1)[0]

    def split(lo: datetime, hi: datetime, n: int) -> None:
        if n <= WINDOW or hi - lo <= MIN_SLICE:
            if n:
                out.append(_pair(lo, hi) + [n])
            return
        mid = lo + (hi - lo) / 2
        mid = mid.replace(microsecond=0) if hi - lo <= timedelta(days=2) else datetime(mid.year, mid.month, mid.day)
        if not lo < mid < hi:
            mid = lo + (hi - lo) / 2
            mid = mid.replace(microsecond=0)
        newer = count(mid, hi)
        split(mid, hi, newer)
        split(lo, mid, count(lo, mid))

    end = (now or datetime.now()) + timedelta(days=1)
    split(CREATED_FLOOR, datetime(end.year, end.month, end.day), total)
    return out


def pull(crm: CRM, preset: str, want: int, state: dict, ledger: dict, out_dir: Path) -> None:
    must = search_shape(crm.preset_must(preset))
    total, _ = crm.search(must, limit=1)
    state["preset_total"] = total
    if not state.get("slices"):
        state["slices"] = created_slices(crm, must, total)
        state["slice"], state["offset"] = 0, 0       # a list begun before the slices starts its reading over
    slices = state["slices"]
    seen, owners = set(state["seen"]), {c["owner"] for c in state["candidates"]}
    loaded_owners = set(ledger.get("owners", {}))
    loaded_emails = set(ledger.get("emails", {}))
    skip = Counter(state["skipped"])
    si, offset = int(state.get("slice", 0)), int(state.get("offset", 0))
    reads_since_save = 0
    rejected = load_json(REJECTED, {})
    cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - REJECT_DAYS * 86400))
    today = time.strftime("%Y-%m-%d")

    def reject(u: str, why: str) -> None:
        skip[why] += 1
        rejected[u] = today
    print(f"{preset}: {total:,} records in {len(slices)} slice(s); collecting {want} candidates", flush=True)
    while len(state["candidates"]) < want and si < len(slices):
        lo, hi, n = slices[si]
        if offset >= min(n, WINDOW):
            si, offset = si + 1, 0
            continue
        part = must if lo is None else dict(must, created=[lo, hi])
        _, rows = crm.search(part, limit=PAGE, offset=offset)
        if not rows:
            si, offset = si + 1, 0
            continue
        whole_page = True
        for row in rows:
            if len(state["candidates"]) >= want:
                # Stopped partway: the next run reads this page again (the rows done are in `seen`). Moving
                # on a whole page here skipped the rest of it for good in a resumed build.
                whole_page = False
                break
            u = row.get("uuid")
            if not u or u in seen:
                continue
            seen.add(u)
            if rejected.get(u, "") >= cutoff:
                skip["ruled out in the last %d days" % REJECT_DAYS] += 1
                continue
            ow = row.get("owner") or {}
            ou = ow.get("uuid") or ""
            v = gate.check_row(row, CHANNEL)
            if not v.ok:
                skip["row: " + v.code] += 1
                continue
            if deep_prospect_batch.is_entity(ow):
                skip["company, trust or no full name"] += 1
                continue
            st = str((row.get("address") or {}).get("state") or "").upper()
            if STATES and st not in STATES:
                # The copy says "a local buyer in <city>": a house outside the market makes that false.
                skip["outside " + "/".join(sorted(STATES))] += 1
                continue
            if ou in owners or ou in loaded_owners:
                skip["owner already on a list"] += 1
                continue
            rec = crm.record(u)
            reads_since_save += 1
            v = email_verdict(rec, row)
            if not v.ok:
                reject(u, "gate: " + v.code)
                continue
            o = rec.get("owner") or {}
            es = emails_of(rec)
            if not es:
                reject(u, "no email on record")
                continue
            e = pick_email(o, es)
            if not e:
                reject(u, "no email with the owner's name")
                continue
            if e in loaded_emails or any(c["email"] == e for c in state["candidates"]):
                skip["email already used"] += 1
                continue
            a = rec.get("address") or {}
            first = touches.clean_first(f"{o.get('first_name') or ''} {o.get('last_name') or ''}")
            state["candidates"].append({
                "record": u, "owner": o.get("uuid") or ou, "email": e,
                "first_name": first, "last_name": tidy_words(o.get("last_name") or ""),
                "street": tidy_words(a.get("street") or ""), "city": tidy_words(a.get("city") or ""),
                "state": a.get("state") or "", "zip": (a.get("postal_code") or "")[:5], "emails_on_record": len(es),
            })
            owners.add(ou)
            if reads_since_save >= 50:
                state.update(seen=sorted(seen), skipped=dict(skip), slice=si, offset=offset)
                save_json(out_dir / "state.json", state)
                save_json(REJECTED, rejected)
                reads_since_save = 0
                print(f"  {len(state['candidates'])} candidates after {len(seen)} rows", flush=True)
        if whole_page:
            offset += PAGE
    state.update(seen=sorted(seen), skipped=dict(skip), slice=si, offset=offset)
    save_json(out_dir / "state.json", state)
    save_json(REJECTED, rejected)


def verify(state: dict, out_dir: Path) -> None:
    from zerobounce import ZeroBounce
    zb = ZeroBounce()
    todo = [c["email"] for c in state["candidates"] if c["email"] not in state["zb"]]
    if not todo:
        return
    before = zb.credits()
    if before < len(todo):
        raise SystemExit(f"ZeroBounce has {before} credits, {len(todo)} needed")
    print(f"checking {len(todo)} addresses with ZeroBounce (Verify+ on), {before:,} credits", flush=True)
    for i in range(0, len(todo), 200):
        for r in zb.validate_batch(todo[i:i + 200], verify_plus=True):
            state["zb"][r["address"].lower()] = {"status": r.get("status"), "sub_status": r.get("sub_status")}
        save_json(out_dir / "state.json", state)
        print(f"  {min(i + 200, len(todo))} of {len(todo)} checked", flush=True)
    state["zb_credits_used"] = state.get("zb_credits_used", 0) + before - zb.credits()
    save_json(out_dir / "state.json", state)


def siblings(crm: CRM, state: dict, out_dir: Path) -> None:
    """The owner's other properties: one stop or in-house lane anywhere takes the owner off."""
    for c in state["candidates"]:
        if (state["zb"].get(c["email"]) or {}).get("status") != "valid" or c["record"] in state["owner_check"]:
            continue
        n, rows = crm.search({"owners": [c["owner"]]}, limit=50)
        why = ""
        for row in rows:
            if row.get("uuid") == c["record"]:
                continue
            v = gate.check_row(row, CHANNEL)
            # Another house of theirs that sold or is listed says nothing about the house we are
            # asking about (34 of 570 owners were held for exactly that on the first run).
            v.reasons = [r for r in v.reasons if r[0] not in ("sold", "listed")]
            if v.reasons:
                why = "another property: " + v.reasons[0][0]
                break
            rec = crm.record(row["uuid"])
            v = email_verdict(rec, row)
            hard = [r for r in v.reasons if r[0] in ("do_not_market", "litigator", "opted_out", "lead", "finished",
                                                     "in_research", "deceased")]   # not "sold" or "listed"
            if hard:
                why = "another property: " + hard[0][0]
                break
            lane = lanes_mod.lane_of(rec)
            if lane in IN_HOUSE:
                why = "another property is in the " + lanes_mod.NAMES.get(lane, lane) + " lane"
                break
        state["owner_check"][c["record"]] = {"properties": n, "hold": why}
        save_json(out_dir / "state.json", state)


def finish(state: dict, out_dir: Path) -> list[dict]:
    final, zb_count, hold = [], Counter(), Counter()
    rejected = load_json(REJECTED, {})
    today = time.strftime("%Y-%m-%d")
    for c in state["candidates"]:
        z = (state["zb"].get(c["email"]) or {}).get("status")
        if z is None:
            continue
        zb_count[z] += 1
        if z != "valid":
            if z != "unknown":            # an unknown is free and may read differently next month
                rejected[c["record"]] = today
            continue
        why = (state["owner_check"].get(c["record"]) or {}).get("hold")
        if why is None:
            continue
        if STATES and str(c.get("state") or "").upper() not in STATES:
            why = "outside " + "/".join(sorted(STATES))
        if why:
            hold[why] += 1
            rejected[c["record"]] = today
            continue
        final.append(dict(c, street=tidy_words(c["street"])))
    save_json(REJECTED, rejected)
    save_json(out_dir / "leads.json", final)
    state["result"] = {"verified": dict(zb_count), "held_for_other_property": dict(hold), "leads": len(final)}
    save_json(out_dir / "state.json", state)
    return final


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--preset", default="Agency - 01 Queue")
    ap.add_argument("--target", type=int, default=600, help="valid leads wanted")
    ap.add_argument("--ratio", type=float, default=1.9, help="candidates gathered per valid lead wanted")
    ap.add_argument("--verify", action="store_true", help="spend ZeroBounce credits and run the owner check")
    ap.add_argument("--slug", default="")
    ap.add_argument("--states", default="TN", help="comma list of states where the copy's 'local buyer' is true")
    a = ap.parse_args()
    if a.verify:
        coldstate.require("build and check a cold email list", LISTS.parent)
    STATES.clear()
    STATES.update(s.strip().upper() for s in a.states.split(",") if s.strip())
    slug = a.slug or re.sub(r"[^a-z0-9]+", "_", a.preset.lower()).strip("_") + "_" + time.strftime("%Y%m%d")
    out_dir = LISTS / slug
    state = load_json(out_dir / "state.json", {}) or {}
    state.setdefault("preset", a.preset)
    for k, d in (("seen", []), ("candidates", []), ("skipped", {}), ("zb", {}), ("owner_check", {})):
        state.setdefault(k, d)
    ledger = load_json(LEDGER, {"owners": {}, "emails": {}})
    crm = CRM()
    pull(crm, a.preset, int(a.target * a.ratio), state, ledger, out_dir)
    print("candidates:", len(state["candidates"]), "| skipped:", json.dumps(dict(Counter(state["skipped"]).most_common())), flush=True)
    if not a.verify:
        print("read only: nothing checked with ZeroBounce. Re-run with --verify.")
        return 0
    verify(state, out_dir)
    siblings(crm, state, out_dir)
    final = finish(state, out_dir)
    print("result:", json.dumps(state["result"]), "| ZeroBounce credits used:", state.get("zb_credits_used"))
    print("leads file:", out_dir / "leads.json")    # the brain's state folder is not under the repo
    return 0 if final else 2


if __name__ == "__main__":
    sys.exit(main())

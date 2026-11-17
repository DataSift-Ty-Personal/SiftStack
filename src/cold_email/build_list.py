"""Build a cold email list from a DataSift preset: one checked email per owner, never anyone we
should not contact.

    python src/cold_email/build_list.py --preset "Agency - 01 Queue" --target 600            # read only
    python src/cold_email/build_list.py --preset "Agency - 01 Queue" --target 600 --verify   # + ZeroBounce

Per record, in this order (the cheap checks first, so a skipped owner costs nothing):
  1. the search row: status and do-not-mail flags (gate.check_row), entity owners out (people only),
     an owner already on this list or loaded before (ledger.json) out
  2. the full record: the same gate every other channel uses, on the mail channel (do not market,
     litigator, opted out, DNC list, sold, deceased or obituary, a lead, a finished status, listed,
     not single family, mail only, do not mail). Returned postal mail does not stop an email.
  3. one address: the first whose name part carries the owner's first or last name. Skip-traced
     emails are a loose list (median 5 per owner, many belong to other people), so an owner with no
     name match is left out rather than guessed.
Then with --verify: ZeroBounce with Verify+ on every address, keep "valid" only; and the owner's
other properties are read, and any that is a lead, opted out, finished, or in an in-house lane
(first to market, Top 1,000, obituary, heirs, hot auction) takes the owner off the list.

Writes output/cold_email/lists/<slug>/ (gitignored: it holds owner emails). Resumable. Read only
against DataSift; the only spend is ZeroBounce credits under --verify.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent
ROOT = SRC.parent
for p in (str(SRC), str(HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

from agents.autopilot import gate, lanes as lanes_mod  # noqa: E402
from agents.autopilot.crm import CRM, search_shape  # noqa: E402
import deep_prospect_batch  # noqa: E402

_spec = importlib.util.spec_from_file_location("touches", SRC / "sms_agent" / "knowledge" / "touches.py")
touches = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(touches)

LISTS = ROOT / "output" / "cold_email" / "lists"
LEDGER = ROOT / "output" / "cold_email" / "ledger.json"
# Records ruled out (gate, no named email, a dead address, another property) are not read again for
# this many days, so the nightly top-up spends its DataSift reads on records it has not judged yet.
REJECTED = ROOT / "output" / "cold_email" / "rejected.json"
REJECT_DAYS = 30
EMAIL_IGNORES = {"return_mail"}          # postal problems that say nothing about an email address
IN_HOUSE = {"lead", "heirs", "hot_auction", "ftm", "obituary", "top1000"}
UPPER_KEEP = {"N", "S", "E", "W", "NE", "NW", "SE", "SW", "US", "TN", "II", "III", "IV"}
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


def email_verdict(rec: dict, row: dict | None = None) -> gate.Verdict:
    v = gate.check(rec, "mail", row=row)
    v.reasons = [r for r in v.reasons if r[0] not in EMAIL_IGNORES]
    v.ok = not v.reasons
    return v


def pull(crm: CRM, preset: str, want: int, state: dict, ledger: dict, out_dir: Path) -> None:
    must = search_shape(crm.preset_must(preset))
    total, _ = crm.search(must, limit=1)
    state["preset_total"] = total
    seen, owners = set(state["seen"]), {c["owner"] for c in state["candidates"]}
    loaded_owners = set(ledger.get("owners", {}))
    loaded_emails = set(ledger.get("emails", {}))
    skip = Counter(state["skipped"])
    offset = state.get("offset", 0)
    reads_since_save = 0
    rejected = load_json(REJECTED, {})
    cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - REJECT_DAYS * 86400))
    today = time.strftime("%Y-%m-%d")

    def reject(u: str, why: str) -> None:
        skip[why] += 1
        rejected[u] = today
    print(f"{preset}: {total:,} records; collecting {want} candidates", flush=True)
    while len(state["candidates"]) < want and offset < min(total, 10000):
        _, rows = crm.search(must, limit=250, offset=offset)
        if not rows:
            break
        for row in rows:
            if len(state["candidates"]) >= want:
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
            v = gate.check_row(row, "mail")
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
                state.update(seen=sorted(seen), skipped=dict(skip), offset=offset)
                save_json(out_dir / "state.json", state)
                save_json(REJECTED, rejected)
                reads_since_save = 0
                print(f"  {len(state['candidates'])} candidates after {len(seen)} rows", flush=True)
        offset += 250
    state.update(seen=sorted(seen), skipped=dict(skip), offset=offset)
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
            v = gate.check_row(row, "mail")
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
    print("leads file:", (out_dir / "leads.json").relative_to(ROOT))
    return 0 if final else 2


if __name__ == "__main__":
    sys.exit(main())

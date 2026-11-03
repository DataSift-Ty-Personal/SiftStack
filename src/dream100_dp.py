"""Deep prospecting v5 on the top of the Dream 100 (build 1.0.56, 2026-09-23).

Takes the N largest opportunities in output/dream100/dream100_ranked.json through the whole
deep-prospecting v5 chain (skills/deep-prospecting-v5) and hands back three things: a research
pack PDF per property, one demo-grade workbook plus a redacted twin for audiences outside the
team, and a CRM write-back.

    python src/dream100_dp.py --phase records             # live CRM, county tax, street photo. Free.
    python src/dream100_dp.py --phase smartskip            # upload + calculate, FREE
    python src/dream100_dp.py --phase smartskip --commit   # pay, poll, download, parse
    python src/dream100_dp.py --phase merge                # SmartSkip + research -> people.json
    python src/dream100_dp.py --phase gapfill --commit     # Tracerfy, phoneless decision makers
    python src/dream100_dp.py --phase score --commit       # Trestle on new numbers only
    python src/dream100_dp.py --phase build                # packs (md + pdf) + workbook + twin
    python src/dream100_dp.py --phase writeback [--commit] # phones, pinned note, tag, death fields
    python src/dream100_dp.py --phase qa

Two layers are written by a researcher, never by this module: research/<slug>.json (sourced
facts: who died, verbatim survivors, identity for a common name) and analysis/<slug>.json (the
judgment: headline, who must sign, the outreach plan). This module renders and writes what
those files say. It never invents a relative, a death or a relationship.

Rules carried from the v5 skill, each one a live failure:
  * SmartSkip's Deceased flag is advisory. A death is a fact only with an obituary behind it.
  * THE SPOUSE-OBITUARY TRAP: an obituary on the record does not mean the owner died.
  * THE OWNER RULE: a number the owner also holds carries tier and source, never a
    relationship, or the dial sheet labels the owner's own landline "Son".
  * Never a person's name in a phone tag. Names live in the note, with last-4 numbers.
  * upsert-phones replaces the object BY NUMBER, so existing phones are never sent at all.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime
from pathlib import Path

SRC = Path(__file__).resolve().parent
ROOT = SRC.parent
sys.path.insert(0, str(SRC))

RANKED = ROOT / "output/dream100/dream100_ranked.json"
FULL = ROOT / "output/dream100/full_records.json"
VENDOR_GLOB = "output/visual_distress/job_*.jsonl"
D4D_SCORES = ROOT / "output/d4d_call_list/phone_scores.json"
SKILL_SCRIPTS = ROOT / "skills/deep-prospecting-v5/scripts"
API_CLIENTS = Path(r"C:\Users\Tyrus\OneDrive\Desktop\Deal Room Coaching Call\_api\clients")
TAXAPI = "https://knox-tn.mygovonline.com/api/v2/parcels/%s?detailLevel=public&start=0&length=25"
SV_META = "https://maps.googleapis.com/maps/api/streetview/metadata"
SV_IMAGE = "https://maps.googleapis.com/maps/api/streetview"

SMARTSKIP_PER_HIT = 0.15
TRACERFY_PER_REC = 0.02
TRESTLE_PER_NUM = 0.015
STREETVIEW_PER_IMG = 0.007

TIERS = ("Dial First", "Dial Second", "Dial Third", "Dial Fourth", "Drop")
WRITE_TIERS = {"Dial First", "Dial Second", "Dial Third"}
MARKER = "[Deep prospecting v5]"
PROPERTY_TAG = "Deep Prospected"
BAD_STATUSES = {"WRONG", "DEAD", "DNC", "CORRECT_DNC", "WRONG_DNC"}
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}


# ------------------------------------------------------------------ plumbing --

class Run:
    def __init__(self, path: str):
        self.dir = ROOT / path
        self.dir.mkdir(parents=True, exist_ok=True)
        for sub in ("research", "analysis", "packs", "img"):
            (self.dir / sub).mkdir(exist_ok=True)

    def p(self, *parts) -> Path:
        return self.dir.joinpath(*parts)

    def load(self, name, default=None):
        f = self.p(name)
        if not f.exists():
            return default
        return json.loads(f.read_text(encoding="utf-8"))

    def save(self, name, obj):
        f = self.p(name)
        tmp = f.with_suffix(f.suffix + ".tmp")
        tmp.write_text(json.dumps(obj, indent=2, default=str, ensure_ascii=False), encoding="utf-8")
        tmp.replace(f)

    def ledger(self, key: str, amount: float, note: str):
        led = self.load("ledger.json", {})
        led[key] = {"usd": round(amount, 4), "note": note, "when": datetime.now().isoformat(timespec="seconds")}
        self.save("ledger.json", led)


def load_env() -> dict:
    env = {}
    f = ROOT / ".env"
    if f.exists():
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    for k, v in env.items():
        os.environ.setdefault(k, v)
    return env


def gate(ok: bool, msg: str):
    if not ok:
        raise SystemExit("STOP: " + msg)


def slug_of(street: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (street or "").lower()).strip("-")


def digits(s) -> str:
    d = re.sub(r"\D", "", str(s or ""))
    return d[1:] if len(d) == 11 and d.startswith("1") else d


def fmt_phone(n: str) -> str:
    n = digits(n)
    return "(%s) %s-%s" % (n[:3], n[3:6], n[6:]) if len(n) == 10 else n


def last4(n: str) -> str:
    return digits(n)[-4:]


def no_dash(s):
    if not isinstance(s, str):
        return s
    return s.replace("\u2014", ", ").replace("\u2013", "-").replace("\u2012", "-")


def http_json(url: str, headers=None, timeout=40):
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def top_rows(n: int) -> list[dict]:
    return json.loads(RANKED.read_text(encoding="utf-8"))[:n]


# ------------------------------------------------------------------ records --

def _crm_clients():
    sys.path.insert(0, str(API_CLIENTS))
    os.environ["REISIFT_ACCOUNT"] = "datasift-apikey"
    from crm_api import CRMClient            # noqa: E402
    from siftmap_api import SiftMapClient    # noqa: E402
    from dossier import resolve_actor, summarize_activity_log  # noqa: E402
    return CRMClient(), SiftMapClient(), resolve_actor, summarize_activity_log


def _when(s) -> str:
    return str(s or "")[:16].replace("T", " ")


def _timeline(events: list) -> list[dict]:
    """Every touch that tells a caller something: calls, texts, notes, status and phone moves.

    The activity log writes one event per call STATE (made, answered, noanswer), so calls are
    folded on the call uuid. Direction comes from the payload, never from who logged it: a
    Kixie dial is logged by "system" and reads like an inbound call if you trust the author.
    """
    calls, sms, out = {}, {}, []
    for e in events:
        et = e.get("event_type") or ""
        pl = e.get("payload") or {}
        ts = _when(e.get("timestamp"))
        if et.startswith("owner.call."):
            c = pl.get("call") or {}
            key = c.get("uuid") or ts + et
            row = calls.setdefault(key, {
                "kind": "call", "when": _when(c.get("created")) or ts,
                "from": digits(c.get("origin_number")), "to": digits(c.get("destination_number")),
                "direction": c.get("direction") or "", "source": c.get("source") or e.get("source") or "",
                "who": (c.get("external_user") or {}).get("name") or "", "outcome": "",
                "seconds": None, "recording": None})
            rank = {"answered": 3, "noanswer": 2, "received": 1, "made": 0}
            state = et.rsplit(".", 1)[-1]
            if rank.get(state, -1) > rank.get(row.get("_state"), -1):
                row["_state"] = state
                row["outcome"] = {"answered": "answered", "noanswer": "no answer",
                                  "received": "received", "made": "dialed"}.get(state, state)
            if c.get("duration") is not None:
                row["seconds"] = c["duration"]
            if c.get("recording"):
                row["recording"] = c["recording"]
            if not row["who"]:
                row["who"] = (c.get("external_user") or {}).get("name") or ""
        elif et.startswith("owner.sms."):
            s = pl.get("sms") or {}
            if et.endswith(".updated"):
                upd = s.get("updated") or []
                born = _when(upd[0]) if isinstance(upd, list) and upd else ""
                st = s.get("status")
                msg = s.get("status_message")
                sms.setdefault(born, {})["status"] = st[-1] if isinstance(st, list) else st
                sms[born]["status_message"] = msg[-1] if isinstance(msg, list) else msg
                continue
            born = _when(s.get("created")) or ts
            sms.setdefault(born, {}).update({
                "kind": "sms", "when": born, "from": digits(s.get("origin_number")),
                "to": digits(s.get("destination_number")), "direction": s.get("direction") or "",
                "who": (s.get("external_user") or {}).get("name") or "", "text": s.get("message") or ""})
        elif et == "property.message.added":
            ax = e.get("author_extra_info") or {}
            out.append({"kind": "note", "when": ts, "who": " ".join(
                x for x in (ax.get("first_name"), ax.get("last_name")) if x) or str(e.get("author") or ""),
                "text": pl.get("message") or ""})
        elif et == "property.status.updated":
            out.append({"kind": "status", "when": ts, "change": (pl.get("property") or {}).get("status")})
        elif et == "owner.phone.status.updated":
            ow = pl.get("owner") or {}
            out.append({"kind": "phone_status", "when": ts, "phone": digits(ow.get("phone")),
                        "change": ow.get("status")})
    for row in calls.values():
        row.pop("_state", None)
        out.append(row)
    out.extend(v for v in sms.values() if v.get("kind") == "sms")
    out.sort(key=lambda r: r.get("when") or "")
    return out


STRICT_PROMPT = """Transcribe this phone call recording verbatim. The recording is {sec} seconds long.
Rules:
- Only write words you can clearly hear. Never invent names, street addresses, phone numbers or dialogue.
- Label each turn CALLER (our company representative, the one asking about a property), CONTACT (the person who answered) or GREETING (an automated or recorded voicemail greeting).
- Write [unclear] for any stretch that is silent, music, ringing or not intelligible.
- The whole transcript must stay under {max_words} words, because that is all the audio can hold.
Output plain text only, one turn per line."""

# Speech tops out near 3 words a second. The first transcription pass on this run returned
# 660 words for an 11 second clip, a whole invented conversation about a house in another
# state, so a transcript is only evidence when its word rate is physically possible.
MAX_WORDS_PER_SEC = 3.0


def _audio_seconds(mp3: Path) -> float | None:
    import subprocess
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                              "-of", "csv=p=0", str(mp3)], capture_output=True, text=True, timeout=30)
        return float(out.stdout.strip())
    except Exception:  # noqa: BLE001
        return None


def _words(text: str) -> int:
    return len(text.split("DELIVERY SUMMARY")[0].split())


def _transcribe_calls(run: Run, slug: str, timeline: list) -> float:
    """Transcribe every answered call that has a recording, then police the result.

    Pass one is the coaching engine's prompt. A transcript whose word rate is impossible is
    marked unreliable and never used. Every plausible one gets a strict second pass that forbids
    guessing, and the strict text is what the research pack quotes.
    """
    sys.path.insert(0, str(SRC / "call_coaching"))
    import base64
    from transcribe import _openrouter, transcribe_audio  # noqa: E402
    spent = 0.0
    (run.p("audio")).mkdir(exist_ok=True)
    for row in timeline:
        if row.get("kind") != "call" or not row.get("recording") or (row.get("seconds") or 0) < 8:
            continue
        cid = hashlib.sha1(row["recording"].encode()).hexdigest()[:12]
        mp3 = run.p("audio", "%s_%s.mp3" % (slug, cid))
        txt = run.p("audio", "%s_%s.txt" % (slug, cid))
        strict = run.p("audio", "%s_%s_strict.txt" % (slug, cid))
        row["transcript_file"] = str(txt.relative_to(ROOT))
        if not mp3.exists():
            req = urllib.request.Request(row["recording"], headers=UA)
            with urllib.request.urlopen(req, timeout=60) as r:
                mp3.write_bytes(r.read())
        if not txt.exists():
            txt.write_text(transcribe_audio(mp3, agent_name=(row.get("who") or "").split(" ")[0] or None),
                           encoding="utf-8")
            spent += max(1, (row.get("seconds") or 60) / 60.0) * 0.002
        row["transcript"] = txt.read_text(encoding="utf-8")
        sec = _audio_seconds(mp3) or float(row.get("seconds") or 0)
        row["audio_seconds"] = round(sec, 1)
        row["transcript_reliable"] = bool(sec) and _words(row["transcript"]) <= MAX_WORDS_PER_SEC * sec
        if not row["transcript_reliable"]:
            continue
        if not strict.exists():
            b64 = base64.b64encode(mp3.read_bytes()).decode()
            prompt = STRICT_PROMPT.format(sec=int(sec), max_words=int(MAX_WORDS_PER_SEC * sec))
            strict.write_text(_openrouter([{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "input_audio", "input_audio": {"data": b64, "format": "mp3"}}]}]), encoding="utf-8")
            spent += max(1, sec / 60.0) * 0.002
        row["transcript_strict"] = strict.read_text(encoding="utf-8")
        row["transcript_reliable"] = _words(row["transcript_strict"]) <= MAX_WORDS_PER_SEC * sec
    return spent


def _vendor_rows(ids: set) -> dict:
    found = {}
    for f in sorted(ROOT.glob(VENDOR_GLOB)):
        for line in f.open(encoding="utf-8"):
            if not any(i in line for i in ids):
                continue
            try:
                o = json.loads(line)
            except ValueError:
                continue
            pid = str(o.get("PropertyID") or "")
            if pid in ids:
                found[pid] = o
    return found


def _tax(query: str) -> dict:
    try:
        return http_json(TAXAPI % urllib.parse.quote(query))
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)[:200]}


def _bearing(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def _street_view(run: Run, slug: str, lat, lon) -> dict:
    key = os.environ.get("GOOGLE_STREETVIEW_API_KEY", "")
    out = run.p("img", slug + ".jpg")
    if out.exists():
        return {"file": str(out.relative_to(ROOT)), "cached": True}
    if not (key and lat and lon):
        return {"error": "no key or no coordinates"}
    q = urllib.parse.urlencode({"location": "%s,%s" % (lat, lon), "key": key, "source": "outdoor",
                                "radius": 60})
    meta = http_json(SV_META + "?" + q)
    if meta.get("status") != "OK":
        return {"error": "no imagery: %s" % meta.get("status")}
    loc = meta.get("location") or {}
    heading = _bearing(loc["lat"], loc["lng"], float(lat), float(lon))
    q = urllib.parse.urlencode({"pano": meta["pano_id"], "size": "640x400", "fov": 75,
                                "heading": round(heading, 1), "pitch": 2, "key": key})
    req = urllib.request.Request(SV_IMAGE + "?" + q, headers=UA)
    with urllib.request.urlopen(req, timeout=40) as r:
        out.write_bytes(r.read())
    return {"file": str(out.relative_to(ROOT)), "pano_date": meta.get("date"), "heading": round(heading, 1)}


def _owner_query(title_owner: str) -> str:
    toks = re.sub(r"[^A-Z ]", " ", (title_owner or "").upper()).split()
    return " ".join(toks[:2])


def phase_records(args, run: Run):
    load_env()
    rows = top_rows(args.top)
    full = json.loads(FULL.read_text(encoding="utf-8"))
    vendor = _vendor_rows({str(r["dataflik_id"]) for r in rows})
    crm, smc, resolve_actor, summarize = _crm_clients()
    out, sv_cost, tx_cost = [], 0.0, 0.0
    for i, r in enumerate(rows, 1):
        slug = slug_of(r["street"])
        uuid = r["uuid"]
        rec = crm.get_record(uuid)
        msgs = crm.get_messages(uuid)
        cfs = crm.get_custom_fields(uuid)
        events = crm.get_activity_log(uuid, limit=50, max_pages=6)
        events = [{**e, "actor": resolve_actor(e)} for e in events]
        timeline = _timeline(events)
        tx_cost += _transcribe_calls(run, slug, timeline)
        sm = {}
        if r.get("dataflik_id"):
            try:
                sm = smc.get_detail(r["dataflik_id"])
            except Exception as exc:  # noqa: BLE001
                sm = {"_error": str(exc)[:200]}
        full_rec = full.get(uuid) or {}
        parcel = (full_rec.get("parcel_id") or rec.get("parcel_id") or "").strip()
        tax = _tax(full_rec.get("parcel_id") or parcel)
        title_owner = ((tax.get("parcels") or [{}])[0] or {}).get("owner", "")
        portfolio = _tax(_owner_query(title_owner)) if title_owner else {}
        vrow = vendor.get(str(r["dataflik_id"])) or {}
        sv = _street_view(run, slug, vrow.get("lat") or r.get("lat"), vrow.get("lon") or r.get("lon"))
        if sv.get("file") and not sv.get("cached"):
            sv_cost += STREETVIEW_PER_IMG
        owner = rec.get("owner") or {}
        out.append({
            "rank": i, "slug": slug, "uuid": uuid, "owner_uuid": owner.get("uuid") or r.get("owner_uuid"),
            "dataflik_id": r.get("dataflik_id"), "row": r,
            "record": rec, "messages": msgs,
            "custom_fields": [{"label": (c.get("custom_field") or {}).get("label"), "value": c.get("value")}
                              for c in cfs if c.get("value") not in (None, "")],
            "activity_summary": summarize(events),
            "timeline": timeline,
            "siftmap": {k: sm.get(k) for k in ("sale_history", "mortgage_history", "obituary_history",
                                               "mls_history", "distressor_data", "owner_info",
                                               "tax_delinquent", "tax_delinquent_year", "tax_lien",
                                               "foreclosure", "last_sale_date", "last_sale_price",
                                               "total_market_value", "lot_acress", "buildings_count",
                                               "flood_zone_description", "subdivision")},
            "tax": tax, "title_owner": title_owner, "portfolio": portfolio,
            "vendor": {k: vrow.get(k) for k in vrow if k.endswith("_score") or k in (
                "distress_explanation", "lat", "lon", "neighborhood", "assessed_value")},
            "street_view": sv,
        })
        print("  %d. %s | CRM %s | msgs %d | events %d | title %s | tax due %s | photo %s" % (
            i, r["street"], "ok" if rec else "MISSING", len(msgs), len(events), title_owner or "?",
            ((tax.get("parcels") or [{}])[0] or {}).get("amount_due"), sv.get("file") or sv.get("error")))
    run.save("records.json", out)
    if sv_cost:
        run.ledger("streetview", sv_cost, "%d Street View images" % round(sv_cost / STREETVIEW_PER_IMG))
    if tx_cost:
        run.ledger("transcription", tx_cost, "answered call recordings, Gemini 2.5 Flash via OpenRouter")
    print("records: %d written to %s" % (len(out), run.p("records.json").relative_to(ROOT)))


# ---------------------------------------------------------------- smartskip --

# Title strings are cleaner than the CRM's split names (the county writes "CHARLES B JR" and
# "BILLY J", the CRM drops both), so the SmartSkip rows carry the middle initial where the
# title has one. A co-owner on the title gets a row of her own: if one spouse has died, the
# survivor's relatives are the ones that matter.
def smartskip_rows(recs: list[dict]) -> list[dict]:
    out = []
    for x in recs:
        rec, row = x["record"], x["row"]
        ow = rec.get("owner") or {}
        ma = ow.get("address") or {}
        pa = rec.get("address") or {}
        base = {"Mailing Address": ma.get("street") or row.get("mail_street") or "",
                "Mailing City": ma.get("city") or row.get("mail_city") or "",
                "Mailing State": ma.get("state") or row.get("mail_state") or "",
                "Mailing Zip": (ma.get("postal_code") or row.get("mail_zip") or "")[:5],
                "Property Address": pa.get("street") or row.get("street"),
                "Property City": pa.get("city") or row.get("city"),
                "Property State": pa.get("state") or "TN",
                "Property Zip": (pa.get("postal_code") or str(row.get("zip") or ""))[:5]}
        people = _title_people(x)
        for first, middle, last in people:
            out.append({"First Name": first, "Middle Name": middle, "Last Name": last, **base,
                        "_slug": x["slug"]})
    return out


def _title_people(x: dict) -> list[tuple[str, str, str]]:
    """(first, middle, last) for every human on the title, from the county string when it parses."""
    rec = x["record"]
    ow = rec.get("owner") or {}
    first = (ow.get("first_name") or "").strip()
    last = (ow.get("last_name") or "").strip()
    title = (x.get("title_owner") or "").upper()
    toks = re.sub(r"[^A-Z& ]", " ", title).split()
    middle = ""
    if toks and last and toks[0] == last.upper() and len(toks) >= 3 and toks[1] == first.split()[0].upper():
        cand = toks[2]
        if len(cand) == 1 and cand.isalpha():           # "HOUSER GEORGE & JO ANN" has no middle
            middle = cand
    if " " in first and len(first.split()[-1]) == 1:          # "Shirley M"
        first, middle = first.split()[0], first.split()[-1]
    people = [(first.title() if first.isupper() else first, middle, last.title() if last.isupper() else last)]
    for so in rec.get("secondary_owners") or []:
        nm = (so.get("full_name") or "").strip()
        if nm:
            parts = nm.split()
            if len(parts) >= 3 and parts[1][:1].isupper() and parts[0].lower() in ("jo", "mary", "anna", "betty"):
                people.append((" ".join(parts[:2]), "", " ".join(parts[2:])))
            else:
                people.append((parts[0], "", " ".join(parts[1:])))
    return people


def _ss_module(run: Run):
    load_env()
    os.environ["SKIPTRACE_RUN_DIR"] = str(run.p(".skiptrace_run"))
    sys.path.insert(0, str(SKILL_SCRIPTS))
    import smartskip_trace as ss  # noqa: E402  (reads SKIPTRACE_RUN_DIR at import)
    import parse_smartskip as ps   # noqa: E402
    return ss, ps


def phase_smartskip(args, run: Run):
    recs = run.load("records.json")
    gate(bool(recs), "run --phase records first")
    rows = smartskip_rows(recs)
    csv_path = run.p("ss_input.csv")
    cols = ["First Name", "Middle Name", "Last Name", "Mailing Address", "Mailing City",
            "Mailing State", "Mailing Zip", "Property Address", "Property City", "Property State",
            "Property Zip"]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    for r in rows:
        w.writerow(r)
    body = buf.getvalue()
    digest = hashlib.sha1(body.encode()).hexdigest()[:12]
    csv_path.write_text(body, encoding="utf-8")
    for r in rows:
        print("  %-10s %-8s %-3s %-14s | %s, %s %s" % (r["First Name"], "", r["Middle Name"], r["Last Name"],
                                                   r["Mailing Address"], r["Mailing City"], r["Mailing State"]))
    ss, ps = _ss_module(run)
    sess = ss.Session()
    state = run.load("smartskip_state.json", {})
    if state.get("digest") != digest or not state.get("bulkSkipId"):
        entry = ss.submit(sess, str(csv_path), {})
        state = {"digest": digest, "bulkSkipId": entry["bulkSkipId"], "entities": entry.get("entities"),
                 "rows": len(rows), "paid": False, "submitted": datetime.now().isoformat(timespec="seconds")}
        run.save("smartskip_state.json", state)
    bid = state["bulkSkipId"]
    print("smartskip: bulkSkipId %s, billable entities %s of %d rows" % (bid, state.get("entities"), len(rows)))
    gate((state.get("entities") or 0) <= len(rows), "calculate returned more entities than rows")
    if not args.commit:
        print("DRY: nothing billed. Re-run with --commit to pay (max $%.2f)."
              % (len(rows) * SMARTSKIP_PER_HIT))
        return
    if not state.get("paid"):
        ok = ss.pay(sess, bid)
        gate(ok, "SmartSkip payment did not go through (3DS or decline); finish it in the app, then re-run")
        state["paid"] = True
        run.save("smartskip_state.json", state)
    t0 = time.time()
    while True:
        items = ss.fetch_status(sess, bid)
        st = ((items[0].get("status") if items else "") or "").lower()
        print("  status=%s" % (st or "?"), flush=True)
        if st == "completed":
            break
        gate(st not in ("error", "failed"), "bulk skip ended in status %s" % st)
        gate(time.time() - t0 < 900, "timed out waiting; re-run later, the order is paid and saved")
        time.sleep(15)
    ss.download(sess, bid, str(run.p("ss_vertical.csv")), "vertical")
    ss.download(sess, bid, str(run.p("ss_horizontal.csv")), "horizontal")
    parsed = ps.parse(str(run.p("ss_vertical.csv")))
    hits = sum(1 for p in parsed if p.get("has_results"))
    run.save("smartskip.json", parsed)
    run.ledger("smartskip", hits * SMARTSKIP_PER_HIT, "%d hits of %d rows, order %s" % (hits, len(rows), bid))
    for p in parsed:
        print("  %-26s subject phones %2d | relatives %2d (%d with phones) | deceased flag %s" % (
            p["input_name"][:26], len(p["subject_phones"]), len(p["relatives"]),
            sum(1 for rl in p["relatives"] if rl["phones"]), p["deceased"]))
    print("smartskip: %d hits, about $%.2f" % (hits, hits * SMARTSKIP_PER_HIT))


# -------------------------------------------------------------------- merge --

IMMEDIATE = {"Husband", "Wife", "Spouse", "Son", "Daughter", "Child", "Mother", "Father",
             "Parent", "Brother", "Sister", "Sibling"}


def _nm(s: str) -> str:
    return re.sub(r"[^a-z]", "", (s or "").lower())


def _na(s: str) -> str:
    """Address key: keeps the house number, so 6517 and 6519 Sample Rd stay two places."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def title_name(s: str) -> str:
    out = []
    for w in (s or "").strip().split():
        t = w.capitalize() if w.isupper() or w.islower() else w
        if re.match(r"^Mc[a-z]", t):
            t = "Mc" + t[2:].capitalize()
        out.append(t)
    return " ".join(out)


def _crm_phones(x: dict) -> dict:
    out = {}
    for p in ((x.get("record") or {}).get("owner") or {}).get("phones") or []:
        n = digits(p.get("number"))
        if n:
            tags = [t if isinstance(t, str) else (t.get("title") or t.get("name") or "")
                    for t in (p.get("tags") or [])]
            out[n] = {"type": (p.get("type") or "UNKNOWN").upper(), "status": (p.get("status") or "UNKNOWN").upper(),
                      "tags": [t for t in tags if t], "raw": p}
    return out


def _csv_emails(run: Run) -> dict:
    out = {}
    f = run.p("ss_vertical.csv")
    if not f.exists():
        return out
    for r in csv.DictReader(f.open(encoding="utf-8-sig")):
        key = (r.get("Input Name"), _nm(r.get("First Name")), _nm(r.get("Last Name")))
        em = [r.get("Email %d" % i).lower() for i in range(1, 6) if r.get("Email %d" % i)]
        out.setdefault(key, {"emails": [], "age": r.get("Age"), "rel": r.get("Relationship"),
                             "deceased": str(r.get("Deceased")).lower() == "true",
                             "middle": r.get("Middle Name") or ""})
        for e in em:
            if e not in out[key]["emails"]:
                out[key]["emails"].append(e)
    return out


def _person(first, last, *, age, label, canon, street, city, state, zipc, phones, ss_deceased,
            via, crm, emails, is_subject, owner_addr, prop_addr, owner_last, prop_state) -> dict:
    ph = []
    for q in phones or []:
        n = digits(q.get("number"))
        if not n or any(p["number"] == n for p in ph):
            continue
        c = crm.get(n) or {}
        ph.append({"number": n, "ss_type": q.get("type") or "", "sources": ["SmartSkip"],
                   "on_crm": bool(c), "crm_status": c.get("status"), "crm_tags": c.get("tags") or []})
    at_owner = bool(street) and _na(street) == _na(owner_addr)
    at_prop = bool(street) and _na(street) == _na(prop_addr)
    in_state = (state or "").upper() == (prop_state or "TN").upper()
    same_last = _nm(last) == _nm(owner_last)
    try:
        age_i = int(str(age).strip())
    except (TypeError, ValueError):
        age_i = None
    return {"key": "%s|%s|%s" % (_nm(first), _nm(last), _na(street)), "name": title_name("%s %s" % (first, last)),
            "first": title_name(first), "last": title_name(last), "age": age_i, "ss_label": label or "",
            "canon": canon or "", "street": title_name(street or ""), "city": title_name(city or ""),
            "state": (state or "").upper(), "zip": (zipc or "")[:5], "phones": ph, "emails": emails or [],
            "ss_deceased": bool(ss_deceased), "via": list(via), "is_subject": is_subject,
            "at_owner_address": at_owner, "at_property": at_prop, "in_state": in_state,
            "score_worthy": (not ss_deceased or is_subject) and (
                is_subject or canon in IMMEDIATE or at_owner or at_prop or (in_state and same_last))}


def phase_merge(args, run: Run):
    """SmartSkip subjects and relatives into one people list per property, ranked as signers."""
    from obituary_dp_run import score_dm  # noqa: E402
    recs = run.load("records.json")
    ss = run.load("smartskip.json")
    gate(bool(recs and ss), "run --phase records and --phase smartskip --commit first")
    meta = _csv_emails(run)
    by_slug = {}
    rows = smartskip_rows(recs)
    slug_for = {("%s %s" % (r["First Name"], r["Last Name"])).strip().lower(): r["_slug"] for r in rows}
    out = []
    for x in recs:
        crm = _crm_phones(x)
        ow = (x["record"].get("owner") or {})
        owner_addr = ((ow.get("address") or {}).get("street") or x["row"].get("mail_street") or "")
        prop_addr = ((x["record"].get("address") or {}).get("street") or x["row"]["street"])
        people = {}
        subjects = [p for p in ss if slug_for.get(p["input_name"].strip().lower()) == x["slug"]]
        for p in subjects:
            m = meta.get((p["input_name"], _nm(p["first"]), _nm(p["last"]))) or {}
            subj = _person(p["first"], p["last"], age=m.get("age"), label="Owner of record",
                           canon="Owner", street=p["mailing_address"], city=p["mailing_city"],
                           state=p["mailing_state"], zipc=p["mailing_zip"], phones=p["subject_phones"],
                           ss_deceased=p["deceased"], via=[p["input_name"]], crm=crm,
                           emails=m.get("emails"), is_subject=True, owner_addr=owner_addr,
                           prop_addr=prop_addr, owner_last=p["last"], prop_state="TN")
            subj["middle"] = m.get("middle") or ""
            people.setdefault(subj["key"], subj)
            rec_for_score = {"last": p["last"], "mailing_address": p["mailing_address"], "property_state": "TN"}
            for rl in p["relatives"]:
                s, why = score_dm(rl, rec_for_score)
                m2 = meta.get((p["input_name"], _nm(rl["first"]), _nm(rl["last"]))) or {}
                per = _person(rl["first"], rl["last"], age=rl.get("age"), label=rl.get("type_raw"),
                              canon=rl.get("canon_rel"), street=rl.get("mailing_street"),
                              city=rl.get("mailing_city"), state=rl.get("mailing_state"),
                              zipc=rl.get("mailing_zip"), phones=rl.get("phones"),
                              ss_deceased=rl.get("deceased"), via=[p["input_name"]], crm=crm,
                              emails=m2.get("emails"), is_subject=False, owner_addr=owner_addr,
                              prop_addr=prop_addr, owner_last=p["last"], prop_state="TN")
                per["dm_score"], per["dm_why"] = round(s, 1), "; ".join(why)
                # the other spouse on the title is a SUBJECT; keep the subject record, add the link
                twin = next((k for k, v in people.items() if v["is_subject"] and _nm(v["first"])[:3] == _nm(per["first"])[:3]
                             and _nm(v["last"]) == _nm(per["last"]) and _na(v["street"]) == _na(per["street"])), None)
                if twin:
                    people[twin]["via"] = sorted(set(people[twin]["via"]) | set(per["via"]))
                    continue
                if per["key"] in people:
                    people[per["key"]]["via"] = sorted(set(people[per["key"]]["via"]) | set(per["via"]))
                    continue
                people[per["key"]] = per
        plist = sorted(people.values(), key=lambda v: (not v["is_subject"], -(v.get("dm_score") or 0)))
        # CRM-only numbers: what DataSift's own trace put on the owner, not in any SmartSkip list
        ss_nums = {q["number"] for v in plist for q in v["phones"]}
        crm_only = [{"number": n, "type": c["type"], "status": c["status"], "tags": c["tags"]}
                    for n, c in crm.items() if n not in ss_nums]
        research = run.load(os.path.join("research", x["slug"] + ".json"))
        out.append({"slug": x["slug"], "people": plist, "crm_only": crm_only,
                    "research_loaded": bool(research)})
        by_slug[x["slug"]] = len(plist)
        print("  %-24s people %2d (score-worthy %2d) | CRM-only numbers %d | research %s" % (
            x["slug"], len(plist), sum(1 for v in plist if v["score_worthy"]), len(crm_only),
            "yes" if research else "pending"))
    run.save("people.json", out)


# ------------------------------------------------------------------- gapfill --

def _analysis(run: Run, slug: str) -> dict:
    return run.load(os.path.join("analysis", slug + ".json"), {}) or {}


def _people_index(run: Run) -> dict:
    return {g["slug"]: {p["key"]: p for p in g["people"]} for g in run.load("people.json", [])}


def phase_gapfill(args, run: Run):
    """Tracerfy only the decision makers the analysis named that SmartSkip left phoneless."""
    from obituary_dp_run import tracerfy_gapfill  # noqa: E402
    env = load_env()
    groups = run.load("people.json", [])
    gate(bool(groups), "run --phase merge first")
    targets = []
    for g in groups:
        an = _analysis(run, g["slug"])
        wanted = {p.get("ss_key") for p in an.get("people") or [] if p.get("role") in ("owner", "signer", "decision maker")}
        for p in g["people"]:
            if p["key"] in wanted and not p["phones"] and p.get("street"):
                targets.append({"slug": g["slug"], "key": p["key"], "first": p["first"], "last": p["last"],
                                "mailing_street": p["street"], "mailing_city": p["city"],
                                "mailing_state": p["state"], "mailing_zip": p["zip"], "phones": []})
    print("gapfill: %d phoneless decision makers: %s" % (len(targets), ", ".join(t["first"] + " " + t["last"] for t in targets)))
    if not targets or not args.commit:
        if targets:
            print("DRY: re-run with --commit (max $%.2f)." % (len(targets) * TRACERFY_PER_REC))
        return
    found, stats = tracerfy_gapfill(targets, env.get("TRACERFY_API_KEY", ""))
    run.save("tracerfy.json", {"found": found, "stats": stats, "targets": targets})
    run.ledger("tracerfy", stats.get("cost", len(targets) * TRACERFY_PER_REC), json.dumps(stats))
    for g in groups:
        for p in g["people"]:
            hit = found.get(("%s %s" % (p["first"], p["last"])).lower())
            if not hit or not any(t["key"] == p["key"] for t in targets):
                continue
            for n in hit["numbers"]:
                if not any(q["number"] == n for q in p["phones"]):
                    p["phones"].append({"number": n, "ss_type": hit["types"].get(n, ""), "sources": ["Tracerfy"],
                                        "on_crm": False, "crm_status": None, "crm_tags": []})
            p["emails"] = sorted(set(p.get("emails") or []) | {e.lower() for e in hit.get("emails") or []})
    run.save("people.json", groups)
    print("gapfill: %s" % stats)


# --------------------------------------------------------------------- score --

def numbers_to_score(run: Run) -> dict:
    """number -> who it reaches. Score-worthy people plus anyone the analysis named."""
    out = {}
    for g in run.load("people.json", []):
        named = {p.get("ss_key") for p in (_analysis(run, g["slug"]).get("people") or [])}
        for p in g["people"]:
            if not (p["score_worthy"] or p["key"] in named):
                continue
            for q in p["phones"]:
                if (q.get("crm_status") or "").upper() in BAD_STATUSES:
                    continue
                out.setdefault(q["number"], []).append("%s (%s)" % (p["name"], g["slug"]))
        for c in g.get("crm_only") or []:
            if c["status"] not in BAD_STATUSES:
                out.setdefault(c["number"], []).append("CRM owner number (%s)" % g["slug"])
    return out


def phase_score(args, run: Run):
    """Trestle on every number that could reach a decision maker, cached numbers free."""
    import d4d_call_list as d4d  # noqa: E402
    load_env()
    want = numbers_to_score(run)
    cache = json.loads(D4D_SCORES.read_text(encoding="utf-8")) if D4D_SCORES.exists() else {}
    scores = run.load("scores.json", {})
    # A CRM number carrying exactly ONE tier tag was already scored; two tiers at once is a
    # conflict the old union writes left behind, and those get a fresh lookup.
    crm_tier = {}
    for g in run.load("people.json", []):
        for p in g["people"]:
            for q in p["phones"]:
                if q.get("on_crm"):
                    crm_tier[q["number"]] = [t for t in q.get("crm_tags") or [] if t in TIERS]
        for c in g.get("crm_only") or []:
            crm_tier[c["number"]] = [t for t in c.get("tags") or [] if t in TIERS]
    for n in want:
        if n in scores and scores[n].get("tier"):
            continue
        if (cache.get(n) or {}).get("tier"):
            scores[n] = {**cache[n], "from_cache": "d4d_call_list 2026-09-11"}
        elif len(crm_tier.get(n) or []) == 1:
            scores[n] = {"tier": crm_tier[n][0], "score": None, "from_cache": "CRM tier tag"}
    todo = [n for n in want if not (scores.get(n) or {}).get("tier")]
    print("score: %d numbers wanted, %d already scored, %d to score (max $%.2f)" % (
        len(want), len(want) - len(todo), len(todo), len(todo) * TRESTLE_PER_NUM))
    if not args.commit:
        run.save("scores.json", scores)
        print("DRY: re-run with --commit to spend.")
        return
    router = d4d._router()
    key = os.environ.get("TRESTLE_PAID_API_KEY") or os.environ.get("TRESTLE_API_KEY")
    paid = free = errors = 0
    for i, n in enumerate(todo, 1):
        res = d4d._score_one(router, key, n)
        if res.get("error"):
            errors += 1
            print("  %s error: %s" % (fmt_phone(n), res["error"][:80]))
            continue
        scores[n] = res
        if res.get("key_used") == "free":
            free += 1
        else:
            paid += 1
        if i % 20 == 0:
            run.save("scores.json", scores)
    run.save("scores.json", scores)
    prior = (run.load("ledger.json", {}).get("trestle") or {}).get("usd", 0)
    run.ledger("trestle", prior + paid * TRESTLE_PER_NUM,
               "%d paid + %d free-tier lookups this pass, %d errors" % (paid, free, errors))
    tiers = {}
    for n in want:
        t = (scores.get(n) or {}).get("tier") or "unscored"
        tiers[t] = tiers.get(t, 0) + 1
    print("score: %d paid, %d free, %d errors | tiers %s" % (paid, free, errors, tiers))


# ------------------------------------------------------------------ assemble --

TIER_ORDER = {"Dial First": 0, "Dial Second": 1, "Dial Third": 2, "Dial Fourth": 3, "Drop": 4, "DNC": 5}
SS_TYPE = {"mobile": "MOBILE", "residential": "LANDLINE", "landline": "LANDLINE"}


def crm_type(scores: dict, n: str, ss_type: str = "") -> str:
    lt = ((scores.get(n) or {}).get("line_type") or "").lower()
    if "voip" in lt:
        return "VOIP"
    if lt in ("mobile", "landline"):
        return lt.upper()
    return SS_TYPE.get((ss_type or "").lower(), "UNKNOWN")


def _short_rel(ap: dict) -> str:
    """'daughter', 'wife', 'owner': the dial sheet needs the tie, not the evidence behind it."""
    if ap.get("role") == "owner":
        return "owner"
    if ap.get("tag"):
        return ap["tag"].lower()
    rel = re.split(r"[(,.;]", ap.get("relationship") or "")[0].strip()
    return rel.lower()


def assemble(run: Run) -> list[dict]:
    """Everything one property needs to render or write: facts, people, judgment, numbers."""
    recs = {x["slug"]: x for x in run.load("records.json", [])}
    groups = {g["slug"]: g for g in run.load("people.json", [])}
    scores = run.load("scores.json", {})
    out = []
    for slug, x in recs.items():
        an = _analysis(run, slug)
        g = groups.get(slug) or {"people": [], "crm_only": []}
        pidx = {p["key"]: p for p in g["people"]}
        crm = _crm_phones(x)
        dial = {}
        for ap in an.get("people") or []:
            p = pidx.get(ap.get("ss_key") or "")
            if not p or ap.get("role") in (None, "", "none"):
                continue
            owner_like = ap.get("role") == "owner" or ap.get("shares_owner_line")
            for q in p["phones"]:
                n = q["number"]
                row = dial.setdefault(n, {"number": n, "reaches": [], "sources": set(), "on_crm": n in crm,
                                          "crm_status": (crm.get(n) or {}).get("status"),
                                          "ss_type": q.get("ss_type") or ""})
                row["reaches"].append({"name": ap.get("name") or p["name"], "relationship": _short_rel(ap),
                                       "tag": ap.get("tag") or "", "role": ap.get("role"), "owner_like": bool(owner_like)})
                row["sources"].update(q.get("sources") or [])
        for n, c in crm.items():
            row = dial.setdefault(n, {"number": n, "reaches": [], "sources": set(), "on_crm": True,
                                      "crm_status": c["status"], "ss_type": ""})
            row["sources"].add("CRM")
            row["crm_type"] = c["type"]
        for n, row in dial.items():
            s = scores.get(n) or {}
            row["tier"] = s.get("tier") or ""
            row["score"] = s.get("score")
            row["litigator"] = bool(s.get("litigator"))
            row["type"] = row.get("crm_type") if row["on_crm"] and row.get("crm_type") else crm_type(scores, n, row["ss_type"])
            row["sources"] = sorted(row["sources"])
            override = (an.get("number_notes") or {}).get(n)
            row["note"] = override or ""
            bad = (row.get("crm_status") or "").upper() in BAD_STATUSES
            row["do_not_dial"] = bad or row["litigator"] or row["tier"] == "Drop"
            owner_line = any(r["owner_like"] for r in row["reaches"])
            rels = [r for r in row["reaches"] if not r["owner_like"]]
            row["owner_rule"] = owner_line and bool(rels)
            if not row["reaches"]:
                row["reaches_label"] = (an.get("crm_number_label") or "Owner record, DataSift trace, holder unconfirmed")
            else:
                row["reaches_label"] = "; ".join(sorted({"%s (%s)" % (r["name"], r["relationship"]) if r["relationship"]
                                                        else r["name"] for r in row["reaches"]}))
        rows = sorted(dial.values(), key=lambda r: (r["do_not_dial"], TIER_ORDER.get(r["tier"], 6),
                                                    0 if r["reaches"] else 1, -(r["score"] or 0),
                                                    0 if r["type"] == "MOBILE" else 1, r["number"]))
        out.append({"slug": slug, "x": x, "an": an, "people": g["people"], "pidx": pidx, "dial": rows,
                    "research": run.load(os.path.join("research", slug + ".json"), {}) or {},
                    "scores": scores})
    out.sort(key=lambda d: d["x"]["rank"])
    return out


def writeback_plan(d: dict) -> dict:
    """New numbers only, for the people the analysis gave a role, tiers First to Third."""
    items = []
    for r in d["dial"]:
        if r["on_crm"] or r["do_not_dial"] or r["tier"] not in WRITE_TIERS or not r["reaches"]:
            continue
        rel_tags = sorted({x["tag"] for x in r["reaches"] if x["tag"] and not x["owner_like"]})
        owner_line = any(x["owner_like"] for x in r["reaches"])
        tags = ([] if owner_line else rel_tags[:1]) + [r["tier"]] + [s for s in r["sources"] if s != "CRM"][:1]
        items.append({"number": r["number"], "type": r["type"], "tags": tags, "status": "UNKNOWN",
                      "is_connected": True, "verified": False,
                      "_reaches": r["reaches_label"]})
    return {"owner_uuid": d["x"]["owner_uuid"], "phones": items}


# ---------------------------------------------------------------------- text --

def money(v) -> str:
    try:
        return "$%s" % format(int(round(float(v))), ",")
    except (TypeError, ValueError):
        return ""


def last4_name(r: dict) -> str:
    return "%s ...%s" % (r.get("reaches_label") or "", last4(r["number"]))


def build_note(d: dict) -> str:
    """The message-board post. Names with last-4 numbers; phone tags never carry a name."""
    an, x = d["an"], d["x"]
    L = [MARKER + " " + date.today().isoformat(), an.get("headline", ""), ""]
    if an.get("owner_status"):
        L.append("Owner status: %s. %s" % (an["owner_status"], an.get("owner_status_note", "")))
    dms = [p for p in an.get("people") or [] if p.get("role") in ("owner", "signer", "decision maker")]
    if dms:
        L.append("Decision makers: " + ", ".join("%s (%s)" % (p["name"], _short_rel(p)) for p in dms[:5]))
    order = an.get("call_order") or [{"who": r["reaches_label"], "number": r["number"],
                                      "why": "%s %s" % (r["tier"], r["type"].lower())}
                                     for r in d["dial"] if not r["do_not_dial"] and r["tier"] in WRITE_TIERS][:4]
    if order:
        L.append("Call order:")
        L += ["  %d. %s ...%s: %s" % (i, c.get("who", ""), last4(c.get("number", "")), c.get("why", ""))
              for i, c in enumerate(order[:4], 1)]
    o = an.get("outreach") or {}
    if o.get("opener"):
        L.append("Opener: " + o["opener"])
    if o.get("avoid"):
        L.append("Do not:")
        L += ["  - " + a for a in o["avoid"][:5]]
    if an.get("corrections"):
        L.append("Record corrections:")
        L += ["  - " + c for c in an["corrections"][:5]]
    L.append("Sources: SmartSkip, Trestle, Knox Trustee tax record, SiftMap deed and mortgage history, "
             "published obituaries and public records. Full detail: the %s research pack PDF and the "
             "Dream 100 top 5 workbook." % x["row"]["street"])
    return no_dash("\n".join(L))


def _md_table(headers, rows) -> list[str]:
    def cell(v):
        return str("" if v is None else v).replace("|", "/").replace("\n", " ")
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    out += ["| " + " | ".join(cell(v) for v in r) + " |" for r in rows]
    return out


def _gist(t: dict) -> str:
    """One line from a transcript that passed the word-rate check. Rules, not a model."""
    if not (t.get("transcript_reliable") and t.get("transcript_strict")):
        return "recording not usable" if t.get("recording") and t.get("transcript") else ""
    s = t["transcript_strict"].lower()
    if "renter" in s:
        return "a renter answered"
    if "wrong number" in s or "don't own that property" in s or "do not own that property" in s:
        return "wrong number"
    if "mailbox is full" in s:
        return "voicemail box full"
    if "not been set up" in s or "mailbox is currently unavailable" in s or "mailbox currently unavailable" in s:
        return "no voicemail box"
    if "record your name" in s:
        return "call screener, message left" if "caller" in s and "property" in s else "call screener"
    if ("greeting" in s or "forwarded to voicemail" in s or "at the tone" in s or "leave a message" in s
            or "not available" in s):
        return "voicemail left" if "caller" in s else "voicemail, no message"
    if "contact" in s:
        return "spoke briefly"
    return ""


def timeline_rows(x: dict) -> list[list]:
    rows = []
    for t in x["timeline"]:
        if t["kind"] == "call":
            who = (t.get("who") or "").split(" ")[0] or ("Kixie dialer" if t.get("source") == "kixie" else "")
            outcome = t.get("outcome") or ""
            gist = _gist(t)
            if gist:
                outcome = "%s, %s" % (outcome, gist) if outcome else gist
            rows.append([t["when"][:10], "Call %s" % t.get("direction", ""), who, fmt_phone(t["to"] if t.get("direction") == "outbound" else t["from"]),
                         outcome + (" (%ss)" % t["seconds"] if t.get("seconds") else "")])
        elif t["kind"] == "sms":
            rows.append([t["when"][:10], "Text %s" % t.get("direction", ""), (t.get("who") or "").split(" ")[0],
                         fmt_phone(t["to"] if t.get("direction") == "outbound" else t["from"]),
                         (t.get("text") or "")[:160] + (" [%s]" % t["status"] if t.get("status") == "failed" else "")])
        elif t["kind"] == "note":
            rows.append([t["when"][:10], "Note", t.get("who") or "", "", t.get("text") or ""])
    return rows


def survivors_quotes(d: dict) -> list[dict]:
    """Verbatim survivors paragraphs. A quote fetched and checked by hand (analysis) beats the
    research agent's copy, which can arrive shortened."""
    an, rs = d["an"], d["research"]
    out = [{"of": q["of"], "text": q["text"]} for q in an.get("survivors_quotes") or []]
    have = {q["of"] for q in out}
    for dec in rs.get("decedents") or []:
        if (dec.get("survivors_verbatim") and dec.get("name") in (an.get("quote_survivors_of") or [])
                and dec["name"] not in have):
            out.append({"of": dec["name"], "text": dec["survivors_verbatim"]})
    return out


def pack_md(d: dict) -> str:
    x, an, rs = d["x"], d["an"], d["research"]
    counter = iter(range(1, 40))

    def sec(title):
        return "## %d) %s" % (next(counter), title)

    row, rec = x["row"], x["record"]
    o = an.get("outreach") or {}
    L = ["# %s, Knoxville, TN %s" % (row["street"], row["zip"]),
         "Deep prospecting research pack, v5. Dream 100 rank %d of 100. Prepared %s for the acquisitions team. "
         "Owner of record: %s. County title: %s." % (x["rank"], date.today().strftime("%B %d, %Y").replace(" 0", " "),
                                                     row["owner"], x.get("title_owner") or "not found"), ""]
    L += ["## Headline", "**%s**" % an.get("headline", "")]
    L += ["- " + p for p in an.get("headline_points") or []]
    L += ["", "## At a glance"]
    glance = [["Owner status", an.get("owner_status", "")],
              ["Decision maker", an.get("decision_maker", "")],
              ["Best first call", an.get("first_call", "")],
              ["Deal size (discount room)", money(row.get("deal_size"))],
              ["Value and equity", "%s AVM, %s%% equity" % (money(row.get("value")), round(row.get("equity_pct") or 0))],
              ["Condition from the street", "D4D visual distress %s of 100. %s" % (row.get("d4d_score"), an.get("condition", ""))],
              ["Taxes", an.get("taxes", "")],
              ["Prior touches", an.get("touches", "")]]
    L += _md_table(["Item", "Detail"], glance)
    L += ["", sec("Level selected and why"), an.get("level", "")]
    L += ["", sec("Sources checked")]
    L += ["- " + s for s in an.get("sources_checked") or []]
    t = an.get("title") or {}
    L += ["", sec("Title and ownership"), "**Vesting:** %s" % t.get("vesting", "")]
    if t.get("chain"):
        L += _md_table(["Date", "From", "To", "Price", "Note"], t["chain"])
    L += ["- " + f for f in t.get("flags") or []]
    if an.get("identity"):
        L += ["", sec("Identity resolution")] + ["- " + i for i in an["identity"]]
    L += ["", sec("Owner status and family findings"), "**%s.** %s" % (an.get("owner_status", ""), an.get("owner_status_note", ""))]
    for e in an.get("evidence") or []:
        L.append("- %s (%s)" % (e.get("finding", ""), e.get("source", "")))
    for q in survivors_quotes(d):
        L += ["", "Survivors paragraph, verbatim from the obituary of %s:" % q["of"], "*%s*" % q["text"].replace("*", "")]
    if an.get("family_tree"):
        L += ["", sec("Family map"), "```"] + an["family_tree"] + ["```"]
    L += ["", sec("Decision makers and contacts")]
    fam = []
    for p in an.get("people") or []:
        best = next((r for r in d["dial"] if not r["do_not_dial"] and any(
            z["name"] == p.get("name") for z in r["reaches"])), None)
        fam.append([p.get("name", ""), p.get("relationship", ""), p.get("age") or "", p.get("location", ""),
                    p.get("living", ""), p.get("role", ""),
                    ("%s %s" % (fmt_phone(best["number"]), best["tier"])) if best
                    else ("deceased, do not dial" if (p.get("living") or "").startswith("Deceased") else "none found")])
    L += _md_table(["Person", "Relationship (source)", "Age", "Lives in", "Living", "Role", "Best number"], fam)
    L += ["", sec("Who must sign to sell")]
    L += _md_table(["Who", "Status", "Why"], [[m.get("who", ""), m.get("status", ""), m.get("why", "")]
                                                for m in an.get("must_sign") or []])
    L += ["- " + f for f in an.get("signing_flags") or []]
    L += ["", sec("Master dial sheet (deduped, best number first)"), "```"]
    L.append("%-15s %-12s %-5s %-9s %-34s %s" % ("PHONE", "TIER", "SCORE", "TYPE", "REACHES", "NOTE"))
    for r in d["dial"]:
        tier = "DO NOT DIAL" if r["do_not_dial"] else r["tier"]
        note = r["note"] or ("CRM marked %s" % r["crm_status"] if (r.get("crm_status") or "") in BAD_STATUSES else
                             ("new, %s" % "/".join(r["sources"]) if not r["on_crm"] else "on CRM"))
        L.append("%-15s %-12s %-5s %-9s %-34s %s" % (fmt_phone(r["number"]), tier[:12], r["score"] if r["score"] is not None else "",
                                                     r["type"].title()[:9], r["reaches_label"][:34], note[:60]))
    L += ["```", "Owner rule: a household line the owner also uses carries tier and source only, never a relationship."]
    tl = timeline_rows(x)
    if tl:
        L += ["", sec("Contact history on this record")]
        L += _md_table(["Date", "Channel", "By", "Number", "What happened"], tl)
    L += ["", sec("Outreach plan"), "**Who to call first:** %s" % o.get("who_first", ""),
          "", "**Angle:** %s" % o.get("angle", ""), "", "**Opening line:** %s" % o.get("opener", "")]
    if o.get("talking_points"):
        L += ["", "**Talking points:**"] + ["- " + tp for tp in o["talking_points"]]
    if o.get("texts"):
        L += ["", "**Text drafts (signed %s):**" % o.get("sender", "")]
        L += ["- Text %d: %s" % (i, tx) for i, tx in enumerate(o["texts"], 1)]
    elif o.get("texts_note"):
        L += ["", "**Texts:** %s" % o["texts_note"]]
    if o.get("letter"):
        L += ["", "**Letter angle:** %s" % o["letter"]]
    if o.get("avoid"):
        L += ["", "**Do not:**"] + ["- " + a for a in o["avoid"]]
    if an.get("corrections") or an.get("open_items"):
        L += ["", sec("Record corrections and open items")]
        L += ["- Correction: " + c for c in an.get("corrections") or []]
        L += ["- Open: " + c for c in an.get("open_items") or []]
    L += ["", "## Sources"]
    for s in an.get("sources") or []:
        L.append("- %s: %s" % (s.get("what", ""), s.get("url", "")))
    return no_dash("\n".join(L)) + "\n"


def render_pdf(md_path: Path, pdf_path: Path, photo: Path | None = None) -> str:
    """The shared renderer's markdown parser, restyled to the DataSift palette with a quiet
    table header, the logo, the street photo and a v5 footer."""
    import deep_prospect_pdf as dpp  # noqa: E402
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.units import inch
    from reportlab.platypus import Image as RLImage, SimpleDocTemplate, Spacer, Table, TableStyle
    navy, blue, ink2 = colors.HexColor("#0A1130"), colors.HexColor("#316AFF"), colors.HexColor("#5B6478")
    from reportlab.lib.enums import TA_LEFT
    dpp.TITLE.textColor = navy
    dpp.TITLE.alignment = TA_LEFT
    dpp.H2.textColor = navy
    dpp.H3.textColor = colors.HexColor("#141D45")
    dpp.BRAND_ACCENT = colors.HexColor("#F5F7FB")      # table header fill: quiet, not a color band
    dpp.CELL_H.textColor = colors.HexColor("#141D45")
    dpp.CODE.backColor = colors.HexColor("#F5F7FB")
    dpp.CODE.textColor = navy
    dpp.BORDER = colors.HexColor("#DDE2EE")
    dpp.FOOT.textColor = ink2
    margin = 0.6 * inch
    avail = letter[0] - 2 * margin
    story = []
    logo = Path(r"C:\Users\Tyrus\OneDrive\Desktop\datasift-design-system\assets\datasift-logo-dark.png")
    head = []
    if logo.exists():
        head.append(RLImage(str(logo), width=1.35 * inch, height=1.35 * inch * 179 / 506))
    if photo and photo.exists():
        head.append(RLImage(str(photo), width=2.6 * inch, height=2.6 * inch * 400 / 640))
    if head:
        t = Table([head], colWidths=[avail - 2.7 * inch, 2.7 * inch] if len(head) == 2 else [avail])
        t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("ALIGN", (-1, 0), (-1, 0), "RIGHT"),
                               ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
        story += [t, Spacer(1, 6)]
    story += dpp.md_to_story(md_path.read_text(encoding="utf-8"), avail)
    doc = SimpleDocTemplate(str(pdf_path), pagesize=letter, leftMargin=margin, rightMargin=margin,
                            topMargin=margin, bottomMargin=0.55 * inch, title=md_path.stem,
                            author="SiftStack Deep Prospecting v5")

    def _footer(canvas, dd):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(ink2)
        canvas.drawString(margin, 0.35 * inch, "SiftStack deep prospecting v5. SmartSkip, Trestle, county records "
                                               "and published obituaries. Confidential, internal use.")
        canvas.drawRightString(letter[0] - margin, 0.35 * inch, "Page %d" % dd.page)
        canvas.restoreState()

    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return str(pdf_path)


# ------------------------------------------------------------------ workbook --

NAVY, INK, INK2, BLUE, GREEN, GOLD = "0A1130", "141D45", "5B6478", "316AFF", "1B9E5A", "B7791F"
SOFT, LINE, TINT = "F5F7FB", "DDE2EE", "E6EEFF"
TIER_COLOR = {"Dial First": GREEN, "Dial Second": BLUE, "Dial Third": INK, "Dial Fourth": INK2, "Drop": INK2}


class Redactor:
    """Masks every person, number, email, link and house number for audiences outside the team."""

    PHONE = re.compile(r"\(?\b(\d{3})\)?[\s.-]?(\d{3})[\s.-]?(\d{4})\b")
    EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
    URL = re.compile(r"https?://\S+")
    # public-record references that let anyone look the owner up: deed instrument numbers,
    # book and page, probate dockets, Knox parcel ids
    REFS = [
        (re.compile(r"(?<!\d)\d{8,}(?!\d)(?:\s\d{5,})?"), "[instrument]"),
        (re.compile(r"\b(Book|WB)\s+\d+,?\s+(Page|Pg)\s+\d+", re.I), "[book and page]"),
        (re.compile(r"\b(docket|Docket)((?:\s+No\.|\s+number)?\s+)\d{4,6}(?:-\d+)?"), r"\1 [removed]"),
        (re.compile(r"(?<![\w-])0\d{2}[A-Z]{2}-?\d{3,5}(?![\w-])"), "[parcel]"),
        (re.compile(r"(?<!\d)0\d{2}\s{1,2}0?\d{3}(?!\d)"), "[parcel]"),
    ]

    def __init__(self, names: list[str], streets: list[str], tokens: set | None = None):
        pairs = {}
        for t in tokens or ():
            if len(t) >= 3:
                pairs[t] = t[0].upper() + "."
        for n in names:
            toks = [t for t in re.split(r"\s+", (n or "").strip()) if t]
            if len(toks) < 2:
                continue
            first, last = toks[0], toks[-1]
            short = "%s %s." % (first.title(), last[0].upper())
            pairs[n] = short
            pairs["%s %s" % (last, first)] = short
            if len(last) >= 4:
                pairs[last] = last[0].upper() + "."
        self.pairs = sorted(pairs.items(), key=lambda kv: -len(kv[0]))
        self.streets = []
        words = set()
        for s in streets:
            m = re.match(r"^\s*(\d+)\s+(.+)$", s or "")
            if m:
                self.streets.append((s, "%s %s" % ("x" * len(m.group(1)), m.group(2))))
                toks = [t for t in re.findall(r"[A-Za-z]+", m.group(2))
                        if t.upper() not in ("N", "S", "E", "W", "EAST", "WEST", "NORTH", "SOUTH")]
                if toks and len(toks[0]) >= 3:
                    words.add(toks[0].title())
        # "2310 Jefferson", "2411 and 2511 Washington", "2048 E Fifth": a house number is any
        # number standing before a known street name, not only inside a full address string
        self.house = None
        if words:
            alt = "|".join(sorted(words, key=len, reverse=True))
            self.house = re.compile(
                r"(?<![\w.])(\d{1,5})(?=(?:\s+and\s+\d{1,5})?\s+(?:(?:[NSEW]\.?|East|West|North|South)\s+)?"
                r"(?:%s)(?![a-z]))" % alt, re.I)

    def __call__(self, v):
        if not isinstance(v, str) or not v:
            return v
        s = self.URL.sub("[link removed]", v)
        s = self.EMAIL.sub("[email removed]", s)
        s = self.PHONE.sub(lambda m: "(%s) xxx-%s" % (m.group(1), m.group(3)), s)
        for rx, rep in self.REFS:
            s = rx.sub(rep, s)
        for full, short in self.streets:
            s = re.sub(re.escape(full), short, s, flags=re.I)
        if self.house is not None:
            s = self.house.sub(lambda m: "x" * len(m.group(1)), s)
        for full, short in self.pairs:
            s = re.sub(r"\b%s\b" % re.escape(full), short, s, flags=re.I)
        return s


# Words that start with a capital in name-dense text but are not part of a person's name. A token
# is masked only when it FOLLOWS another name word in the same phrase, so a first name survives
# ("Robin W."), while every surname, middle and maiden name goes to an initial.
NAME_STOP = set("""
Son Daughter Wife Husband Father Mother Sister Brother Nephew Niece Uncle Aunt Cousin Step Grandchildren
Children Grandchild Survivors Survived Preceded Owner Owners The His Her She He They Letters Notice
Estate Deceased Living Mr Mrs Ms Dr Rev Jr Sr II III IV Late County Court Chancery Probate Division
Funeral Home Homes Cemetery Memorial Services Cremation Chapel Church Foundation Center University
College Library Public News Sentinel Find Grave Knoxville Tennessee Maryville Street Avenue Ave Road
Rd Pike Lane Ln Drive Dr Court Ct Place Pl Way Boulevard Blvd Bank Federal Credit Union Properties
Investments LLC Inc Trust Revocable Agreement Fixer Upper Mockingbird Zenith Delta Airlines Andy
Honor Air Kidney Tennova Turkey Creek Parkridge Edgewood Park City Historic Zoning Commission Planning
Register Deeds Trustee Assessor Sun Wells Fargo Regions AEB Ombre Dog North South East West Old New
Orleans Jefferson Washington Gill Thorn Grove Louise Fifth Rader Harvey Freels Salem Ridge Mansfield
Tell Farrington Beach Cliff Caton Pearl Nelson Kingston Strawberry Plains Clinch Jonquil Nineteenth
Curtis Broadway Sutherland Lake Bay Marina Asbury Greenwood Eastview Gardens Sunlight Baptist Crossing
Kodak Rocky River Jackson Slidell Duluth Tucker Friendsville Philadelphia Sevierville Dallas Atlanta
Gwinnett Georgia Ohio Louisiana UT TANDEC INDA Knox KGIS MLS Mynatt Book Page Instrument WB Pg Lot
Quitclaim Deed Deeds Listed Pulled Bank January February March April May June July August September
October November December Monday Tuesday Wednesday Thursday Friday Saturday Sunday Lord Online
Condolences Family Friends Tyler Archer
""".split())


def _name_tokens(text: str) -> set:
    """Surname, middle and maiden-name tokens in a stretch of name-dense text.

    Written without word-boundary escapes on purpose: this function once reached the file
    through a shell heredoc that turned each one into a backspace byte, and matched nothing.
    """
    out = set()
    for piece in re.split(r"[,;:()&]| and ", text or ""):
        toks = re.findall(r"[A-Z][A-Za-z'-]+", piece)
        toks = [t[:-2] if t.endswith("'s") else t for t in toks]
        toks = [t for t in toks if t not in NAME_STOP and len(t) > 1]
        out.update(toks[1:])
    return out


STREET_RX = re.compile(
    r"(?<![\w$,])\d{1,5}\s+(?:(?:[NSEW]\.?|East|West|North|South)\s+)?[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2}\s+"
    r"(?:Ave|Avenue|Rd|Road|St|Street|Dr|Drive|Ln|Lane|Pike|Way|Pl|Place|Ct|Court|Blvd|Boulevard|Cir|Circle)(?![a-z])")


def redaction_tokens(data: list[dict]) -> set:
    """Every surname, middle and maiden name in the name-dense fields the workbook renders."""
    tokens = set()
    for d in data:
        an, rs = d["an"], d["research"]
        dense = [p.get("name") or "" for p in (an.get("people") or []) + (rs.get("people") or [])]
        dense += [p["name"] for p in d["people"]]
        for dec in rs.get("decedents") or []:
            dense += [dec.get("name") or "", dec.get("survivors_verbatim") or "", dec.get("predeceased_verbatim") or ""]
        dense += [q.get("text") or "" for q in an.get("survivors_quotes") or []]
        dense += [e.get("finding") or "" for e in an.get("evidence") or []]
        for c in (an.get("title") or {}).get("chain") or []:
            dense += [str(c[1]), str(c[2])]
        dense += [m.get("who") or "" for m in an.get("must_sign") or []]
        for t in dense:
            tokens |= _name_tokens(t)
    return tokens


def _wb_styles():
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    thin = Side(style="thin", color=LINE)
    return {
        "title": Font(name="Calibri", size=16, bold=True, color=NAVY),
        "sub": Font(name="Calibri", size=10, color=INK2),
        "section": Font(name="Calibri", size=12, bold=True, color=NAVY),
        "head": Font(name="Calibri", size=10, bold=True, color=INK),
        "body": Font(name="Calibri", size=10, color=INK),
        "bold": Font(name="Calibri", size=10, bold=True, color=INK),
        "muted": Font(name="Calibri", size=9, color=INK2),
        "link": Font(name="Calibri", size=10, color=BLUE, underline="single"),
        "headline": Font(name="Calibri", size=12, bold=True, color=NAVY),
        "soft": PatternFill("solid", fgColor=SOFT),
        "tint": PatternFill("solid", fgColor=TINT),
        "grid": Border(left=thin, right=thin, top=thin, bottom=thin),
        "headrule": Border(left=thin, right=thin, top=thin, bottom=Side(style="medium", color=INK)),
        "secrule": Border(bottom=Side(style="thin", color=BLUE)),
        "leftbar": Border(left=Side(style="thick", color=BLUE)),
        "wrap": Alignment(wrap_text=True, vertical="top"),
        "center": Alignment(horizontal="center", vertical="top", wrap_text=True),
        "right": Alignment(horizontal="right", vertical="top"),
    }


class Sheet:
    """A cursor over one worksheet with fixed column widths, so wrapped rows can be sized."""

    def __init__(self, ws, widths: list[float], red=None):
        from openpyxl.utils import get_column_letter
        self.ws, self.widths, self.row, self.red = ws, widths, 1, red or (lambda v: v)
        self.redacting = red is not None
        self.st = _wb_styles()
        for i, w in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(i)].width = w
        ws.sheet_view.showGridLines = False
        ws.sheet_view.zoomScale = 100
        ws.page_setup.orientation = "landscape"
        ws.page_setup.fitToWidth = 1
        ws.page_setup.fitToHeight = 0
        ws.sheet_properties.pageSetUpPr.fitToPage = True
        ws.print_options.horizontalCentered = True

    def span_width(self, c1, c2):
        return sum(self.widths[c1 - 1:c2])

    def _fit(self, r, texts_widths, base=13.2):
        lines = 1
        for text, width in texts_widths:
            text = str(text or "")
            per = max(1.0, width * 1.22)
            n = sum(max(1, math.ceil(len(part) / per)) for part in text.split("\n"))
            lines = max(lines, n)
        cur = self.ws.row_dimensions[r].height or 0
        self.ws.row_dimensions[r].height = max(cur, base * lines + 3)

    def put(self, r, c, v, font="body", fill=None, border=None, align="wrap", c2=None, fmt=None, link=None):
        ws = self.ws
        v = self.red(v) if isinstance(v, str) else v
        if isinstance(v, str):
            v = no_dash(v)
        cell = ws.cell(row=r, column=c, value=v)
        cell.font = self.st[font]
        cell.alignment = self.st[align]
        if fill:
            cell.fill = self.st[fill]
        if border:
            cell.border = self.st[border]
        if fmt:
            cell.number_format = fmt
        if link and not self.redacting:
            cell.hyperlink = link
            cell.font = self.st["link"]
        if c2 and c2 > c:
            ws.merge_cells(start_row=r, start_column=c, end_row=r, end_column=c2)
            for cc in range(c + 1, c2 + 1):
                if border:
                    ws.cell(row=r, column=cc).border = self.st[border]
                if fill:
                    ws.cell(row=r, column=cc).fill = self.st[fill]
        return cell

    def title(self, text, sub=""):
        self.put(self.row, 1, text, font="title", c2=len(self.widths), align="wrap")
        self.ws.row_dimensions[self.row].height = 24
        self.row += 1
        if sub:
            self.put(self.row, 1, sub, font="sub", c2=len(self.widths))
            self._fit(self.row, [(sub, self.span_width(1, len(self.widths)))], base=13.5)
            self.row += 1
        self.row += 1

    def section(self, text, c2=None):
        c2 = c2 or len(self.widths)
        self.put(self.row, 1, text, font="section", border="secrule", c2=c2, align="wrap")
        self.ws.row_dimensions[self.row].height = 20
        self.row += 1

    def para(self, text, font="body", c1=1, c2=None, fill=None, border=None):
        c2 = c2 or len(self.widths)
        self.put(self.row, c1, text, font=font, c2=c2, fill=fill, border=border)
        self._fit(self.row, [(self.red(text), self.span_width(c1, c2))], base=13.2 if font != "headline" else 16)
        self.row += 1

    def kv(self, pairs, spans=((1, 1, 2, 2),)):
        """pairs laid out as label/value; each span is (label_col, label_end, value_col, value_end)."""
        for i in range(0, len(pairs), len(spans)):
            chunk = pairs[i:i + len(spans)]
            fit = []
            for (lab, val), (lc, le, vc, ve) in zip(chunk, spans):
                self.put(self.row, lc, lab, font="bold", fill="soft", border="grid", c2=le)
                self.put(self.row, vc, val, font="body", border="grid", c2=ve,
                         fmt='"$"#,##0' if isinstance(val, (int, float)) and not isinstance(val, bool) else None)
                fit += [(lab, self.span_width(lc, le)), (val if isinstance(val, str) else "", self.span_width(vc, ve))]
            self._fit(self.row, fit)
            self.row += 1

    def table(self, headers, rows, spans=None, colors_by=None, fmts=None, links=None, freeze=False, filt=False):
        spans = spans or [(i, i) for i in range(1, len(headers) + 1)]
        hr = self.row
        for h, (c1, c2) in zip(headers, spans):
            self.put(hr, c1, h, font="head", fill="soft", border="headrule", c2=c2)
        self._fit(hr, [(h, self.span_width(c1, c2)) for h, (c1, c2) in zip(headers, spans)], base=14)
        self.row += 1
        for ri, r in enumerate(rows):
            fit = []
            for ci, (v, (c1, c2)) in enumerate(zip(r, spans)):
                fmt = (fmts or {}).get(ci)
                lk = (links or {}).get((ri, ci))
                cell = self.put(self.row, c1, v, border="grid", c2=c2, fmt=fmt, link=lk)
                if colors_by and ci in colors_by:
                    col = colors_by[ci](v)
                    if col:
                        from openpyxl.styles import Font
                        cell.font = Font(name="Calibri", size=10, bold=col in (GREEN,), color=col)
                fit.append((v if isinstance(v, str) else str(v if v is not None else ""), self.span_width(c1, c2)))
            self._fit(self.row, fit)
            self.row += 1
        if freeze:
            self.ws.freeze_panes = self.ws.cell(row=hr + 1, column=1)
        if filt and rows:
            from openpyxl.utils import get_column_letter
            self.ws.auto_filter.ref = "A%d:%s%d" % (hr, get_column_letter(spans[-1][1]), self.row - 1)
        return hr

    def gap(self, n=1):
        self.row += n


def _tier_color(v):
    return TIER_COLOR.get(v) if isinstance(v, str) else None


def _living_color(v):
    s = (v or "").lower() if isinstance(v, str) else ""
    if s.startswith("living"):
        return GREEN
    if s.startswith("deceased"):
        return INK2
    if "verify" in s or "unknown" in s or "likely" in s:
        return GOLD
    return None


def build_workbook(data: list[dict], path: Path, *, redact: bool, audit: dict, ledger: dict, run: Run) -> Path:
    from openpyxl import Workbook
    from openpyxl.drawing.image import Image as XLImage
    names, streets = set(), set()
    for d in data:
        row = d["x"]["row"]
        streets.add(row["street"])
        streets.add(row.get("mail_street") or "")
        if not row.get("entity"):
            names.add(row["owner"])
        # county strings run LAST FIRST ("HASSENBOEHLER CHARLES B JR"): feed only the surname
        for s in (d["x"].get("title_owner") or "", row["owner"] if row.get("entity") else ""):
            toks = re.sub(r"[^A-Za-z ]", " ", s).split()
            if toks and len(toks[0]) >= 4:
                names.add("X %s" % toks[0].title())
        for p in d["people"]:
            names.add(p["name"])
            if p.get("street"):
                streets.add(p["street"])
        for p in d["an"].get("people") or []:
            names.add(p.get("name") or "")
        for p in d["research"].get("people") or []:
            names.add(p.get("name") or "")
        for dec in d["research"].get("decedents") or []:
            names.add(dec.get("name") or "")
        for pr in (d["x"]["siftmap"].get("owner_info") or {}).get("owner_properties") or []:
            streets.add((pr.get("address") or "").split(",")[0])
        for pr in (d["x"].get("portfolio") or {}).get("parcels") or []:
            streets.add(title_name(pr.get("parcel_address") or ""))
    if redact:
        for d in data:
            blob = json.dumps(d["an"]) + json.dumps(d["research"])
            for m in STREET_RX.finditer(blob):
                streets.add(m.group(0))
    tokens = redaction_tokens(data) if redact else set()
    red = Redactor(sorted(n for n in names if n), sorted(s for s in streets if s), tokens) if redact else None
    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    S = Sheet(ws, [5, 20, 18, 42, 28, 13, 11, 26], red)
    logo = Path(r"C:\Users\Tyrus\OneDrive\Desktop\datasift-design-system\assets\datasift-logo-dark.png")
    if logo.exists():
        img = XLImage(str(logo))
        img.width, img.height = 150, int(150 * 179 / 506)
        ws.add_image(img, "A1")
        ws.row_dimensions[1].height = 30
        ws.row_dimensions[2].height = 14
        S.row = 3
    S.title("Dream 100 top 5: deep prospecting results",
            "The five largest opportunities in the D4D top 3,000, researched end to end with deep prospecting v5: "
            "SmartSkip relatives and phones, Trestle dial tiers, county title and tax records, SiftMap deed history, "
            "published obituaries and the call history already on each record. Built %s. %s" % (
                date.today().strftime("%B %d, %Y").replace(" 0", " "),
                "Redacted copy for audiences outside the team: names shortened, numbers and links masked." if redact
                else "Internal copy with full contact data."))
    rows = []
    for d in data:
        an, row = d["an"], d["x"]["row"]
        live = [r for r in d["dial"] if not r["do_not_dial"]]
        new = [r for r in live if not r["on_crm"] and r["tier"] in WRITE_TIERS]
        people_n = len([p for p in an.get("people") or [] if p.get("role") not in (None, "", "none")])
        rows.append([d["x"]["rank"], "%s, %s" % (row["street"], row["zip"]), row["owner"],
                     an.get("headline", ""), an.get("first_call", ""),
                     "%d people, %d new numbers" % (people_n, len(new)),
                     row.get("deal_size"), an.get("next_step", "")])
    S.table(["Rank", "Property", "Owner of record", "What the research found", "Who to call first",
             "Reach", "Deal size", "Next step"], rows, fmts={6: '"$"#,##0'}, freeze=True)
    S.gap()
    S.section("How to read this workbook")
    for line in [
        "One tab per property carries the full research: title, owner status with evidence, family, who must sign, "
        "the dial sheet, the call history already on the record, and a personalized outreach plan.",
        "Dial tiers come from Trestle activity scores: Dial First 81 to 100, Dial Second 61 to 80, Dial Third 41 to 60, "
        "Dial Fourth 21 to 40, Drop 0 to 20. Numbers a caller already marked wrong or dead are listed as do not dial.",
        "Relationships are labelled with their source. SmartSkip labels are a starting point; an obituary or county "
        "record overrides them. A death is stated only when a published obituary confirms it.",
        "Deal size is discount room (the smaller of equity and 30 percent of value), not profit. Values are automated "
        "estimates; comp before any offer."]:
        S.para(line)
    S.gap()
    S.section("What it cost")
    labels = {"smartskip": "SmartSkip relatives and phones", "trestle": "Trestle phone scoring",
              "tracerfy": "Tracerfy gap-fill", "streetview": "Google Street View photos",
              "transcription": "Call recording transcription", "enformion": "Enformion BusinessV2"}
    cost_rows = [[labels.get(k, k), v.get("note", ""), v.get("usd")] for k, v in sorted(ledger.items())]
    total = sum(v.get("usd") or 0 for v in ledger.values())
    cost_rows.append(["Total", "All paid lookups for the five properties", round(total, 2)])
    S.table(["Step", "Detail", "Cost"], cost_rows, spans=[(1, 2), (3, 7), (8, 8)], fmts={2: '"$"#,##0.00'})

    for d in data:
        _property_sheet(wb, d, red, run)
    _master_dial(wb, data, red, audit)
    _people_sheet(wb, data, red)
    _sources_sheet(wb, data, red, audit, ledger)
    tmp = path.with_name("_PENDING_" + path.name)
    wb.save(tmp)
    tmp.replace(path)
    return path


def _short(street: str) -> str:
    """'5619 Sample Grove Pike' -> 'Sample Grove': the tab name carries no house number."""
    toks = street.split()
    return " ".join(toks[1:-1] if len(toks) > 2 else toks[1:]) or street


ROLE_LABEL = {"owner": "owner", "decision maker": "decision maker", "signer": "signer",
              "channel": "family channel", "attorney": "estate attorney"}


def _status_role(p: dict) -> str:
    role = ROLE_LABEL.get(p.get("role") or "")
    return "%s, %s" % (p.get("living", ""), role) if role else p.get("living", "")


def _property_sheet(wb, d, red, run: Run):
    from openpyxl.drawing.image import Image as XLImage
    x, an, rs, row = d["x"], d["an"], d["research"], d["x"]["row"]
    ws = wb.create_sheet("%d %s" % (x["rank"], _short(row["street"])[:26]))
    S = Sheet(ws, [16, 26, 14, 22, 22, 46], red)
    S.title("%d. %s, Knoxville, TN %s" % (x["rank"], row["street"], row["zip"]),
            "Owner of record: %s. County title reads: %s. Dream 100 rank %d. %s" % (
                row["owner"], x.get("title_owner") or "not found", x["rank"],
                "Assigned to %s in the CRM." % an["assignee"] if an.get("assignee") else "Unassigned in the CRM."))
    S.put(S.row, 1, an.get("headline", ""), font="headline", fill="tint", border="leftbar", c2=6)
    S._fit(S.row, [(an.get("headline", ""), S.span_width(1, 6) * 0.85)], base=17)
    S.row += 1
    for p in an.get("headline_points") or []:
        S.put(S.row, 1, "- " + p, font="body", fill="tint", border="leftbar", c2=6)
        S._fit(S.row, [(p, S.span_width(1, 6))], base=14.5)
        S.row += 1
    S.gap()
    top = S.row
    photo = run.p("img", d["slug"] + ".jpg")
    pairs = [("Owner status", an.get("owner_status", "")), ("Decision maker", an.get("decision_maker", "")),
             ("Best first call", an.get("first_call", "")), ("Deal size", row.get("deal_size")),
             ("Value (AVM)", row.get("value")), ("Equity", "%s%%, %s" % (round(row.get("equity_pct") or 0),
                                                                        an.get("debt", "no mortgage of record"))),
             ("Condition", "D4D visual distress %s of 100. %s" % (row.get("d4d_score"), an.get("condition", ""))),
             ("House", "%s bed, %s bath, %s sqft, built %s" % (row.get("beds"), row.get("baths"),
                                                              format(int(row.get("sqft") or 0), ","), row.get("year"))),
             ("Taxes", an.get("taxes", "")), ("Held", an.get("tenure", "")),
             ("Prior touches", an.get("touches", "")), ("Lists", ", ".join(l for l in row.get("lists") or []
                                                                         if l != "D4D Top 3000"))]
    S.kv(pairs, spans=((1, 1, 2, 4),))
    if photo.exists() and not red:
        img = XLImage(str(photo))
        img.width, img.height = 330, 206
        ws.add_image(img, "E%d" % top)
        S.put(top + 11, 5, "Street View, Google. Vendor condition scores: abandoned %s, overgrown %s, roof %s, windows %s." % (
            x["vendor"].get("abandoned_score"), x["vendor"].get("overgrown_score"), x["vendor"].get("bad_roof_score"),
            x["vendor"].get("damaged_windows_score")), font="muted", c2=6)
        S._fit(top + 11, [("x" * 120, S.span_width(5, 6))], base=12)
    S.gap()
    S.section("Title and ownership")
    t = an.get("title") or {}
    S.para("Vesting: " + t.get("vesting", ""), font="bold")
    if t.get("chain"):
        S.table(["Date", "From", "To", "Price", "Note"], [[c[0], c[1], c[2], c[3], c[4] if len(c) > 4 else ""]
                                                          for c in t["chain"]],
                spans=[(1, 1), (2, 2), (3, 4), (5, 5), (6, 6)])
    for f in t.get("flags") or []:
        S.para("- " + f)
    S.gap()
    S.section("Owner status and evidence")
    S.para("%s. %s" % (an.get("owner_status", ""), an.get("owner_status_note", "")), font="bold")
    ev = an.get("evidence") or []
    if ev:
        links = {(i, 1): e.get("url") for i, e in enumerate(ev) if e.get("url")}
        S.table(["Finding", "Source"], [[e.get("finding", ""), e.get("source", "")] for e in ev],
                spans=[(1, 4), (5, 6)], links=links)
    for q in survivors_quotes(d):
        S.para("Survivors paragraph from the obituary of %s, verbatim:" % q["of"], font="bold")
        S.para(q["text"], fill="soft")
    S.gap()
    S.section("Family and decision makers")
    fam = []
    for p in an.get("people") or []:
        best = next((r for r in d["dial"] if not r["do_not_dial"] and any(
            z["name"] == p.get("name") for z in r["reaches"])), None)
        fam.append([p.get("name", ""), p.get("relationship", ""), p.get("age") or "", p.get("location", ""),
                    _status_role(p),
                    ("%s, %s %s" % (fmt_phone(best["number"]), best["tier"], best["type"].lower())) if best
                    else ("Deceased, do not dial" if (p.get("living") or "").startswith("Deceased")
                          else (p.get("reach_note") or "no number found"))])
    S.table(["Person", "Relationship and source", "Age", "Lives in", "Status and role", "Best number"], fam,
            colors_by={4: _living_color})
    S.gap()
    S.section("Who must sign to sell")
    S.table(["Who", "Status", "Why"], [[m.get("who", ""), m.get("status", ""), m.get("why", "")]
                                       for m in an.get("must_sign") or []],
            spans=[(1, 2), (3, 3), (4, 6)], colors_by={1: lambda v: GREEN if v == "Required" else GOLD})
    for f in an.get("signing_flags") or []:
        S.para("- " + f)
    S.gap()
    S.section("Dial sheet (best number first)")
    drows = []
    for r in d["dial"]:
        tier = "Do not dial" if r["do_not_dial"] else r["tier"]
        note = r["note"] or ("Caller marked %s" % r["crm_status"].lower() if (r.get("crm_status") or "") in BAD_STATUSES
                             else ("New from %s" % ", ".join(s for s in r["sources"] if s != "CRM") if not r["on_crm"]
                                   else "Already on the CRM"))
        drows.append([fmt_phone(r["number"]), "%s%s" % (tier, (" (%s)" % r["score"]) if r["score"] is not None else ""),
                      r["type"].title(), r["reaches_label"], ", ".join(r["sources"]), note])
    S.table(["Number", "Tier (score)", "Type", "Reaches", "Source", "Note"], drows,
            colors_by={1: lambda v: _tier_color(v.split(" (")[0]) if isinstance(v, str) else None})
    tl = timeline_rows(x)
    if tl:
        S.gap()
        S.section("Contact history already on the record")
        S.table(["Date", "Channel", "By", "Number", "What happened"], tl, spans=[(1, 1), (2, 2), (3, 3), (4, 4), (5, 6)])
    o = an.get("outreach") or {}
    S.gap()
    S.section("Outreach plan")
    plan = [("Who to call first", o.get("who_first", "")), ("Angle", o.get("angle", "")),
            ("Opening line", o.get("opener", ""))]
    plan += [("Talking point", tp) for tp in o.get("talking_points") or []]
    plan += [("Text %d (%s)" % (i, o.get("sender", "")), tx) for i, tx in enumerate(o.get("texts") or [], 1)]
    if not o.get("texts") and o.get("texts_note"):
        plan.append(("Texts", o["texts_note"]))
    if o.get("letter"):
        plan.append(("Letter angle", o["letter"]))
    plan += [("Do not", a) for a in o.get("avoid") or []]
    S.kv(plan, spans=((1, 1, 2, 6),))
    if an.get("corrections") or an.get("open_items"):
        S.gap()
        S.section("Record corrections and open items")
        S.kv([("Correction", c) for c in an.get("corrections") or []] +
             [("Open item", c) for c in an.get("open_items") or []], spans=((1, 1, 2, 6),))
    ws.freeze_panes = None


def _master_dial(wb, data, red, audit):
    ws = wb.create_sheet("Master dial sheet")
    S = Sheet(ws, [16, 15, 11, 7, 10, 24, 14, 12, 12, 10, 28], red)
    S.title("Master dial sheet", "Every number for the five properties, best first. Numbers a caller already "
                                 "marked wrong or dead are last and flagged do not dial.")
    rows = []
    for d in data:
        wrote = {p["number"] for p in (audit.get(d["slug"]) or {}).get("phones_written") or []}
        for r in d["dial"]:
            rel = "; ".join(sorted({z["relationship"] for z in r["reaches"] if z["relationship"]}))
            rows.append([d["x"]["row"]["street"], fmt_phone(r["number"]), "Do not dial" if r["do_not_dial"] else r["tier"],
                         r["score"] if r["score"] is not None else "", r["type"].title(), r["reaches_label"], rel,
                         ", ".join(r["sources"]),
                         (r.get("crm_status") or "").replace("_", " ").capitalize() if r["on_crm"] else "",
                         "Yes" if r["number"] in wrote else "", r["note"]])
    S.table(["Property", "Number", "Tier", "Score", "Type", "Reaches", "Relationship", "Source", "CRM status",
             "Added now", "Note"], rows, colors_by={2: _tier_color}, freeze=True, filt=True)


def _people_sheet(wb, data, red):
    ws = wb.create_sheet("People")
    S = Sheet(ws, [16, 22, 18, 22, 6, 20, 12, 14, 9, 22], red)
    S.title("People", "Everyone the research tied to the five properties: owners, family and anyone who can "
                      "reach a decision maker. Relationship source shows how each tie was established.")
    rows = []
    for d in data:
        for p in d["an"].get("people") or []:
            nums = [r for r in d["dial"] if any(z["name"] == p.get("name") for z in r["reaches"]) and not r["do_not_dial"]]
            best = nums[0]["tier"] if nums else ""
            rows.append([d["x"]["row"]["street"], p.get("name", ""), p.get("relationship", ""), p.get("verified_by", ""),
                         p.get("age") or "", p.get("location", ""), p.get("living", ""),
                         (ROLE_LABEL.get(p.get("role") or "") or "not a contact").capitalize(),
                         len(nums), best])
    S.table(["Property", "Person", "Relationship", "Verified by", "Age", "Lives in", "Living", "Role", "Numbers",
             "Best tier"], rows, colors_by={6: _living_color, 9: _tier_color}, freeze=True, filt=True)


def _sources_sheet(wb, data, red, audit, ledger):
    ws = wb.create_sheet("Sources and audit")
    S = Sheet(ws, [18, 58, 64], red)
    S.title("Sources and audit", "Every source behind a finding, what the run cost, and what was written back "
                                 "to the CRM, with the read-back result.")
    S.section("Sources")
    rows, links = [], {}
    for d in data:
        for s in d["an"].get("sources") or []:
            if s.get("url"):
                links[(len(rows), 2)] = s["url"]
            rows.append([d["x"]["row"]["street"], s.get("what", ""), s.get("url", "")])
    S.table(["Property", "What it shows", "Source"], rows, links=links)
    S.gap()
    S.section("CRM write-back")
    arows = []
    for d in data:
        a = audit.get(d["slug"]) or {}
        arows.append([d["x"]["row"]["street"], a.get("summary", "not written yet"), a.get("readback", "")])
    S.table(["Property", "What was written", "Read-back"], arows)


# --------------------------------------------------------------------- build --

def phase_build(args, run: Run):
    data = assemble(run)
    missing = [d["slug"] for d in data if not d["an"]]
    gate(not missing, "analysis missing for: %s" % ", ".join(missing))
    audit = run.load("writeback_audit.json", {}) or {}
    ledger = run.load("ledger.json", {}) or {}
    for d in data:
        md = run.p("packs", d["slug"] + ".md")
        md.write_text(pack_md(d), encoding="utf-8")
        pdf = render_pdf(md, md.with_suffix(".pdf"), photo=run.p("img", d["slug"] + ".jpg"))
        print("  pack %s" % Path(pdf).relative_to(ROOT))
    stamp = date.today().strftime("%Y%m%d")
    full = build_workbook(data, run.p("Dream100_Top5_DeepProspect_%s.xlsx" % stamp), redact=False,
                          audit=audit, ledger=ledger, run=run)
    demo = build_workbook(data, run.p("Dream100_Top5_DeepProspect_DEMO_redacted_%s.xlsx" % stamp), redact=True,
                          audit=audit, ledger=ledger, run=run)
    print("  workbook %s\n  workbook %s" % (full.relative_to(ROOT), demo.relative_to(ROOT)))


# ----------------------------------------------------------------- writeback --

def _readback(api, d: dict, plan: dict) -> dict:
    uuid = d["x"]["uuid"]
    rec = api.call("/api/internal/property/%s/" % uuid)
    have = {digits(p.get("number")): [t if isinstance(t, str) else (t.get("title") or t.get("name"))
                                      for t in (p.get("tags") or [])]
            for p in ((rec.get("owner") or {}).get("phones") or [])}
    phones_ok = all(digits(p["number"]) in have and set(p["tags"]) <= set(have[digits(p["number"])])
                    for p in plan["phones"])
    tags = [t if isinstance(t, str) else (t.get("title") or t.get("name")) for t in rec.get("tags") or []]
    msgs = api.call("/api/internal/property/%s/message/?limit=50" % uuid)
    msgs = msgs.get("results") if isinstance(msgs, dict) else msgs
    note = next((m for m in msgs or [] if MARKER in str(m.get("message") or "")), None)
    return {"phones_ok": phones_ok, "tag_ok": PROPERTY_TAG in tags, "note_ok": bool(note),
            "note_pinned": bool((note or {}).get("pinned")),
            "owner_deceased": bool((rec.get("owner") or {}).get("deceased"))}


def phase_writeback(args, run: Run):
    """New phones, one pinned research note, the property tag, confirmed-death fields. Read back."""
    from datasift_api_upload import Api, attach_to_record, field_index  # noqa: E402
    from ftm_deep_prospect_push import upsert_phones  # noqa: E402  (the index-drop retry)
    data = assemble(run)
    if args.only:
        data = [d for d in data if d["slug"] in args.only.split(",")]
    gate(bool(data) and all(d["an"] for d in data), "analysis files missing")
    audit = run.load("writeback_audit.json", {}) or {}
    api = Api() if args.commit else None
    idx = field_index(api) if api else {}
    for d in data:
        an, x = d["an"], d["x"]
        plan = writeback_plan(d)
        note = build_note(d)
        fields = dict(an.get("crm_fields") or {})
        print("\n%s  owner %s" % (x["row"]["street"], (plan["owner_uuid"] or "")[:8]))
        for p in plan["phones"]:
            print("  + %s %-8s %-40s -> %s" % (fmt_phone(p["number"]), p["type"], ", ".join(p["tags"]), p["_reaches"]))
        for k, v in fields.items():
            print("  field %s = %s" % (k, v))
        if (an.get("death") or {}).get("owner_deceased"):
            print("  owner deceased flag -> true (obituary: %s)" % an["death"].get("obituary_url"))
        print("  note: %d lines, tag %s" % (note.count("\n") + 1, PROPERTY_TAG))
        if not args.commit:
            continue
        uuid = x["uuid"]
        entry = {"phones_written": [], "fields": {}, "errors": []}
        if plan["phones"]:
            tags = {p["number"]: p["tags"] for p in plan["phones"]}
            # upsert_phones builds items with type UNKNOWN; send full objects instead, same retry rule
            items = [{k: v for k, v in p.items() if not k.startswith("_")} for p in plan["phones"]]
            ok, dropped = _upsert_full(api, plan["owner_uuid"], items)
            if not ok:
                ok2, dropped2 = upsert_phones(api, plan["owner_uuid"], tags)
                ok, dropped = ok2, dropped + dropped2
            entry["phones_written"] = plan["phones"] if ok else []
            entry["phones_dropped"] = dropped
            if not ok:
                entry["errors"].append("upsert-phones failed")
        try:
            msgs = api.call("/api/internal/property/%s/message/?limit=50" % uuid)
            msgs = msgs.get("results") if isinstance(msgs, dict) else msgs
            prior = next((m for m in msgs or [] if MARKER in str(m.get("message") or "")), None)
            if prior:
                api.call("/api/internal/property/%s/message/%s/" % (uuid, prior["uuid"]), "PATCH", {"message": note})
                mu = prior["uuid"]
            else:
                mu = (api.call("/api/internal/property/%s/message/" % uuid, "POST", {"message": note}) or {}).get("uuid")
            if mu:
                try:
                    api.call("/api/internal/property/%s/message/%s/pin/" % (uuid, mu), "POST")
                except Exception:  # noqa: BLE001
                    pass
        except Exception as exc:  # noqa: BLE001
            entry["errors"].append("note: %s" % str(exc)[:120])
        try:
            attach_to_record(api, uuid, {"tags": [PROPERTY_TAG]})
        except Exception as exc:  # noqa: BLE001
            entry["errors"].append("tag: %s" % str(exc)[:120])
        pairs = []
        for label, val in fields.items():
            f = idx.get(label)
            if not f:
                entry["errors"].append("field %s does not exist in the account, skipped" % label)
                continue
            if val in (None, ""):
                continue
            if f["field_type"] in ("select", "multiselect"):
                v = next((u for k, u in f["options"].items() if (k or "").lower() == str(val).lower()), None)
                if not v:
                    entry["errors"].append("field %s has no option %r, skipped" % (label, val))
                    continue
            else:
                v = val
            pairs.append({"field_uuid": f["uuid"], "value": v})
            entry["fields"][label] = val
        if pairs:
            try:
                api.call("/api/internal/property/%s/custom-field/update-values/" % uuid, "PATCH", pairs)
            except Exception as exc:  # noqa: BLE001
                # one refused value sinks the whole batch, so fall back to one field at a time
                entry["errors"].append("fields batch refused, retried singly: %s" % str(exc)[:120])
                for pr in pairs:
                    label = next((k for k, f in idx.items() if f["uuid"] == pr["field_uuid"]), pr["field_uuid"])
                    try:
                        api.call("/api/internal/property/%s/custom-field/update-values/" % uuid, "PATCH", [pr])
                    except Exception as exc2:  # noqa: BLE001
                        entry["fields"].pop(label, None)
                        entry["errors"].append("field %s refused: %s" % (label, str(exc2)[:100]))
        if (an.get("death") or {}).get("owner_deceased"):
            try:
                api.call("/api/internal/owner/%s/" % plan["owner_uuid"], "PATCH", {"deceased": True})
            except Exception as exc:  # noqa: BLE001
                entry["errors"].append("owner deceased flag: %s" % str(exc)[:120])
        time.sleep(1.0)
        rb = _readback(api, d, plan)
        entry["readback"] = ", ".join("%s %s" % (k.replace("_", " "), "yes" if v else "no") for k, v in rb.items())
        k = len(entry["phones_written"])
        entry["summary"] = "%s, pinned research note, tag %s%s%s" % (
            "no new numbers" if not k else "%d new number%s" % (k, "" if k == 1 else "s"), PROPERTY_TAG,
            ", fields " + ", ".join(entry["fields"]) if entry["fields"] else "",
            ", owner marked deceased" if (an.get("death") or {}).get("owner_deceased") else "")
        entry["rb"] = rb
        audit[d["slug"]] = entry
        run.save("writeback_audit.json", audit)
        print("  written. read-back: %s%s" % (entry["readback"], ("  ERRORS: %s" % entry["errors"]) if entry["errors"] else ""))
    if not args.commit:
        print("\nDRY RUN. Nothing written. Re-run with --commit.")


def _upsert_full(api, owner_uuid: str, items: list, max_retries=6):
    """upsert-phones with the real line type; drops a refused number by index and retries."""
    dropped = 0
    for _ in range(max_retries):
        if not items:
            return True, dropped
        try:
            api.call("/api/internal/owner/%s/upsert-phones/" % owner_uuid, "POST", {"phones": items})
            return True, dropped
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            idxs = sorted({int(m) for m in re.findall(r'"phones":\s*\{\s*"(\d+)"', msg)}
                          | {int(m) for m in re.findall(r"'phones':\s*\{\s*'(\d+)'", msg)})
            if not idxs:
                print("      upsert failed: %s" % msg[:140])
                return False, dropped
            for i in reversed(idxs):
                if 0 <= i < len(items):
                    items.pop(i)
                    dropped += 1
    return False, dropped


# ------------------------------------------------------------------------ qa --

def phase_qa(args, run: Run):
    from openpyxl import load_workbook
    problems, checks = [], 0
    data = assemble(run)
    for d in data:
        md = run.p("packs", d["slug"] + ".md")
        checks += 1
        if not md.exists() or not md.with_suffix(".pdf").exists():
            problems.append("%s: pack md or pdf missing" % d["slug"])
        elif re.search("[\u2013\u2014]", md.read_text(encoding="utf-8")):
            problems.append("%s: dash in pack" % d["slug"])
        checks += 1
        if re.search("[\u2013\u2014]", build_note(d)):
            problems.append("%s: dash in CRM note" % d["slug"])
        for p in d["an"].get("people") or []:
            checks += 1
            if p.get("role") not in (None, "", "none") and not (p.get("verified_by") or "").strip():
                problems.append("%s: %s has a role but no relationship source" % (d["slug"], p.get("name")))
        try:
            from sms_agent.respond import validate  # noqa: E402
            for tx in (d["an"].get("outreach") or {}).get("texts") or []:
                checks += 1
                ok, why = validate(tx, max_questions=2, program="seller")
                if not ok:
                    problems.append("%s: text draft blocked (%s): %s" % (d["slug"], "; ".join(why), tx[:60]))
        except ImportError as exc:
            problems.append("sms validator not importable: %s" % exc)
    stamp = date.today().strftime("%Y%m%d")
    full = run.p("Dream100_Top5_DeepProspect_%s.xlsx" % stamp)
    demo = run.p("Dream100_Top5_DeepProspect_DEMO_redacted_%s.xlsx" % stamp)
    names, surnames = set(), {t.lower() for t in redaction_tokens(data) if len(t) >= 4}
    for d in data:
        for p in d["an"].get("people") or []:
            if len((p.get("name") or "").split()) >= 2:
                names.add(p["name"].lower())
                if len(p["name"].split()[-1]) >= 4:
                    surnames.add(p["name"].split()[-1].lower())
    for path, redacted in ((full, False), (demo, True)):
        checks += 1
        if not path.exists():
            problems.append("%s missing" % path.name)
            continue
        wb = load_workbook(path)
        for ws in wb.worksheets:
            for rowc in ws.iter_rows():
                for c in rowc:
                    v = c.value
                    if not isinstance(v, str):
                        continue
                    if re.search("[\u2013\u2014]", v):
                        problems.append("%s/%s!%s: dash" % (path.name, ws.title, c.coordinate))
                    if redacted:
                        low = v.lower()
                        if re.search(r"\b\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}\b", v):
                            problems.append("%s!%s: unmasked phone" % (ws.title, c.coordinate))
                        if "@" in v and re.search(r"[\w.+-]+@[\w-]+\.\w+", v):
                            problems.append("%s!%s: email" % (ws.title, c.coordinate))
                        for n in names:
                            if n in low:
                                problems.append("%s!%s: full name %s" % (ws.title, c.coordinate, n))
                        for sn in surnames:
                            if re.search(r"\b%s\b" % re.escape(sn), low):
                                problems.append("%s!%s: surname %s" % (ws.title, c.coordinate, sn))
        checks += 1
        if redacted and any(ws._images for ws in wb.worksheets if ws.title != "Summary"):
            problems.append("redacted workbook carries a house photo")
    audit = run.load("writeback_audit.json", {}) or {}
    for d in data:
        a = audit.get(d["slug"])
        checks += 1
        if not a:
            problems.append("%s: not written back yet" % d["slug"])
        elif not all((a.get("rb") or {}).get(k) for k in ("phones_ok", "tag_ok", "note_ok")):
            problems.append("%s: read-back incomplete: %s" % (d["slug"], a.get("readback")))
    led = run.load("ledger.json", {}) or {}
    print("cost ledger: %s  total $%.2f" % (", ".join("%s $%.2f" % (k, v["usd"]) for k, v in sorted(led.items())),
                                             sum(v["usd"] for v in led.values())))
    print("qa: %d checks, %d problems" % (checks, len(problems)))
    for p in problems[:60]:
        print("  " + p)
    if problems:
        raise SystemExit(1)


# --------------------------------------------------------------------- main --

PHASES = {
    "records": phase_records,
    "smartskip": phase_smartskip,
    "merge": phase_merge,
    "gapfill": phase_gapfill,
    "score": phase_score,
    "build": phase_build,
    "writeback": phase_writeback,
    "qa": phase_qa,
}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--phase", required=True, choices=sorted(PHASES))
    ap.add_argument("--top", type=int, default=5)
    ap.add_argument("--run", default="output/deep_prospect/dream100_top5_20260923")
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--only", default="", help="writeback: comma-separated slugs")
    args = ap.parse_args(argv)
    PHASES[args.phase](args, Run(args.run))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

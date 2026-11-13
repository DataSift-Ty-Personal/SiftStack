"""Skip trace sources and the phone scorer the autopilot calls.

A source takes subjects (one per record: who to look for, and where) and returns, per record, the
numbers it found. It never writes to the CRM. The stage does that after scoring, so a number lands
on the record once, already tiered.

Two hard lessons are built in:
  * Never search a person by name plus city. A common name returns whoever ranks first and a later
    check confirms the number belongs to A person of that name. Enformion here always sends the
    full street line and keeps the match only when that street is in the person's address history.
  * A number is bought once. Every score is cached by number in the ledger, so the same phone on a
    second record (a spouse, a portfolio owner) costs nothing.

The order the sources run in is the playbook's (`sources:` on a retrace stage), so swapping the
third pass (decision D10: DirectSkip in place of Tracerfy, once an account and a key exist) is a
playbook edit plus one new Source class here, registered in default_sources() and in
policy.SOURCES. No DirectSkip client is written yet: its API contract has not been read, and a
client written from a guess is how a batch gets billed and its results lost.

A source is only ever handed a COMPLETE subject (missing_for_trace): a person with a first and a
last name and a mailing address. A record without those is fixed up by a person first, because a
trace on half a name bills for a stranger's numbers.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

SRC = Path(__file__).resolve().parents[2]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

log = logging.getLogger(__name__)

ORG = re.compile(r"\b(LLC|L\s*L\s*C|INC|CORP|CORPORATION|COMPANY|TRUST|TRUSTEE|TRUSTEES|BANK|LP|LLP|LTD|"
                 r"PARTNERS|PARTNERSHIP|HOLDINGS|PROPERTIES|INVESTMENTS|CHURCH|MINISTRIES|ASSOCIATION|"
                 r"CITY OF|COUNTY|AUTHORITY|FOUNDATION|HOMES|REALTY|GROUP|VENTURES|ENTERPRISES)\b", re.I)
TRACERFY_PHONE_FIELDS = ["primary_phone", "mobile_1", "mobile_2", "mobile_3", "mobile_4", "mobile_5",
                         "landline_1", "landline_2", "landline_3"]
TRACERFY_TRACE_URL = "https://tracerfy.com/v1/api/trace/"
TRACERFY_QUEUE_URL = "https://tracerfy.com/v1/api/queue/"
ENFORMION_URL = "https://devapi.enformion.com/PersonSearch"


def digits(n) -> str:
    d = re.sub(r"\D", "", str(n or ""))
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    return d if len(d) == 10 else ""


def _norm_street(s: str) -> str:
    s = re.sub(r"[^A-Z0-9 ]", " ", str(s or "").upper())
    rep = {"STREET": "ST", "AVENUE": "AVE", "ROAD": "RD", "DRIVE": "DR", "LANE": "LN", "COURT": "CT",
           "CIRCLE": "CIR", "BOULEVARD": "BLVD", "PLACE": "PL", "TERRACE": "TER", "PIKE": "PIKE",
           "HIGHWAY": "HWY", "PARKWAY": "PKWY", "NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W"}
    return " ".join(rep.get(t, t) for t in s.split())


def same_street(a: str, b: str) -> bool:
    """House number equal and the street name tokens equal, after common abbreviations."""
    ta, tb = _norm_street(a).split(), _norm_street(b).split()
    if not ta or not tb or ta[0] != tb[0] or not ta[0].isdigit():
        return False
    na = [t for t in ta[1:] if t not in ("APT", "UNIT", "STE")][:3]
    nb = [t for t in tb[1:] if t not in ("APT", "UNIT", "STE")][:3]
    return bool(na) and na[:2] == nb[:2]


@dataclass
class Subject:
    record_uuid: str
    owner_uuid: str
    kind: str                     # person | entity | deceased | probate
    first: str = ""
    last: str = ""
    company: str = ""
    street: str = ""
    city: str = ""
    state: str = ""
    zip: str = ""
    mail_street: str = ""
    mail_city: str = ""
    mail_state: str = ""
    mail_zip: str = ""
    attempts: int = 0
    existing: set = field(default_factory=set)
    tags: list = field(default_factory=list)
    org: bool = False             # a company, trust or agency owns it (a blank name alone is not that)


def missing_for_trace(s: Subject) -> list[str]:
    """What a record lacks before a paid trace is worth buying, in plain words; [] when it is
    complete. A source matches on first name, last name and mailing address: without all three it
    either bills for a miss or returns a stranger who shares half the name."""
    out = []
    if not (s.first and s.last):
        out.append("no full name")
    if not (s.mail_street and (s.mail_zip or (s.mail_city and s.mail_state))):
        out.append("no mailing address")
    return out


def line_kind(text) -> str:
    """The scorer's line type in DataSift's phone-type words (MOBILE, LANDLINE, VOIP), or '' when
    the scorer could not say. A text may only use a mobile and landlines are dialed last, so a
    number left at UNKNOWN is a number neither rule can see."""
    t = str(text or "").lower()
    if "voip" in t:
        return "VOIP"
    if "mobile" in t or "wireless" in t or "cell" in t:
        return "MOBILE"
    if "land" in t:
        return "LANDLINE"
    return ""


def _tag_titles(rec: dict) -> list[str]:
    out = []
    for t in rec.get("tags") or []:
        n = (t.get("title") or t.get("name")) if isinstance(t, dict) else t
        if n:
            out.append(str(n))
    return out


def subject_of(rec: dict, probate_tags=("Probate",), deceased_tags=("deceased", "Obituary")) -> Subject:
    ow = rec.get("owner") or {}
    pa = rec.get("address") or {}
    ma = ow.get("address") or {}
    tags = _tag_titles(rec)
    low = {t.lower() for t in tags}
    first = " ".join(str(ow.get("first_name") or "").split())
    last = " ".join(str(ow.get("last_name") or "").split())
    company = " ".join(str(ow.get("company") or "").split())
    name = " ".join([first, last, company])
    if ow.get("deceased") or low & {t.lower() for t in deceased_tags}:
        kind = "deceased"
    elif low & {t.lower() for t in probate_tags}:
        kind = "probate"
    elif company or ORG.search(name) or not (first and last):
        kind = "entity"
    else:
        kind = "person"
    existing = {digits(p.get("number")) for p in (ow.get("phones") or [])} - {""}
    return Subject(record_uuid=rec.get("uuid") or "", owner_uuid=ow.get("uuid") or "", kind=kind,
                   first=first.split(" ")[0] if first else "", last=last, company=company,
                   street=pa.get("street") or "", city=pa.get("city") or "", state=pa.get("state") or "TN",
                   zip=str(pa.get("postal_code") or pa.get("zip5") or "")[:5],
                   mail_street=ma.get("street") or "", mail_city=ma.get("city") or "",
                   mail_state=ma.get("state") or "", mail_zip=str(ma.get("postal_code") or "")[:5],
                   attempts=int(rec.get("predictivecall_attempts") or 0), existing=existing, tags=tags,
                   org=bool(company or ORG.search(name)))


class Source:
    name = ""
    unit_usd = 0.0
    per = "row"          # row: billed per submitted subject; match: billed per match only

    def configured(self) -> bool:
        return False

    def estimate(self, n: int) -> float:
        return round(n * self.unit_usd, 2)

    def trace(self, subjects: list[Subject]) -> tuple[dict, float]:
        """({record_uuid: [{"number": digits, "type": MOBILE|LANDLINE|UNKNOWN}]}, usd actually billed)."""
        raise NotImplementedError


class Tracerfy(Source):
    """Tracerfy batch: multipart CSV, Bearer auth, the mail_* columns are required even when empty,
    phones come back flat. $0.02 per submitted row, a miss included."""
    name, unit_usd, per = "tracerfy", 0.02, "row"

    def __init__(self, key: str = "", poll_s: float = 5.0, max_wait_s: float = 300.0):
        self.key = key or os.environ.get("TRACERFY_API_KEY", "")
        self.poll_s, self.max_wait_s = poll_s, max_wait_s

    def configured(self) -> bool:
        return bool(self.key)

    def _multipart(self, fields: dict, csv_text: str) -> tuple[bytes, str]:
        b = "----siftstack%d" % int(time.time() * 1000)
        parts = []
        for k, v in fields.items():
            parts.append("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n" % (b, k, v))
        parts.append("--%s\r\nContent-Disposition: form-data; name=\"csv_file\"; filename=\"batch.csv\"\r\n"
                     "Content-Type: text/csv\r\n\r\n%s\r\n--%s--\r\n" % (b, csv_text, b))
        return "".join(parts).encode("utf-8"), "multipart/form-data; boundary=%s" % b

    def trace(self, subjects: list[Subject]) -> tuple[dict, float]:
        rows = [s for s in subjects if s.first and s.last and s.street]
        if not rows:
            return {}, 0.0
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["first_name", "last_name", "address", "city", "state", "zip", "mail_address", "mail_city",
                    "mail_state", "mail_zip"])
        for s in rows:
            w.writerow([s.first, s.last, s.street, s.city, s.state, s.zip, s.mail_street, s.mail_city,
                        s.mail_state, s.mail_zip])
        fields = {"first_name_column": "first_name", "last_name_column": "last_name", "address_column": "address",
                  "city_column": "city", "state_column": "state", "zip_column": "zip",
                  "mail_address_column": "mail_address", "mail_city_column": "mail_city",
                  "mail_state_column": "mail_state", "mailing_zip_column": "mail_zip"}
        body, ctype = self._multipart(fields, buf.getvalue())
        req = urllib.request.Request(TRACERFY_TRACE_URL, data=body, method="POST",
                                     headers={"Authorization": "Bearer " + self.key, "Content-Type": ctype})
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                queued = json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            if e.code == 402:
                raise RuntimeError("Tracerfy is out of credits (HTTP 402); top up before the next run")
            raise RuntimeError("Tracerfy submit HTTP %s: %s" % (e.code, e.read().decode()[:200]))
        qid = queued.get("queue_id")
        if not qid:
            raise RuntimeError("Tracerfy returned no queue id: %s" % str(queued)[:200])
        billed = round(len(rows) * self.unit_usd, 2)
        t0 = time.time()
        records = None
        while time.time() - t0 < self.max_wait_s:
            time.sleep(self.poll_s)
            req = urllib.request.Request(TRACERFY_QUEUE_URL + str(qid), headers={"Authorization": "Bearer " + self.key})
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read().decode() or "{}")
            if isinstance(data, list):
                records = data
                break
            st = (data.get("status") or "").lower()
            if st == "failed":
                raise RuntimeError("Tracerfy job %s failed (billed rows: %d)" % (qid, len(rows)))
            if st == "completed":
                records = data.get("records") or []
                break
        if records is None:
            raise RuntimeError("Tracerfy job %s still running after %ds; billed, results not read" % (qid, self.max_wait_s))
        return self.match(rows, records), billed

    @staticmethod
    def match(rows: list[Subject], records: list[dict]) -> dict:
        """Results back to records: name AND street when the result echoes the street, else name only
        when that name is unique in the batch (a shared name could hand one owner another's phones)."""
        by_name: dict[tuple, list[Subject]] = {}
        for s in rows:
            by_name.setdefault((s.first.lower(), s.last.lower()), []).append(s)
        out: dict[str, list[dict]] = {}
        for rec in records or []:
            if not isinstance(rec, dict):
                continue
            key = ((rec.get("first_name") or "").strip().lower(), (rec.get("last_name") or "").strip().lower())
            cands = by_name.get(key) or []
            street = rec.get("address") or rec.get("property_address") or ""
            hit = [s for s in cands if street and same_street(s.street, street)]
            if not hit and len(cands) == 1:
                hit = cands
            if len(hit) != 1:
                continue
            nums = []
            for f in TRACERFY_PHONE_FIELDS:
                d = digits(rec.get(f))
                if d and d not in [n["number"] for n in nums]:
                    nums.append({"number": d, "type": "MOBILE" if "mobile" in f else
                                 ("LANDLINE" if "landline" in f else "UNKNOWN")})
            if nums:
                out.setdefault(hit[0].record_uuid, []).extend(nums)
        return out


class Enformion(Source):
    """Enformion Person Search, anchored on the full street line, billed per match."""
    name, unit_usd, per = "enformion", 0.10, "match"

    def __init__(self, ap_name: str = "", ap_password: str = ""):
        self.ap_name = ap_name or os.environ.get("ENFORMION_AP_NAME", "")
        self.ap_password = ap_password or os.environ.get("ENFORMION_AP_PASSWORD", "")

    def configured(self) -> bool:
        return bool(self.ap_name and self.ap_password)

    def _search(self, s: Subject) -> dict:
        body = {"FirstName": s.first, "LastName": s.last, "Page": 1, "ResultsPerPage": 3,
                "Addresses": [{"AddressLine1": s.street,
                               "AddressLine2": ("%s, %s %s" % (s.city, s.state, s.zip)).strip(", ")}]}
        req = urllib.request.Request(ENFORMION_URL, data=json.dumps(body).encode(), method="POST", headers={
            "galaxy-ap-name": self.ap_name, "galaxy-ap-password": self.ap_password, "galaxy-search-type": "Person",
            "Content-Type": "application/json", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                return json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            log.warning("Enformion HTTP %s for record %s", e.code, s.record_uuid)
            return {}

    @staticmethod
    def phones_if_match(data: dict, s: Subject) -> tuple[list[dict], bool]:
        """(numbers, billed). A person whose address history lacks the anchor street is a stranger
        with the same name: billed, but nothing of theirs lands on the record."""
        persons = data.get("persons") or data.get("people") or []
        if not persons:
            return [], False
        p = persons[0]
        hist = [a.get("fullAddress") or a.get("addressLine1") or "" for a in (p.get("addresses") or [])]
        if not any(same_street(s.street, h) or (s.mail_street and same_street(s.mail_street, h)) for h in hist):
            return [], True
        out = []
        for ph in p.get("phoneNumbers") or []:
            d = digits(ph.get("phoneNumber") or ph.get("number")) if isinstance(ph, dict) else ""
            if d and d not in [n["number"] for n in out]:
                t = str(ph.get("phoneType") or "").upper()
                out.append({"number": d, "type": "MOBILE" if "WIRELESS" in t or "MOBILE" in t else
                            ("LANDLINE" if "LAND" in t else "UNKNOWN")})
        return out, True

    def trace(self, subjects: list[Subject]) -> tuple[dict, float]:
        out, billed = {}, 0.0
        self.costs = {}
        for s in subjects:
            if not (s.first and s.last and s.street):
                continue
            nums, paid = self.phones_if_match(self._search(s), s)
            if paid:
                billed += self.unit_usd
                self.costs[s.record_uuid] = self.unit_usd
            if nums:
                out[s.record_uuid] = nums
            time.sleep(0.3)
        return out, round(billed, 2)


class SmartSkip(Source):
    """SmartSkip bulk skip: the lessons' second pass on first-to-market records (5-01: DataSift's
    free trace first, then SmartSkip at 15 cents a hit). Same client the deep prospect chain uses
    (skills/deep-prospecting-v5/scripts): upload, calculate, pay the saved card through a payment
    intent, poll, download, parse. Billed per HIT. On a living owner only the subject's own numbers
    land; relatives' numbers belong to the deep prospect, where a person decides who signs.

    The client sys.exits on any error; every exit becomes a RuntimeError here so a stage reports it
    instead of the worker dying. A payment that needs 3DS stops with the order id, nothing guessed."""
    name, unit_usd, per = "smartskip", 0.15, "match"

    def __init__(self, max_wait_s: float = 600.0, poll_s: float = 15.0):
        self.max_wait_s, self.poll_s = max_wait_s, poll_s
        self.costs: dict = {}

    def configured(self) -> bool:
        return bool(os.environ.get("SMARTSKIP_EMAIL") and os.environ.get("SMARTSKIP_PASSWORD"))

    def _modules(self):
        import importlib
        from .. import config
        os.environ.setdefault("SKIPTRACE_RUN_DIR", str(Path(config.DATA_DIR) / "smartskip"))
        scripts = SRC.parent / "skills" / "deep-prospecting-v5" / "scripts"
        if str(scripts) not in sys.path:
            sys.path.insert(0, str(scripts))
        return importlib.import_module("smartskip_trace"), importlib.import_module("parse_smartskip")

    def trace(self, subjects: list[Subject]) -> tuple[dict, float]:
        rows = [s for s in subjects if s.first and s.last and (s.mail_street or s.street)]
        self.costs = {}
        if not rows:
            return {}, 0.0
        ss, ps = self._modules()
        run_dir = Path(os.environ["SKIPTRACE_RUN_DIR"])
        run_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        csv_path = run_dir / ("autopilot_%s.csv" % stamp)
        cols = ["First Name", "Middle Name", "Last Name", "Mailing Address", "Mailing City", "Mailing State",
                "Mailing Zip", "Property Address", "Property City", "Property State", "Property Zip"]
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(cols)
            for s in rows:
                w.writerow([s.first, "", s.last, s.mail_street or s.street, s.mail_city or s.city,
                            s.mail_state or s.state, s.mail_zip or s.zip, s.street, s.city, s.state, s.zip])
        try:
            sess = ss.Session()
            entry = ss.submit(sess, str(csv_path), {})
            bid = entry["bulkSkipId"]
            if not ss.pay(sess, bid):
                raise RuntimeError("SmartSkip order %s needs a 3DS card confirmation in the app; nothing was traced" % bid)
            t0 = time.time()
            while True:
                items = ss.fetch_status(sess, bid)
                st = ((items[0].get("status") if items else "") or "").lower()
                if st == "completed":
                    break
                if st in ("error", "failed"):
                    raise RuntimeError("SmartSkip order %s ended %s" % (bid, st))
                if time.time() - t0 > self.max_wait_s:
                    raise RuntimeError("SmartSkip order %s is paid and still running after %ds; its results "
                                       "are not on the records yet" % (bid, self.max_wait_s))
                time.sleep(self.poll_s)
            out = run_dir / ("autopilot_%s_vertical.csv" % stamp)
            ss.download(sess, bid, str(out), "vertical")
            parsed = ps.parse(str(out))
        except SystemExit as exc:
            raise RuntimeError("SmartSkip: %s" % exc)
        return self.match(rows, parsed), round(self.unit_usd * sum(1 for p in parsed if p.get("has_results")), 2)

    def match(self, rows: list[Subject], parsed: list[dict]) -> dict:
        by_name: dict[tuple, list[Subject]] = {}
        for s in rows:
            by_name.setdefault((s.first.lower(), s.last.lower()), []).append(s)
        out: dict[str, list[dict]] = {}
        for p in parsed or []:
            cands = by_name.get(((p.get("first") or "").strip().lower(), (p.get("last") or "").strip().lower())) or []
            hit = [s for s in cands if same_street(s.street, p.get("property_address") or "")] or (cands if len(cands) == 1 else [])
            if len(hit) != 1:
                continue
            u = hit[0].record_uuid
            if p.get("has_results"):
                self.costs[u] = self.unit_usd
            nums = []
            for ph in p.get("subject_phones") or []:
                d = digits(ph.get("number"))
                if d and d not in [n["number"] for n in nums]:
                    t = str(ph.get("type") or "").upper()
                    nums.append({"number": d, "type": "MOBILE" if ("WIRELESS" in t or "MOBILE" in t or "CELL" in t)
                                 else ("LANDLINE" if "LAND" in t else "UNKNOWN")})
            if nums:
                out[u] = nums
        return out


class Scorer:
    """Trestle phone intelligence with the litigator add-on. $0.015 a number on the paid key; the
    free key is used first when the Deal Room router is importable on this box."""
    unit_usd = 0.015

    def __init__(self, key: str = ""):
        self.key = (key or os.environ.get("TRESTLE_PAID_API_KEY") or os.environ.get("TRESTLE_API_KEY") or "")

    def configured(self) -> bool:
        return bool(self.key)

    def score(self, numbers: list[str]) -> dict:
        import phone_validator as pv
        out = {}
        for n in numbers:
            data = pv.call_trestle(n, self.key, add_litigator=True)
            if data.get("error"):
                out[n] = {"error": str(data["error"])[:120]}
                continue
            score = data.get("activity_score")
            add = data.get("add_ons") or {}
            lit = bool(isinstance(add, dict) and isinstance(add.get("litigator_checks"), dict)
                       and add["litigator_checks"].get("phone.is_litigator_risk"))
            out[n] = {"score": score, "tier": "DNC" if lit else pv.assign_tier(score, pv.DEFAULT_TIERS),
                      "line_type": data.get("line_type") or "", "litigator": lit}
        return out


def default_sources() -> dict:
    return {"smartskip": SmartSkip(), "tracerfy": Tracerfy(), "enformion": Enformion()}

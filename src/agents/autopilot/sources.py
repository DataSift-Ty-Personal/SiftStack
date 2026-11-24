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
# Tracerfy sits behind Cloudflare, which refuses urllib's default "Python-urllib/3.x" User-Agent with
# HTTP 403 "error code: 1010" before the key is read (measured on the brain 2026-11-20: default 403,
# a named agent reaches the API). Every Tracerfy request names itself.
TRACERFY_AGENT = "SiftStack-autopilot/1.0"


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
                                     headers={"Authorization": "Bearer " + self.key, "Content-Type": ctype,
                                              "User-Agent": TRACERFY_AGENT})
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
            req = urllib.request.Request(TRACERFY_QUEUE_URL + str(qid), headers={"Authorization": "Bearer " + self.key,
                                                                                 "User-Agent": TRACERFY_AGENT})
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
    instead of the worker dying. A payment that needs 3DS stops with the order id, nothing guessed.

    THE ORDER BOOK (2026-11-23). An order that timed out used to be paid and forgotten: trace() raised,
    no ledger row was written, and the next run bought the same records again, while the only trace of
    the order id was the client's own file, which sits wherever the first importer of the client
    pointed it (a research run's folder, on a box that runs both). So every order is written to this
    class's own book (`<DATA_DIR>/smartskip/autopilot_orders.json`) the moment SmartSkip gives it an id,
    BEFORE the card is charged, with the rows it carries; `collect()` picks a paid order up again on a
    later run (a finished order downloads for free), and a caller marks it claimed once its results
    are used. Downloads are kept under `<DATA_DIR>/smartskip/orders/` so nothing is downloaded twice.

    `details` keeps, per record, everything the parser read for the person matched to it (SmartSkip's
    Deceased flag, every relative with their own address, phones and flag) and the order id, beside the
    numbers `match` returns as it always did."""
    name, unit_usd, per = "smartskip", 0.15, "match"
    TRACE_LABEL = "trace"
    # submitted: the id is known, the payment is not confirmed; needs_card: the payment asked for 3DS;
    # paid: charged, results not read yet; collected: results on disk, not yet used by the caller.
    OPEN = ("submitted", "needs_card", "paid", "collected")
    ROW_FIELDS = ("record_uuid", "owner_uuid", "kind", "first", "last", "company", "street", "city", "state", "zip",
                  "mail_street", "mail_city", "mail_state", "mail_zip")
    COLS = ["First Name", "Middle Name", "Last Name", "Mailing Address", "Mailing City", "Mailing State",
            "Mailing Zip", "Property Address", "Property City", "Property State", "Property Zip"]
    import threading as _threading   # the book is read and rewritten whole, so writers take turns
    _BOOK_LOCK = _threading.Lock()

    def __init__(self, max_wait_s: float = 600.0, poll_s: float = 15.0, card_wait_days: float = 3.0,
                 expire_days: float = 14.0, folder=None):
        self.max_wait_s, self.poll_s = max_wait_s, poll_s
        self.card_wait_days, self.expire_days = card_wait_days, expire_days
        self.folder = Path(folder) if folder else None
        self.costs: dict = {}
        self.details: dict = {}

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

    # ------------------------------------------------------------------ the order book
    def home(self) -> Path:
        if self.folder is not None:
            return self.folder
        from .. import config
        return Path(config.DATA_DIR) / "smartskip"

    def book_path(self) -> Path:
        return self.home() / "autopilot_orders.json"

    def order_file(self, bid: str) -> Path:
        return self.home() / "orders" / ("%s_vertical.csv" % bid)

    def book(self) -> list[dict]:
        p = self.book_path()
        try:
            got = json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
        except (OSError, ValueError):
            return []
        return got if isinstance(got, list) else []

    def keep(self, entry: dict) -> dict:
        """Write one order into the book (replacing the entry with the same id). A finished entry is
        dropped after 60 days so the book stays small; its download stays in orders/."""
        with self._BOOK_LOCK:
            now = time.time()
            rows = [e for e in self.book() if e.get("bulkSkipId") != entry.get("bulkSkipId")
                    and (e.get("status") in self.OPEN or now - float(e.get("created_ts") or now) < 60 * 86400)]
            rows.append(entry)
            p = self.book_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_name(p.name + ".tmp")
            tmp.write_text(json.dumps(rows, indent=1, default=str), encoding="utf-8")
            tmp.replace(p)
        return entry

    @staticmethod
    def _stamp() -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def pending(self, label: str = "") -> list[dict]:
        """The orders still open for this label, oldest first. An order whose payment was never confirmed
        for `card_wait_days` is given up (nothing was charged: an unpaid order never runs), and any open
        order older than `expire_days` is given up too, so the book cannot hold a stage still for ever."""
        out = []
        now = time.time()
        for e in sorted(self.book(), key=lambda x: float(x.get("created_ts") or 0)):
            if (label and e.get("label") != label) or e.get("status") not in self.OPEN:
                continue
            age_days = (now - float(e.get("created_ts") or now)) / 86400.0
            if e.get("status") in ("submitted", "needs_card") and age_days > self.card_wait_days:
                self.keep(dict(e, status="abandoned", why="the payment was never confirmed"))
                continue
            if age_days > self.expire_days:
                self.keep(dict(e, status="abandoned", why="still open after %g days" % self.expire_days))
                continue
            out.append(e)
        return out

    def claim(self, entry: dict, note: str = "") -> dict:
        """The caller has used this order's results: it leaves the open orders for good."""
        return self.keep(dict(entry, status="claimed", claimed=self._stamp(), note=note))

    @classmethod
    def row_of(cls, s: Subject) -> dict:
        return {k: getattr(s, k) for k in cls.ROW_FIELDS}

    @classmethod
    def subject_from(cls, row: dict) -> Subject:
        return Subject(**{k: row.get(k) or "" for k in cls.ROW_FIELDS})

    # ------------------------------------------------------------------ one order
    def place(self, subjects: list[Subject], label: str, meta: dict | None = None) -> dict:
        """Upload, map and calculate (free), write the order into the book, THEN pay. Returns the book
        entry: status paid, or needs_card when the card asks for 3DS (nothing charged). A refused payment
        is recorded and raised; an error before the order exists raises with nothing ordered."""
        rows = [s for s in subjects if s.first and s.last and (s.mail_street or s.street)]
        if not rows:
            raise ValueError("no subject has a full name and an address")
        ss, _ = self._modules()
        home = self.home()
        home.mkdir(parents=True, exist_ok=True)
        csv_path = home / ("%s_%s.csv" % (label or "autopilot", time.strftime("%Y%m%d_%H%M%S")))
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(self.COLS)
            for s in rows:
                w.writerow([s.first, "", s.last, s.mail_street or s.street, s.mail_city or s.city,
                            s.mail_state or s.state, s.mail_zip or s.zip, s.street, s.city, s.state, s.zip])
        try:
            sess = ss.Session()
            got = ss.submit(sess, str(csv_path), {})
        except SystemExit as exc:
            raise RuntimeError("SmartSkip: %s" % exc)
        bid = got["bulkSkipId"]
        entry = self.keep({"bulkSkipId": bid, "label": label, "status": "submitted", "created": self._stamp(),
                           "created_ts": time.time(), "csv": str(csv_path), "entities": got.get("entities"),
                           "rows": [self.row_of(s) for s in rows], "meta": meta or {}})
        try:
            ok = ss.pay(sess, bid)
        except SystemExit as exc:
            self.keep(dict(entry, status="pay_failed", why=str(exc)[:200]))
            raise RuntimeError("SmartSkip refused the payment for order %s (%s); nothing was traced" % (bid, str(exc)[:120]))
        return self.keep(dict(entry, status="paid" if ok else "needs_card", paid=self._stamp() if ok else ""))

    def _parse(self, path: Path) -> list:
        _, ps = self._modules()
        return ps.parse(str(path))

    def _download(self, ss, sess, bid: str):
        """The order's vertical file, downloaded once (free) into orders/, parsed; None when SmartSkip has
        nothing to give yet. An empty answer is not taken as a finished order."""
        f = self.order_file(bid)
        f.parent.mkdir(parents=True, exist_ok=True)
        part = f.with_name(f.name + ".part")
        try:
            ss.download(sess, bid, str(part), "vertical")
            parsed = self._parse(part)
        except (SystemExit, OSError, ValueError, KeyError):
            return None
        if not parsed:
            return None
        part.replace(f)
        return parsed

    def collect(self, entry: dict, wait_s: float | None = None):
        """The parsed results of a book entry, or None while SmartSkip is still working on it (or the
        payment was never confirmed). Polls up to `wait_s` (default max_wait_s; 0 is one look). An order
        SmartSkip ended in error is recorded failed and raised. SmartSkip's order list holds only orders
        that were paid, so an order seen there is paid whatever the book said."""
        bid = entry["bulkSkipId"]
        f = self.order_file(bid)
        if entry.get("status") == "collected" and f.exists():
            try:
                return self._parse(f)
            except SystemExit as exc:
                raise RuntimeError("SmartSkip order %s: its saved results do not read (%s)" % (bid, exc))
        ss, _ = self._modules()
        try:
            sess = ss.Session()
        except SystemExit as exc:
            raise RuntimeError("SmartSkip: %s" % exc)
        wait = self.max_wait_s if wait_s is None else float(wait_s)
        t0 = time.time()
        while True:
            try:
                items = ss.fetch_status(sess, bid)
            except SystemExit as exc:
                raise RuntimeError("SmartSkip: could not read order %s (%s)" % (bid, str(exc)[:120]))
            st = ((items[0].get("status") if items else "") or "").lower()
            if st in ("error", "failed"):
                self.keep(dict(entry, status="failed", why="SmartSkip ended the order %s" % st))
                raise RuntimeError("SmartSkip order %s ended %s" % (bid, st))
            if items and entry.get("status") in ("submitted", "needs_card"):
                entry = self.keep(dict(entry, status="paid", paid=self._stamp()))
            if st == "completed" or (not items and entry.get("status") == "paid"):
                got = self._download(ss, sess, bid)
                if got is not None:
                    self.keep(dict(entry, status="collected", collected=self._stamp(), vertical=str(f)))
                    return got
                if st == "completed":
                    raise RuntimeError("SmartSkip order %s is finished but its results could not be read" % bid)
            if time.time() - t0 >= wait:
                return None
            time.sleep(self.poll_s)

    def completed_orders(self) -> list[dict]:
        """Every order SmartSkip finished on this account, newest first. Its order list answers with every
        order in one reply (page, skip and limit are ignored; measured 2026-11-23 on 120 orders)."""
        ss, _ = self._modules()
        try:
            items = ss.fetch_status(ss.Session())
        except SystemExit as exc:
            raise RuntimeError("SmartSkip: could not list the orders (%s)" % str(exc)[:120])
        return [i for i in items or [] if str(i.get("status") or "").lower() == "completed"]

    def results(self, bid: str):
        """A finished order's parsed results: the saved download when there is one, else one free download."""
        f = self.order_file(bid)
        if f.exists():
            try:
                return self._parse(f)
            except SystemExit:
                pass
        ss, _ = self._modules()
        try:
            sess = ss.Session()
        except SystemExit as exc:
            raise RuntimeError("SmartSkip: %s" % exc)
        return self._download(ss, sess, bid)

    # ------------------------------------------------------------------ the retrace's contract
    def trace(self, subjects: list[Subject]) -> tuple[dict, float]:
        """({record_uuid: numbers}, usd billed), as every source answers. A paid order an earlier call left
        unfinished is collected first and its records are not bought again; while it is still running,
        or still waiting for a card confirmation, nothing new is ordered for them (raised, like a timeout,
        so the caller writes nothing and tries again next run). What that earlier order cost is returned
        here, because the call that paid for it raised before it could say so."""
        rows = [s for s in subjects if s.first and s.last and (s.mail_street or s.street)]
        self.costs, self.details = {}, {}
        if not rows:
            return {}, 0.0
        left = {s.record_uuid: s for s in rows}
        found, billed = {}, 0.0
        for entry in self.pending(self.TRACE_LABEL):
            bid = entry["bulkSkipId"]
            covers = [r.get("record_uuid") for r in entry.get("rows") or [] if r.get("record_uuid") in left]
            if not covers:
                continue
            parsed = self.collect(entry, wait_s=0)
            if parsed is None:
                raise RuntimeError("SmartSkip order %s from an earlier run is %s; its records wait for it and nothing "
                                   "new was ordered" % (bid, "still running" if entry.get("status") == "paid"
                                                        else "waiting for a card confirmation in the app"))
            sub = [left.pop(u) for u in covers]
            found.update(self.match(sub, parsed, order=bid))
            billed += sum(self.costs.get(u, 0.0) for u in covers)
            self.claim(entry, "collected by a later trace")
        if left:
            sub = list(left.values())
            entry = self.place(sub, self.TRACE_LABEL)
            bid = entry["bulkSkipId"]
            if entry.get("status") == "needs_card":
                raise RuntimeError("SmartSkip order %s needs a 3DS card confirmation in the app; nothing was traced" % bid)
            parsed = self.collect(entry)
            if parsed is None:
                raise RuntimeError("SmartSkip order %s is paid and still running after %ds; its results are not on "
                                   "the records yet (the next run collects them)" % (bid, self.max_wait_s))
            found.update(self.match(sub, parsed, order=bid))
            billed += self.unit_usd * sum(1 for p in parsed if p.get("has_results"))
            self.claim(entry)
        return found, round(billed, 2)

    def match(self, rows: list[Subject], parsed: list[dict], order: str = "", by_input: bool = False) -> dict:
        """Results back to records: name AND street when the result echoes the street, else name only when
        that name is unique in the order. `by_input` also matches on the order's own Input Name column
        (the name as it was sent), for a person SmartSkip writes back under a fuller first name."""
        by_name: dict[tuple, list[Subject]] = {}
        by_sent: dict[str, list[Subject]] = {}
        for s in rows:
            by_name.setdefault((s.first.lower(), s.last.lower()), []).append(s)
            by_sent.setdefault(" ".join(("%s %s" % (s.first, s.last)).lower().split()), []).append(s)
        out: dict[str, list[dict]] = {}
        for p in parsed or []:
            cands = by_name.get(((p.get("first") or "").strip().lower(), (p.get("last") or "").strip().lower())) or []
            if not cands and by_input:
                cands = by_sent.get(" ".join(str(p.get("input_name") or "").lower().split())) or []
            hit = [s for s in cands if same_street(s.street, p.get("property_address") or "")] or (cands if len(cands) == 1 else [])
            if len(hit) != 1:
                continue
            u = hit[0].record_uuid
            self.details[u] = dict(p, order=order)
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

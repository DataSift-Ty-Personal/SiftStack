"""The county sources the ranking reads, by county FIPS code. Every source here is public and free.

A county with an adapter gets the live tax roll (who the county says owns the house, every bill, who paid the
last one), the city's own tax page where a city bills separately, an obituary index to check a death against,
and the probate court's index of estates. A county without one is still ranked, on what the account and
SiftMap know; the workbook says which sources a house was read against.

Built in (Tennessee):
    47093 Knox County    Trustee roll (knox-tn.mygovonline.com), City of Knoxville tax (city code 386), the Knox
                         County Public Library's obituary index (News Sentinel since 1991) and its probate index
                         (cases opened 1954 to about February 2025, a public Google Sheet)
    47009 Blount County  Trustee roll (blount-tn.mygovonline.com)

Adding a county is one entry in COUNTIES: give it a `roll` key that agents/autopilot/taxwatch.COUNTY_ROLLS
knows (the same vendor serves many Tennessee trustees), and any of the other readers it has.
"""
from __future__ import annotations

import csv
import datetime as dt
import glob
import io
import os
import re
import sys
import time
import urllib.parse
from pathlib import Path

from . import common as C

if str(C.SRC) not in sys.path:
    sys.path.insert(0, str(C.SRC))

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"

# ----------------------------------------------------------------------------------- the tax roll
def _tw():
    from agents.autopilot import taxwatch as tw
    return tw


def bill_view(b: dict) -> dict:
    tw = _tw()
    pays = b.get("payments") or []
    last = pays[-1] if pays and isinstance(pays[-1], dict) else {}
    info = b.get("parcelInformation") or {}
    return {"year": str(b.get("taxYear") or b.get("tax_year") or ""), "type": b.get("bill_type"),
            "delinquent": bool(b.get("delinquent")), "paid": bool(b.get("paid", True)), "due": tw._num(b.get("due")) or 0.0,
            "amount": tw._num(b.get("amount")) or 0.0, "turned_over": bool(b.get("turnedOver")),
            "owner": b.get("owner"), "payor": last.get("payor"), "pay_date": last.get("date"),
            "pay_amount": tw._num(last.get("amount")), "mailing": info.get("mailingAddress"),
            "city_no": info.get("cityNumber")}


def read_roll(roll_key: str, street: str, owner: str = "", fetch=None) -> dict:
    """One house on a county trustee roll: {"status", "chosen", "accounts": [...]}. Status is ok, ambiguous
    (two accounts at the house give different answers and none is in the owner's name), no bills, not on roll,
    no house number, or no roll. Raises OSError when the county does not answer (worth asking again)."""
    tw = _tw()
    base = tw.COUNTY_ROLLS.get(roll_key)
    if not base:
        return {"status": "no roll"}
    fetch = fetch or (lambda url: tw._fetch(url, pause=0.5))
    line = (street or "").split(",")[0].strip()
    if not line[:1].isdigit():
        return {"status": "no house number"}
    tried, found = {}, []
    for q in tw.street_queries(line):
        data = fetch("%s/parcels/%s?detail_level=public&start=0&length=10" % (base, urllib.parse.quote(q)))
        parcels = (data or {}).get("parcels") if isinstance(data, dict) else None
        hits = [p for p in (parcels if isinstance(parcels, list) else [])
                if isinstance(p, dict) and tw.same_house(line, str(p.get("parcel_address") or ""))]
        if not hits:
            continue
        for p in hits:
            acct = tw._account(p.get("account_number"), base)
            if not acct or acct in tried:
                continue
            bills = tw._bills(fetch("%s/due/PPT/%s?detail_level=public" % (base, urllib.parse.quote(acct))))
            tried[acct] = bills
            found.append({"account": acct, "owner": p.get("owner"), "address": p.get("parcel_address"),
                          "parcel_id": p.get("parcel_id"), "assessor_url": p.get("appraserURL"),
                          "bills": [bill_view(b) for b in (bills or []) if isinstance(b, dict)]})
        break
    if not found:
        return {"status": "not on roll"}
    answered = {f["account"]: (tried[f["account"]], f["address"], f["owner"]) for f in found if tried.get(f["account"]) is not None}
    choice = ""
    if answered:
        try:
            choice = tw._choose(answered, owner or "")
        except Exception:  # noqa: BLE001  (an unreadable choice is "ambiguous", never a guess)
            choice = ""
    return {"status": "ok" if choice else ("ambiguous" if answered else "no bills"), "chosen": choice, "accounts": found}


# ----------------------------------------------------------------------------------- city of Knoxville
KNOXVILLE_URL = "https://propertytax.knoxvilletn.gov/ptax_master_show_vw/ShowPtax_master_show_vw.aspx"
_MONEY = r"\$([\d,]+\.\d\d)"


def _cells(tr: str) -> list[str]:
    out = []
    for td in re.findall(r"(?is)<td[^>]*>(.*?)</td>", tr):
        t = re.sub(r"(?s)<[^>]+>", " ", td).replace("&nbsp;", " ").replace("&amp;", "&")
        out.append(re.sub(r"\s+", " ", t).strip())
    return out


def _amt(s) -> float:
    m = re.search(r"\$?([\d,]+\.\d\d)", s or "")
    return float(m.group(1).replace(",", "")) if m else 0.0


def parse_knoxville(html: str, this_year: int | None = None) -> dict:
    """The City of Knoxville's tax page for one parcel: owner, prior years owed, each year's status, and whether
    tax suit costs are on the bill. Read by table cells: flattening the page loses its empty columns."""
    this_year = this_year or C.today().year
    txt = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or "")).replace("&nbsp;", " ").replace("&amp;", "&")
    out = {"owner": None, "prior": None, "total": None, "years": [], "suit": False}
    m = re.search(r"Owner Names:\s*(.*?)\s*Assessed Value:", txt)
    if m:
        out["owner"] = m.group(1).strip()
    m = re.search(r"Taxes and Fees for Prior Years:\s*" + _MONEY, txt)
    if m:
        out["prior"] = float(m.group(1).replace(",", ""))
    m = re.search(r"Total Balance Due:\s*" + _MONEY, txt)
    if m:
        out["total"] = float(m.group(1).replace(",", ""))
    i = (html or "").find("Tax Summary")
    body = (html or "")[i:] if i >= 0 else ""
    for tr in re.findall(r"(?is)<tr[^>]*>(.*?)</tr>", body):
        c = _cells(tr)
        if len(c) < 9 or not re.fullmatch(r"(19|20)\d\d", c[0] or ""):
            continue
        levy, bal, inte, pen, suit, court, misc = (_amt(x) for x in (c + [""] * 12)[4:11])
        total = _amt(c[11]) if len(c) > 11 else 0.0
        owed = total or round(bal + inte + pen + suit + court + misc, 2)
        st = c[2].upper()
        if st != "PAID" and not owed and int(c[0]) < this_year:
            owed = levy                     # an unpaid prior year with no balance printed still owes its levy
        out["years"].append({"year": c[0], "status": st, "paid_on": c[3], "levy": levy, "balance": bal,
                             "owed": owed if st != "PAID" else 0.0, "suit_expense": suit})
        if suit > 0:
            out["suit"] = True
    return out


def read_knoxville(account: str, get=None) -> dict | None:
    """One parcel's city bill by the county account without its dash, or None when the city did not answer."""
    pid = str(account or "").replace("-", "").replace(" ", "")
    if not pid:
        return None
    if get is None:
        import requests
        sess = requests.Session()
        sess.headers.update({"User-Agent": UA})

        def get(url, params):
            r = sess.get(url, params=params, timeout=40)
            return r.status_code, r.text
    for attempt in range(3):
        try:
            code, text = get(KNOXVILLE_URL, {"property_id": pid})
            if code == 200:
                return parse_knoxville(text)
        except Exception:  # noqa: BLE001  (the city page is slow at times: ask again, then give up for this run)
            pass
        time.sleep(4 * (attempt + 1))
    return None


# ----------------------------------------------------------------------------------- obituary index
def knox_obituaries(last: str, first: str) -> str | None:
    """The Knox County Public Library obituary index rows for a name (text, one row a line), or None when the
    index did not answer."""
    from agents.research.open_research import lookup_obituary_index
    txt = lookup_obituary_index(str(last).title(), str(first).title())
    if str(txt).startswith("index lookup failed"):
        time.sleep(8)
        txt = lookup_obituary_index(str(last).title(), str(first).title())
    return None if str(txt).startswith("index lookup failed") else str(txt)


# ----------------------------------------------------------------------------------- probate index
KNOX_PROBATE_SHEET = "1pwR4Zn4jEBafeG7wEbwDqJKy48Yxi02CrDlozN4IGmM"
KNOX_PROBATE_GID = "1812720079"


class ProbateIndex:
    """A probate court's index of estates as a CSV (columns Name, Type, Title, Date Open, Status, Date Closed,
    Docket, Will Book-Page Number), matched to a dead owner.

    A case counts for an owner when the first and last name match, a spelled-out middle name agrees (an initial
    needs the same letter), and the case opened from 45 days before to 12 years after the death. A will probated
    or an estate still open puts the house on the probate path; a closed no-will estate is a note (the heirs
    still hold their shares, and the court file names them)."""

    SUFFIX = {"JR", "SR", "II", "III", "IV", "MRS", "MR", "DR"}

    def __init__(self, path: Path | None):
        self.path = Path(path) if path else None
        self._by_surname: dict | None = None
        self.covers_to: dt.date | None = None

    def available(self) -> bool:
        return bool(self.path and self.path.exists())

    def _toks(self, s) -> list[str]:
        return [t for t in re.sub(r"[^A-Z ]", " ", str(s or "").upper()).split() if t not in self.SUFFIX]

    @staticmethod
    def _day(s):
        try:
            m, d, y = str(s).strip().split("/")
            return dt.date(int(y), int(m), int(d))
        except ValueError:
            return None

    def _load(self) -> dict:
        if self._by_surname is None:
            self._by_surname = {}
            if self.available():
                newest = None
                with open(self.path, encoding="utf-8", newline="") as fh:
                    for r in csv.DictReader(fh):
                        r = {str(k).replace("\n", " "): v for k, v in r.items()}
                        t = self._toks(r.get("Name"))
                        opened = self._day(r.get("Date Open"))
                        if not t or not opened:
                            continue
                        newest = max(newest, opened) if newest else opened
                        r["_open"], r["_tok"] = opened, set(t) | set(self._toks(r.get("Title")))
                        for s in t[:2]:            # a two-word surname puts the first name second or third
                            self._by_surname.setdefault(s, []).append(r)
                self.covers_to = newest
        return self._by_surname

    @staticmethod
    def kind(case: dict) -> str:
        t = str(case.get("type") or "").upper()
        if t.startswith(("TESTATE", "MUNIMENT")):
            return "will"
        if t.startswith(("INTESTATE", "PROBATE")):
            return "open" if case.get("status") in ("OPEN", "REOPEN") else "closed"
        return "other"

    def lookup(self, name, died: dt.date | None) -> list[dict]:
        """Cases opened after this owner's death under the same name, best fit first."""
        t = self._toks(name)
        if len(t) < 2 or not died:
            return []
        first, last = t[0], t[-1]
        mid = t[1] if len(t) > 2 else None
        hits = []
        same = [r for r in self._load().get(last, []) if first in r["_tok"] and last in r["_tok"]]
        for r in same:
            others = [x for x in self._toks(r.get("Title")) if x not in (first, last)]
            mid_ok = None
            if mid and others:
                if len(mid) > 1 and any(len(x) > 1 for x in others):
                    mid_ok = any(x.startswith(mid) or mid.startswith(x) for x in others if len(x) > 1)
                else:
                    mid_ok = any(x[0] == mid[0] for x in others)
                if not mid_ok:
                    continue
            if not (died - dt.timedelta(days=45) <= r["_open"] <= died + dt.timedelta(days=365 * 12)):
                continue
            years = (r["_open"] - died).days / 365.25
            fit = "strong" if mid_ok and years <= 3 else ("good" if years <= 3 else "weak")
            c = {"docket": r.get("Docket"), "title": r.get("Title"), "type": r.get("Type"), "open": r.get("Date Open"),
                 "opened": r["_open"].isoformat(), "status": r.get("Status"), "closed": r.get("Date Closed"),
                 "will": r.get("Will Book-Page Number"), "fit": fit, "same_name": len(same)}
            c["kind"] = self.kind(c)
            c["sure"] = fit == "strong" or (fit == "good" and len(same) <= 2)
            hits.append(c)
        return sorted(hits, key=lambda c: ({"strong": 0, "good": 1, "weak": 2}[c["fit"]], c["opened"]))

    @staticmethod
    def describe(c: dict) -> str:
        what = {"will": "will probated", "open": "estate open, no will",
                "closed": "no-will estate opened and closed"}.get(c.get("kind"), c.get("type") or "case")
        if c.get("status") == "REOPEN":
            what += " (reopened)"
        when = "opened %s" % c.get("open") + (", closed %s" % c["closed"] if c.get("closed") else "")
        return "%s (%s, %s, %s)" % (what, c.get("docket"), c.get("title"), when)


def download_sheet_csv(sheet: str, gid: str, dest: Path, get=None) -> Path:
    """A public Google Sheet tab as CSV, saved once a day."""
    if get is None:
        import requests

        def get(url):
            r = requests.get(url, headers={"User-Agent": UA}, timeout=300)
            r.raise_for_status()
            return r.content
    url = "https://docs.google.com/spreadsheets/d/%s/export?format=csv&gid=%s" % (sheet, gid)
    data = get(url)
    text = data.decode("utf-8", "replace") if isinstance(data, (bytes, bytearray)) else str(data)
    first = next(csv.reader(io.StringIO(text)), [])
    if "Name" not in [str(x).strip() for x in first] or "Date Open" not in [str(x).replace("\n", " ").strip() for x in first]:
        raise ValueError("the probate index sheet did not answer with its columns (got %r)" % first[:6])
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding="utf-8", newline="")
    return dest


# ----------------------------------------------------------------------------------- the registry
class County:
    def __init__(self, fips: str, name: str, state: str, roll: str = "", city_codes: dict | None = None,
                 obituaries=None, probate_sheet: tuple[str, str] | None = None, obituary_name: str = "",
                 probate_name: str = ""):
        self.fips, self.name, self.state, self.roll_key = fips, name, state, roll
        self.city_codes = city_codes or {}
        self.obituaries = obituaries
        self.probate_sheet = probate_sheet
        self.obituary_name, self.probate_name = obituary_name, probate_name

    @property
    def label(self) -> str:
        return "%s County, %s" % (self.name, self.state)

    def sources(self) -> list[str]:
        out = []
        if self.roll_key:
            out.append("the county trustee's live tax roll")
        if self.city_codes:
            out.append("the city's own tax page (%s)" % ", ".join(v[0] for v in self.city_codes.values()))
        if self.obituaries:
            out.append(self.obituary_name or "an obituary index")
        if self.probate_sheet:
            out.append(self.probate_name or "the probate court's index")
        return out

    def probate_index(self, work: Path, refresh: bool = False, get=None) -> ProbateIndex:
        """The probate index for this county, downloaded into the work folder at most once a day."""
        if not self.probate_sheet:
            return ProbateIndex(None)
        have = sorted(glob.glob(str(work / ("probate_index_%s_*.csv" % self.fips))))
        if have and not refresh:
            return ProbateIndex(Path(have[-1]))
        dest = work / ("probate_index_%s_%s.csv" % (self.fips, C.today().isoformat()))
        try:
            return ProbateIndex(download_sheet_csv(self.probate_sheet[0], self.probate_sheet[1], dest, get=get))
        except Exception:  # noqa: BLE001  (no index is no index: the workbook says so, the run goes on)
            return ProbateIndex(Path(have[-1]) if have else None)


COUNTIES = {
    "47093": County("47093", "Knox", "TN", roll="knox",
                    city_codes={"386": ("City of Knoxville", read_knoxville)},
                    obituaries=knox_obituaries,
                    obituary_name="the Knox County Public Library obituary index (News Sentinel since 1991)",
                    probate_sheet=(KNOX_PROBATE_SHEET, KNOX_PROBATE_GID),
                    probate_name="the Knox County Public Library probate index (cases opened 1954 to about Feb 2025)"),
    "47009": County("47009", "Blount", "TN", roll="blount"),
}

_ALIASES = {"knox": "47093", "knox county": "47093", "knox, tn": "47093", "knox county, tn": "47093",
            "blount": "47009", "blount county": "47009", "blount, tn": "47009", "blount county, tn": "47009"}


def county_for(fips: str = "", name: str = "", state: str = "") -> County:
    """The adapter for a county, or a bare one (no county sources) for any other county."""
    f = re.sub(r"\D", "", str(fips or ""))
    if f in COUNTIES:
        return COUNTIES[f]
    key = ("%s, %s" % (str(name or "").strip(), str(state or "").strip())).strip(", ").lower()
    for k in (key, str(name or "").strip().lower()):
        if k in _ALIASES and (not state or COUNTIES[_ALIASES[k]].state == C.state_code(state)):
            return COUNTIES[_ALIASES[k]]
    nm = re.sub(r"\s+county$", "", str(name or "").strip(), flags=re.I).title() or "Unknown"
    return County(f or "", nm, C.state_code(state) or str(state or "").upper())


def resolve(text: str) -> County:
    """'Knox, TN', 'Knox County, Tennessee', '47093' -> the county."""
    s = str(text or "").strip()
    if re.fullmatch(r"\d{5}", s):
        return county_for(fips=s)
    name, _, st = s.partition(",")
    if not st and s.lower() in _ALIASES:
        return COUNTIES[_ALIASES[s.lower()]]
    return county_for(name=name, state=st.strip())

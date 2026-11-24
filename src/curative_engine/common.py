"""Names, dates, money and the work folder, shared by every step of the curative engine."""
from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timezone
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
ROOT = SRC.parent

# ------------------------------------------------------------------------------------------- names
SUFFIX = {"JR", "SR", "II", "III", "IV", "V"}
NOISE = {"AND", "THE", "OF", "ETAL", "ET", "AL", "ETUX", "ETVIR", "MRS", "MR", "DR", "TR", "TRS", "TRUSTEE", "TRUST",
         "LIVING", "REVOCABLE", "FAMILY", "LIFE", "EST", "ESTATE", "HEIRS", "LE", "LT", "C", "O", "CO"}
# The words an obituary or a deed carries that are never part of the person's name.
NAME_NOISE = {"TRUST", "TRUSTEE", "LIVING", "REVOCABLE", "FAMILY", "ESTATE", "HEIRS", "ETAL", "LIFE", "EST", "OF", "THE",
              "TR", "TRS"}
ENTITY_RX = re.compile(r"\b(LLC|L L C|INC|CORP|CORPORATION|COMPANY|PROPERTIES|HOLDINGS|INVESTMENTS?|PARTNERS|LP|LLP|"
                       r"BANK|CHURCH|MINISTR|ASSOCIATION|ASSN|HOMES|REALTY|GROUP|FUND|VENTURES|ENTERPRISES|TRUSTEES? OF|"
                       r"HOUSING|AUTHORITY|COUNTY|CITY OF|STATE OF|UNITED STATES|SECRETARY|FEDERAL|NATIONAL)\b", re.I)
# A person's estate. "Real Estate" is a business word, and an estate company (Estate Holdings LLC) is a company.
ESTATE_RX = re.compile(r"\b(ESTATE OF|EST OF|HEIRS OF|UNKNOWN HEIRS|DECEASED|DECD|DEC'D)\b"
                       r"|\bHEIRS\b(?!\s+(?:LLC|INC|HOLDINGS|GROUP|PROPERTIES))"
                       r"|(?<!REAL )\bESTATE\b(?!\s+(?:LLC|INC|HOLDINGS|MANAGEMENT|INVEST\w*|GROUP|CO\b|COMPANY|PARTNERS|SERVICES|"
                       r"SOLUTIONS|DEVELOPMENT|PROPERTIES|ACADEMIC|BUYERS|SALES|AGENCY|LAW))", re.I)
COMPANY_RX = re.compile(r"\b(LLC|L L C|INC|CORP|CORPORATION|COMPANY|LP|LLP|LTD)\b", re.I)
LIFE_EST_RX = re.compile(r"\bLIFE\s+EST(ATE)?\b|\(LE\)|\bL/E\b", re.I)
TRUST_RX = re.compile(r"\bTRUST\b|\bTRUSTEE|\bTRS\b|\bTR\b", re.I)
# Who paid a tax bill: a channel (lockbox, online) says nothing about the person.
CHANNEL_RX = re.compile(r"^(LOCKBOX|ONLINE|ON LINE|REMITTANCE|BANK DRAFT|DRAFT|CASH|CHECK|MAIL|WALK ?IN|COUNTER|EFT|ACH|"
                        r"CREDIT CARD|E-?CHECK|KIOSK|PAYMENTS?|IVR|PHONE|WEB|INTERNET|TRUSTEE)\b", re.I)
LENDER_RX = re.compile(r"BANK|MORTGAGE|MTG|LOAN|SERVIC|ESCROW|CORELOGIC|LERETA|FINANCIAL|CREDIT UNION|FEDERAL|LENDING|"
                       r"TAX SERV|FARGO|CHASE|TRUIST|REGIONS|PENNYMAC|NEWREZ|ROCKET|LAKEVIEW|CARRINGTON|NATIONSTAR|"
                       r"FREEDOM|GUILD|SAVINGS|PNC|MIDLAND|SHELLPOINT|SPECIALIZED|CENLAR|SELECT PORTFOLIO|DOVENMUEHLE|"
                       r"LOANCARE|FLAGSTAR|AMERIHOME|CROSSCOUNTRY|UNITED WHOLESALE|TITLE|CORP|LLC|INC|HOMEBRIDGE", re.I)
# A first name never matches its feminine form: ERNEST is not ERNESTINE, PAUL is not PAULA.
FEM = {"A", "INE", "ETTE", "ELLE", "ENE", "NA", "IA", "ICE", "ITA", "ELLA", "INA", "ETTA", "ANNE", "ANN", "ENA", "ISE"}
GENERATIONS = {"JR", "SR", "II", "III", "IV"}

STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA", "colorado": "CO",
    "connecticut": "CT", "delaware": "DE", "district of columbia": "DC", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS", "kentucky": "KY",
    "louisiana": "LA", "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV", "new hampshire": "NH",
    "new jersey": "NJ", "new mexico": "NM", "new york": "NY", "north carolina": "NC", "north dakota": "ND",
    "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI",
    "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT",
    "virginia": "VA", "washington": "WA", "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
}
STATE_NAMES = {v: k.title() for k, v in STATES.items()}


def toks(name) -> list[str]:
    """The words of a name, upper case, with the generation (Jr, Sr, II) taken off."""
    clean = re.sub(r"[^A-Za-z\s\-]", " ", str(name or "")).upper().replace("-", " ")
    return [t for t in clean.split() if t not in SUFFIX]


def name_toks(name) -> list[str]:
    """toks() without the words a deed or an obituary adds (trust, estate, heirs, of)."""
    return [t for t in toks(name) if t not in NAME_NOISE]


def gens(name) -> set[str]:
    """The generation words in a name: JR and SR are two people."""
    return {w for w in re.findall(r"[A-Za-z]+", str(name or "").upper()) if w in GENERATIONS}


def first_ok(a: str, b: str) -> bool:
    """Same first name: equal, or one a 4+ letter stem of the other (JACK, JACKSON), never a gendered form."""
    if a == b:
        return True
    s, longer = (a, b) if len(a) <= len(b) else (b, a)
    return len(s) >= 4 and longer.startswith(s) and longer[len(s):] not in FEM


def first_on(first: str, owner_t) -> bool:
    return any(first_ok(first, t) for t in owner_t)


def person_on(person, owner_str) -> bool:
    """The person's surname AND first name are both in the owner string (a deed or a roll owner)."""
    p = [t for t in toks(person) if t not in NOISE]
    if len(p) < 2:
        return False
    owner_t = set(toks(owner_str))
    surnames = {p[-1]} | ({p[-2]} if len(p) >= 4 else set())
    if not (surnames & owner_t):
        return False
    return first_on(p[0], owner_t)


def same_person(a, b) -> bool:
    """Two written names are one person: same surname, same first name (or its stem)."""
    x, y = name_toks(a), name_toks(b)
    if len(x) < 2 or len(y) < 2:
        return False
    return x[-1] in y and any(first_ok(x[0], t) for t in y if t != x[-1])


def is_estate(name) -> bool:
    n = str(name or "")
    return bool(ESTATE_RX.search(n)) and not COMPANY_RX.search(n)


def is_entity(name) -> bool:
    n = str(name or "")
    return bool(ENTITY_RX.search(n)) and not is_estate(n)


def roll_people(owner) -> tuple[list[str], bool]:
    """A county roll owner in LAST FIRST form split into people, and whether it says et al.
    'SMITH JOHN A & MARY B' -> ['SMITH JOHN A', 'MARY B SMITH']."""
    s = re.sub(r"\s+", " ", str(owner or "")).strip()
    if not s:
        return [], False
    etal_rx = r"\bET\s*AL\b|\bETAL\b|\bETUX\b|\bETVIR\b"
    etal = bool(re.search(etal_rx, s, re.I))
    s = re.sub(etal_rx, "", s, flags=re.I)
    parts = [p.strip(" ,") for p in re.split(r"\s*&\s*|\s+AND\s+", s) if p.strip(" ,")]
    if not parts:
        return [], etal
    # "SAMPLE ANNE MARIE SAMPLE JOHN LEE": two people with no '&', the surname said twice
    w = parts[0].split()
    if len(w) >= 4 and w[0].isalpha() and w[0] in w[2:]:
        j = w.index(w[0], 2)
        parts = [" ".join(w[:j]), " ".join(w[j:])] + parts[1:]
    t0 = toks(parts[0])
    sur = t0[0] if t0 else ""
    out = [parts[0]]
    for p in parts[1:]:
        t = toks(p)
        out.append(p if len(t) >= 3 and sur not in t and len(t[-1]) > 1 else (p + " " + sur).strip())
    return out, etal


def split_name(name) -> tuple[str, str, str] | None:
    """'John Q Sample' -> ('John', 'Q', 'Sample'). A spelled-out middle name is dropped: a skip trace misses
    more often with it than without it."""
    w = [x for x in re.sub(r"[^A-Za-z\s\-']", " ", str(name or "")).replace("-", " ").split()
         if x.upper() not in SUFFIX and x.upper() not in NAME_NOISE]
    if len(w) < 2:
        return None
    first, last = w[0], w[-1]
    mid = w[1] if len(w) >= 3 and len(w[1]) == 1 else ""

    def t(s: str) -> str:
        return s.capitalize() if s.isupper() or s.islower() else s
    return t(first), mid.upper(), t(last)


def split_roll_name(name) -> tuple[str, str, str] | None:
    """A county roll writes LAST FIRST M: 'SAMPLE JOHN Q' -> ('John', 'Q', 'Sample')."""
    w = [x for x in re.sub(r"[^A-Za-z\s\-']", " ", str(name or "")).replace("-", " ").split()
         if x.upper() not in SUFFIX and x.upper() not in NAME_NOISE]
    if len(w) < 2:
        return None
    mid = w[2] if len(w) >= 3 and len(w[2]) == 1 else ""
    return w[1].capitalize(), mid.upper(), w[0].capitalize()


def nice(name) -> str:
    """A name or street in plain capitals: SAMPLE JOHN -> Sample John (two-letter all-caps words kept)."""
    out = []
    for w in str(name or "").split():
        if len(w) <= 2 and w.isupper() and not w.isalpha():
            out.append(w)
        elif w.isupper() or w.islower():
            out.append(w.capitalize())
        else:
            out.append(w)
    return " ".join(out)


def state_code(s) -> str:
    v = str(s or "").strip()
    if len(v) == 2 and v.isalpha():
        return v.upper()
    return STATES.get(v.lower(), "")


# ------------------------------------------------------------------------------------------- dates
def today() -> date:
    """The run's day. CURATIVE_TODAY=YYYY-MM-DD pins it (tests, and re-running a past day's work)."""
    pinned = os.environ.get("CURATIVE_TODAY", "").strip()
    if pinned:
        d = parse_day(pinned)
        if d:
            return d
    return date.today()


def parse_day(v) -> date | None:
    s = str(v or "").strip()
    if not s:
        return None
    for cand in (s, s[:10], s[:19]):
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y-%m-%dT%H:%M:%S", "%B %d, %Y", "%b %d, %Y"):
            try:
                return datetime.strptime(cand, fmt).date()
            except ValueError:
                continue
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


def from_epoch(v) -> date | None:
    """A date from epoch seconds or milliseconds (SiftMap and DataSift use both), or from a date string."""
    if v in (None, ""):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return parse_day(v)
    if x <= 0:
        return None
    if x > 1e11:
        x /= 1000.0
    try:
        return datetime.fromtimestamp(x, tz=timezone.utc).date()
    except (OverflowError, OSError, ValueError):
        return None


def months_between(a: date | None, b: date | None = None) -> float | None:
    if not a:
        return None
    b = b or today()
    return round((b - a).days / 30.44, 1)


def iso(d) -> str | None:
    return d.isoformat() if isinstance(d, date) else (str(d) if d else None)


def fmt_day(s) -> str:
    d = parse_day(s)
    return d.strftime("%b %d, %Y").replace(" 0", " ") if d else (str(s) if s else "")


# ------------------------------------------------------------------------------------------- money
def money(v) -> float | None:
    if v in (None, ""):
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(re.sub(r"[^\d.\-]", "", str(v)))
    except ValueError:
        return None


def dollars(v) -> str:
    return "$" + format(int(round(v)), ",") if isinstance(v, (int, float)) else ""


# ------------------------------------------------------------------------------------------- places
def slug(text) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", str(text or "").lower())).strip("-")


_ABBR = {"STREET": "ST", "AVENUE": "AVE", "ROAD": "RD", "DRIVE": "DR", "LANE": "LN", "COURT": "CT", "CIRCLE": "CIR",
         "BOULEVARD": "BLVD", "PLACE": "PL", "TRAIL": "TRL", "PARKWAY": "PKWY", "HIGHWAY": "HWY", "TERRACE": "TER",
         "NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W", "PK": "PIKE"}


def street_key(street, zip5="") -> str:
    s = re.sub(r"[^A-Z0-9 ]", " ", str(street or "").upper())
    return " ".join(_ABBR.get(x, x) for x in s.split()) + ("|" + str(zip5)[:5] if zip5 else "")


def digits(s) -> str:
    d = re.sub(r"\D", "", str(s or ""))
    return d[-10:] if len(d) >= 10 else ""


# ------------------------------------------------------------------------------------------- files
def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path, data, indent: int | None = 1) -> Path:
    """Write through a temp file, so a run that dies mid-write never leaves half a file."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=indent, default=str), encoding="utf-8", newline="\n")
    os.replace(tmp, p)
    return p


class Workspace:
    """One market's folders. The default is output/curative_title/<market>/ in the repo (gitignored: it holds
    owner and family names). CURATIVE_DIR moves the whole thing.

        work/      everything read and scored (records.json, county.json, scored.json, family.json ...)
        stories/   the heir research: houses/<slug>/{input.json, heirs.json, story.md}, stories_results.json
    """

    def __init__(self, market: str, base=None):
        self.market = slug(market) or "market"
        root = Path(base or os.environ.get("CURATIVE_DIR") or (ROOT / "output" / "curative_title"))
        self.root = root / self.market
        self.work = self.root / "work"
        self.stories = self.root / "stories"
        self.houses = self.stories / "houses"

    def path(self, name: str) -> Path:
        return self.work / name

    def read(self, name: str, default=None):
        return read_json(self.work / name, default)

    def write(self, name: str, data, indent: int | None = 1) -> Path:
        return write_json(self.work / name, data, indent)

    def settings(self) -> dict:
        return read_json(self.work / "settings.json", {}) or {}

    def save_settings(self, s: dict) -> None:
        write_json(self.work / "settings.json", s)


def load_env() -> dict:
    """The repo's .env as a dict (without exporting anything), with the process environment on top."""
    out: dict = {}
    p = ROOT / ".env"
    if p.exists():
        for ln in p.read_text(encoding="utf-8", errors="replace").splitlines():
            if not ln.strip() or ln.lstrip().startswith("#") or "=" not in ln:
                continue
            k, _, v = ln.partition("=")
            out[k.strip()] = v.strip().strip('"').strip("'")
    out.update({k: v for k, v in os.environ.items() if v})
    return out

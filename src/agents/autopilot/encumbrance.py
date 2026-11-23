"""Encumbrances filled the moment a record becomes a lead (Ty, 2026-11-15: "Encumbrance fields filled
automatically when a record becomes a lead: county tax owed, open liens from our lien records, and
the mortgage").

The lesson it automates (8-05, the intake structure): "Fill the encumbrance fields: mortgage balance,
months behind, back taxes (Unknown until checked) and the back taxes amount, HOA, and code violation
liens." Done by hand it was asked on the first call or not at all, and a lead manager making an
offer had no idea whether the county was owed two years of tax.

Each run:
  1. the shared log copy is read for records that ENTERED a lead status since the stage's mark: a
     change into one of the lead statuses from a status that is not a lead at all. New Lead to Warm
     Lead is the same lead; a record that left and came back is a new one, and is checked again;
  2. each, a capped number a run and oldest first, is read in full with its custom fields, and has
     to pass the gate's research check (Do Not Market, an opt-out, a litigator or a sold house hold
     it) and be owned by a person (the owner's rule, 2026-11-14);
  3. each encumbrance is worked out with where it came from and when:
       back taxes    DELINQUENT county tax from the Trustee's own bills, by street (Knox, Blount)
       liens         what our lien records already wrote on the record
       mortgage      open mortgages, the loan type, and what the value estimate and equity imply
       months behind, HOA, code liens   Unknown, unless a field on the record says otherwise
  4. live: Tax delinquency amount is written, a note "Encumbrances" goes on the record in one fixed
     format (each item with its source and date, Unknown where nothing was checked), and the intake
     fields that exist in the account are filled. Every change keeps its old value for undo.

Rules, each one a way this has gone wrong before:
  * `amount_due` is NOT a delinquency. A parcel row's amount due is every unpaid bill, the current
    year's included, which is not late until the end of February; read as a delinquency it marked
    1,274 of 1,451 lien parcels tax delinquent in November. Only bills that are both delinquent and
    unpaid count, read from the account's own bills.
  * Total Delinquency is liens PLUS taxes (13,766.72 = 12,908.72 lien + 858.00 tax on one record).
    It is never written here: the lien records own it, and writing the tax figure into it would
    erase the lien half. Tax delinquency amount is the tax figure's own field.
  * The county search is literal about the suffix and fuzzy in what it ranks first, so the street is
    asked the way the county writes it and a parcel counts only when its own address is the same
    house (the auction lane's county client, reused here). 3 of 51 records once carried another
    house's bill.
  * An account whose bills cannot be read gets a BLANK: the field is left as it is, the note says
    Unknown and why, and a problem line says it out loud. A guess reads exactly like a fact.
  * COUNTY tax only. The City of Knoxville, Maryville and Alcoa bill separately; the note says so.
  * A person's answer wins: an intake field that already holds something is never overwritten. The
    tax field is the exception (settings: overwrite), because the county's bills on the day the
    record became a lead are fresher than the figure the upload wrote months before.
  * An estimate never goes into a money field. "About $96,000 owed" from a value estimate and an
    equity percent is useful in a note and in a text field; in a money field it reads like a payoff.
  * Plan and live keep separate memories and marks: a plan run checks each new lead once and says
    what it would write, and the first live run still writes them.
"""
from __future__ import annotations

import logging
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import store
from . import activity, budget, lanes, leadclock, ledger
from .auctiontasks import COUNTY_ROLLS
from .stages import Ctx, addr, marks_of
from .taxwatch import NoRoll, Unreadable, _fetch, call_lookup, county_tax, delinquent, lookup_owner  # noqa: F401  (the shared county client)

SRC = Path(__file__).resolve().parents[2]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

log = logging.getLogger(__name__)

KV = "autopilot:encumbrance:%s:through"            # the live mark, moved only by live runs
KV_PLAN = "autopilot:encumbrance:%s:plan_through"  # a plan run's own mark
MEMO = "encumbrance"                               # per record, live runs only: {"entry", "done", "tries", ...}
MEMO_PLAN = "encumbrance_plan"                     # per record, plan runs only
TAX_LABEL = "Tax delinquency amount"
TOTAL_LABEL = "Total Delinquency"                  # liens plus taxes: never written here
LIEN_AMOUNT, LIEN_COUNT, LIEN_TYPE = "Outstanding Lien Amount", "Liens Active vs Total", "Lien Type"
# key -> the custom field it fills. Only Tax delinquency amount exists in the account today (2026-11-15);
# the other four are created by a person, and until then their values go into the note only.
FIELDS = {"tax": TAX_LABEL, "mortgage_balance": "Mortgage Balance", "months_behind": "Months Behind",
          "hoa": "HOA", "code_liens": "Code Liens"}
DEFAULTS = {"first_window_days": 14, "max_records": 15, "max_tries": 3, "note": True, "sync": True}
ORDER = ("tax", "liens", "mortgage_balance", "months_behind", "hoa", "code_liens")
LABELS = {"tax": "Back taxes", "liens": "Liens", "mortgage_balance": "Mortgage", "months_behind": "Months behind",
          "hoa": "HOA", "code_liens": "Code liens"}
NOTE_HEAD = "Encumbrances"
TEXT_TYPES = {"text", "text_input"}
NUMBER_TYPES = {"money", "number"}
LIEN_COUNTIES = {"knox"}            # our lien records come from the Knox Register of Deeds only
CODE_MARKS = ("condemned", "code violation")
HOA_RX = re.compile(r"\bhoa\b|home ?owners'? association|owners'? association", re.I)
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
# ---------------------------------------------------------------------- settings
def settings(st) -> dict:
    out = dict(DEFAULTS)
    for k in DEFAULTS:
        v = st.get(k)
        if v is not None:
            out[k] = v
    out["lead_statuses"] = [str(s) for s in (st.get("lead_statuses") or lanes.LEAD_STATUS_VALUES)]
    fields = dict(FIELDS)
    for k, v in (st.get("fields") or {}).items():
        fields[str(k)] = str(v).strip() if v else ""
    out["fields"] = fields
    out["overwrite"] = [str(x) for x in (st.get("overwrite") if st.get("overwrite") is not None else ["tax"])]
    return out


def block_problems(st) -> list[str]:
    """What is wrong with an encumbrance stage's block, in the playbook checker's words, so a typo is
    refused when the playbook loads instead of read as a default on the first live morning."""
    out = []
    ls = st.get("lead_statuses")
    if ls is not None and (not isinstance(ls, list) or not ls or not all(isinstance(x, str) and x.strip() for x in ls)):
        out.append("stage %s: lead_statuses lists the statuses that make a record a lead, as the account spells them" % st.id)
    f = st.get("fields")
    if f is not None:
        if not isinstance(f, dict):
            out.append("stage %s: fields maps %s to custom field labels" % (st.id, ", ".join(FIELDS)))
        else:
            bad = sorted(str(k) for k in f if str(k) not in FIELDS)
            if bad:
                out.append("stage %s: fields names %s; the keys are %s" % (st.id, bad, ", ".join(FIELDS)))
            if any(v and str(v).strip().lower() == TOTAL_LABEL.lower() for v in f.values()):
                out.append("stage %s: fields may not name Total Delinquency (it is liens plus taxes, kept by the lien records)" % st.id)
    ow = st.get("overwrite")
    if ow is not None and (not isinstance(ow, list) or any(str(x) not in FIELDS for x in ow)):
        out.append("stage %s: overwrite lists the field keys a fresh value may replace (%s)" % (st.id, ", ".join(FIELDS)))
    for key in ("first_window_days", "max_records", "max_tries"):
        v = st.get(key)
        if v is not None and (isinstance(v, bool) or not isinstance(v, int) or v < 1):
            out.append("stage %s: %s must be a whole number, 1 or more" % (st.id, key))
    for key in ("note", "sync"):
        v = st.get(key)
        if v is not None and not isinstance(v, bool):
            out.append("stage %s: %s is true or false" % (st.id, key))
    return out


# ---------------------------------------------------------------------- small readers
def _num(v) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = "".join(ch for ch in str(v) if ch.isdigit() or ch in ".-")
    try:
        return float(s) if s not in ("", ".", "-") else None
    except ValueError:
        return None


def _int(v) -> int | None:
    n = _num(v)
    return None if n is None else int(n)


def _money(n: float) -> str:
    """$1,234.56, or $96,000 for a whole figure."""
    return "$" + (format(n, ",.0f") if abs(n - round(n)) < 0.005 else format(n, ",.2f"))


def _figure(n: float) -> str:
    """A figure the way a money field takes it: "1234.56", "0"."""
    s = "%.2f" % n
    return s.rstrip("0").rstrip(".") if "." in s else s


def _rounded(n: float) -> float:
    return float(round(n / 1000.0) * 1000) if abs(n) >= 10000 else float(round(n / 100.0) * 100)


def _day(dt: datetime, tz: str) -> str:
    return dt.astimezone(ZoneInfo(tz)).strftime("%b %d, %Y").replace(" 0", " ")


def _date_text(v) -> str:
    s = str(v or "")[:10]
    try:
        return datetime.strptime(s, "%Y-%m-%d").strftime("%b %d, %Y").replace(" 0", " ")
    except ValueError:
        return ""


def _and(words: list) -> str:
    """"2024", "2023 and 2024", "2022, 2023 and 2024"."""
    words = [str(w) for w in words if str(w)]
    if len(words) <= 1:
        return "".join(words)
    return ", ".join(words[:-1]) + " and " + words[-1]


def _not_uuid(v) -> str:
    """A select field can read back as its option's id, which means nothing in a note."""
    s = str(v or "").strip()
    return "" if not s or _UUID.match(s) else s


def county_key(county: str) -> str:
    return str(county or "").strip().lower().replace(" county", "").strip()


def county_name(county: str) -> str:
    k = county_key(county)
    return ("%s County" % k.title()) if k else "the county"


# ---------------------------------------------------------------------- the county roll
# The county client is taxwatch's (one client for both stages): it tries every account at the house,
# passes over a business account whose bills route answers 404 (which used to hide the real bill),
# and refuses two accounts that both answer rather than guess between them.


def _tax(ctx: Ctx, rec: dict, state: dict) -> dict:
    """The back taxes for one record: {"status": ok | none | unwired | unreadable | down, ...}."""
    a = rec.get("address") or {}
    street, county = str(a.get("street") or "").strip(), str(a.get("county") or "")
    name = county_name(county)
    if not street:
        return {"status": "none", "why": "The record has no street to look up"}
    if not county_key(county):
        return {"status": "unwired", "why": "The record names no county, so no county roll was read"}
    if county_key(county) not in COUNTY_ROLLS:
        return {"status": "unwired", "why": "No county tax roll is read for %s" % name}
    if state.get("county_down"):
        return {"status": "down", "tried": False, "why": state["county_down"]}
    lookup = (ctx.sources or {}).get("county_tax") or county_tax
    try:
        found = call_lookup(lookup, street, county, lookup_owner(rec))
    except budget.BudgetExhausted:
        raise
    except NoRoll as exc:
        return {"status": "unwired", "why": str(exc)[:1].upper() + str(exc)[1:]}
    except Unreadable as exc:
        return {"status": "unreadable", "why": "The %s Trustee's bills could not be read (%s)" % (name, str(exc)[:80])}
    except OSError as exc:
        state["county_down"] = str(exc)[:80]
        ctx.out.problem("the county tax site did not answer (%s); the new leads waiting on it are tried again next run"
                        % str(exc)[:80])
        return {"status": "down", "tried": True, "why": str(exc)[:80]}
    except Exception as exc:  # noqa: BLE001  (one odd answer must not stop the run: said, and left Unknown)
        return {"status": "unreadable", "why": "The %s Trustee's answer could not be read (%s)" % (name, type(exc).__name__)}
    if found is None:
        return {"status": "none", "why": "The %s Trustee's roll has no parcel at %s" % (name, street)}
    return {"status": "ok", "amount": float(_num(found.get("amount")) or 0.0), "years": list(found.get("years") or []),
            "account": str(found.get("account") or ""), "source": "%s Trustee bills" % name}


# ---------------------------------------------------------------------- the encumbrances
def _item(key: str, value: str = "", source: str = "", why: str = "", field: dict | None = None,
          short: str = "") -> dict:
    """One encumbrance. `value` and `source` when it was checked; `why` when it is Unknown. `field`
    is what may go into its custom field: text (always), number (a figure of record), choice (a
    select option's label) and flag (a yes or no); None when nothing may be written. `short` is how
    the run's one line per lead says it."""
    plain = (value[:1].lower() + value[1:]) if value else "unknown"
    return {"key": key, "label": LABELS[key], "known": bool(value), "value": value or "Unknown", "source": source,
            "why": why, "field": field, "short": short or plain}


def tax_item(tax: dict) -> dict:
    if tax.get("status") != "ok":
        return _item("tax", why=tax.get("why") or "Not checked")
    amt, years = float(tax.get("amount") or 0.0), tax.get("years") or []
    if amt > 0:
        value = "%s delinquent county tax%s" % (_money(amt), (" for %s" % _and(years)) if years else "")
        short = "%s%s" % (_money(amt), (" for %s" % _and(years)) if years else "")
    else:
        value, short = "None delinquent", "none delinquent"
    return _item("tax", value=value, source="%s (county tax only; a city bill is separate)" % tax["source"],
                 field={"text": _money(amt) if amt > 0 else "None delinquent", "number": amt,
                        "choice": "Yes" if amt > 0 else "No", "flag": amt > 0}, short=short)


def lien_item(rec: dict, custom: dict) -> dict:
    have = {str(k).strip().lower(): v for k, v in (custom or {}).items()}
    amount = _num(have.get(LIEN_AMOUNT.lower()))
    count = str(have.get(LIEN_COUNT.lower()) or "").strip()
    kind = str(rec.get("lien_type") or "").strip() or _not_uuid(have.get(LIEN_TYPE.lower()))
    recorded = _date_text(rec.get("lien_recording_date"))
    if amount is None and not count and not kind and not recorded:
        if county_key((rec.get("address") or {}).get("county")) in LIEN_COUNTIES:
            return _item("liens", value="None on our lien records", source="our lien records, as written on the record")
        return _item("liens", why="Our lien records cover Knox County only")
    parts = []
    if amount is not None:
        parts.append("%s outstanding" % _money(amount))
    if count:
        parts.append("%s active" % count)
    if kind:
        parts.append("type %s" % kind)
    if recorded:
        parts.append("recorded %s" % recorded)
    return _item("liens", value=", ".join(parts), source="our lien records, as written on the record",
                 short=", ".join(parts[:2]))


def mortgage_item(rec: dict) -> dict:
    n = _int(rec.get("open_mortgages"))
    kind = str(rec.get("mortgage_type") or "").strip()
    value, eq, ltv = _num(rec.get("estimate_value")), _num(rec.get("equity_percent")), _num(rec.get("loan_to_value"))
    if n == 0 or (n is None and eq is not None and eq >= 100):
        # No mortgage of record is free and clear (Ty): a balance of zero is a fact of record, not an estimate.
        return _item("mortgage_balance", value="No open mortgage of record (free and clear)",
                     source="the record (%s)" % ("0 open mortgages" if n == 0 else "%s equity" % _pct(eq)),
                     field={"text": "None of record", "number": 0.0, "choice": "None", "flag": False},
                     short="free and clear")
    owed, how = None, ""
    if value and eq is not None:
        owed, how = value * (100.0 - eq) / 100.0, "%s value estimate and %s equity" % (_money(value), _pct(eq))
    elif value and ltv:
        owed, how = value * ltv / 100.0, "%s value estimate and %s loan to value" % (_money(value), _pct(ltv))
    if n is None and owed is None and not kind:
        return _item("mortgage_balance", why="The record carries no mortgage data",
                     field={"text": "Unknown", "number": None, "choice": "Unknown", "flag": None})
    head = ("%d open mortgage%s" % (n, "" if n == 1 else "s")) if n else ("a mortgage" if kind else "")
    if kind and head:
        head += " (%s)" % kind
    parts = [head] if head else []
    if owed is not None:
        about = _money(_rounded(owed))
        parts.append("about %s owed%s" % (about, ", more than the value estimate" if value and owed > value else ""))
    text = ", ".join(parts)
    text = text[:1].upper() + text[1:]
    source = "the record's %s (an estimate, not a payoff)" % how if how else "the record"
    est = ("About %s (estimate, not a payoff)" % _money(_rounded(owed))) if owed is not None else "Unknown"
    return _item("mortgage_balance", value=text, source=source,
                 field={"text": est, "number": None, "choice": "Unknown", "flag": None},
                 short=("about %s owed" % _money(_rounded(owed))) if owed is not None else "")


def _pct(n) -> str:
    return ("%s%%" % format(n, ".0f")) if n is not None else ""


def months_item(rec: dict) -> dict:
    foreclosure = bool(rec.get("foreclosure_date") or rec.get("auction_date")) or "foreclosure" in marks_of(rec)
    why = "Ask the seller" + ("; a foreclosure notice is on the record, so the loan is behind" if foreclosure else "")
    return _item("months_behind", why=why, field={"text": "Unknown", "number": None, "choice": "Unknown", "flag": None})


def hoa_item(rec: dict, custom: dict) -> dict:
    have = {str(k).strip().lower(): v for k, v in (custom or {}).items()}
    said = [str(rec.get("lien_type") or ""), _not_uuid(have.get(LIEN_TYPE.lower()))] + sorted(marks_of(rec))
    if any(HOA_RX.search(s) for s in said if s):
        return _item("hoa", value="An HOA lien is on our lien records", source="our lien records, as written on the record",
                     field={"text": "Yes, an HOA lien", "number": None, "choice": "Yes", "flag": True})
    return _item("hoa", why="Not checked", field={"text": "Unknown", "number": None, "choice": "Unknown", "flag": None})


def code_item(rec: dict) -> dict:
    """A code action is named when the record carries one (the Condemned list, a Code Violation list,
    or the uploader's `code_violation` tag); the lien itself is only ever confirmed at title."""
    hit = sorted(m.replace("_", " ") for m in marks_of(rec) if any(c in m.replace("_", " ") for c in CODE_MARKS))
    if hit:
        return _item("code_liens", why="A code action is on the record (%s); a lien, if any, is confirmed at title" % hit[0].title(),
                     field={"text": "Check at title: a code action is on the record", "number": None, "choice": "Unknown",
                            "flag": None})
    return _item("code_liens", why="Confirmed at title", field={"text": "Unknown", "number": None, "choice": "Unknown",
                                                                "flag": None})


def encumbrances(rec: dict, custom: dict, tax: dict) -> list[dict]:
    """Every item, in the note's fixed order."""
    got = {"tax": tax_item(tax), "liens": lien_item(rec, custom), "mortgage_balance": mortgage_item(rec),
           "months_behind": months_item(rec), "hoa": hoa_item(rec, custom), "code_liens": code_item(rec)}
    return [got[k] for k in ORDER]


def note_text(items: list[dict], now: datetime, tz: str) -> str:
    """The note in its one fixed format: a head line, then one line per item, always in the same order.
    A checked item says what was found, its source and the day it was read; one that was not checked
    says Unknown and why."""
    day = _day(now, tz)
    lines = ["%s, filled when this became a lead (%s):" % (NOTE_HEAD, day)]
    for it in items:
        if it["known"]:
            lines.append("%s: %s. Source: %s, %s." % (it["label"], it["value"], it["source"], day))
        else:
            lines.append("%s: Unknown. %s." % (it["label"], it["why"]))
    return "\n".join(lines)


def summary_line(rec: dict, items: list[dict]) -> str:
    """One line per lead for the run: "101 Example St, Knoxville: back taxes $1,234.56 for 2024;
    liens $12,908.72 outstanding, 3 of 8 active; mortgage about $96,000 owed"."""
    by = {it["key"]: it["short"] for it in items}
    return "%s: back taxes %s; liens %s; mortgage %s" % (addr(rec), by["tax"], by["liens"], by["mortgage_balance"])


# ---------------------------------------------------------------------- the custom fields
def field_index(crm) -> dict:
    """label (lower case) -> {"uuid", "type", "options"} for the account's property custom fields, read
    once a run (GET /api/internal/custom-fields/, documented). The type decides what may be written."""
    r = crm.call("/api/internal/custom-fields/?limit=999")
    rows = r if isinstance(r, list) else ((r or {}).get("results") or [])
    out = {}
    for row in rows:
        if not isinstance(row, dict) or str(row.get("entity_type") or "property") != "property" or row.get("is_active") is False:
            continue
        label = str(row.get("label") or "").strip().lower()
        if label and row.get("uuid"):
            opts = {str(o.get("label") or "").strip().lower(): str(o.get("uuid") or "")
                    for o in row.get("options") or [] if isinstance(o, dict) and o.get("uuid")}
            out[label] = {"uuid": str(row["uuid"]), "type": str(row.get("field_type") or "text"), "options": opts}
    return out


def value_for(field: dict, want: dict):
    """What goes into one field, or None when nothing fits its type: text into a text field, only a
    figure of record into a money or number field, a matching option into a select, a yes or no into
    a boolean. A date, a url, a phone or a multiselect field is never written here."""
    t = field.get("type")
    if t in TEXT_TYPES:
        return want.get("text") or None
    if t in NUMBER_TYPES:
        return None if want.get("number") is None else _figure(float(want["number"]))
    if t == "select":
        return (field.get("options") or {}).get(str(want.get("choice") or "").strip().lower()) or None
    if t == "boolean":
        return None if want.get("flag") is None else bool(want["flag"])
    return None


def _same(before, after) -> bool:
    """Does the field already read what would be written? A money field reads back 858.0 for 858."""
    if isinstance(before, bool) or isinstance(after, bool):
        return before == after
    a, b = _num(before), _num(after)
    if a is not None and b is not None:
        return abs(a - b) < 0.005
    return str(before).strip().lower() == str(after).strip().lower()


def _write(ctx: Ctx, st: dict, rec: dict, custom: dict, items: list[dict], state: dict) -> list[str]:
    """The writes for one record, each through ctx.write with its before-value kept for undo. Returns
    what was (or, in plan mode, would be) written."""
    u = rec["uuid"]
    have = {str(k).strip().lower(): v for k, v in (custom or {}).items()}
    wrote: list[str] = []
    if state["fields"] is None and not state["fields_failed"]:
        try:
            state["fields"] = field_index(ctx.crm)
        except budget.BudgetExhausted:
            raise
        except Exception as exc:  # noqa: BLE001  (the note still carries every value)
            state["fields_failed"] = True
            ctx.out.problem("could not read the custom field list (%s), so no encumbrance field was written; "
                            "the notes still were" % str(exc)[:80])
    index = state["fields"] or {}
    by_key = {it["key"]: it for it in items}
    for key, label in st["fields"].items():
        if not label or key not in by_key:
            continue
        if label.strip().lower() == TOTAL_LABEL.lower():
            ctx.out.problem("the playbook points an encumbrance at Total Delinquency, which is liens plus taxes; it was not written")
            continue
        f = index.get(label.strip().lower())
        if f is None:
            if not state["fields_failed"] and label not in state["missing"]:
                state["missing"].append(label)
            continue
        want = by_key[key].get("field")
        if want is None:
            continue                     # nothing was learned (an unreadable account): left blank, never guessed
        value = value_for(f, want)
        if value is None:
            ctx.out.tally["values that do not fit their field's type (kept in the note)"] += 1
            continue
        before = have.get(label.strip().lower())
        if before not in (None, "") and key not in st["overwrite"]:
            ctx.out.tally["fields a person already filled (kept)"] += 1
            continue
        if before not in (None, "") and _same(before, value):
            ctx.out.tally["fields that already read right"] += 1
            continue
        ctx.change(u, "custom_field", {"field_uuid": f["uuid"], "value": before if before is not None else ""},
                   {"field_uuid": f["uuid"], "value": value})
        try:
            ctx.write("encumbrance field", ctx.crm.set_custom_field, u, f["uuid"], value)
        except budget.BudgetExhausted:
            raise
        except Exception as exc:  # noqa: BLE001  (one refused field must not cost the record its note)
            ctx.out.problem("DataSift refused a %s value on a new lead (%s)" % (label, str(exc)[:80]))
            continue
        wrote.append(label)
    if st["note"]:
        text = note_text(items, ctx.now, ctx.policy.tz)
        ctx.change(u, "note", None, {"text": text})
        try:
            ctx.write("encumbrance note", ctx.crm.message, u, text)
            wrote.append("note")
        except budget.BudgetExhausted:
            raise
        except Exception as exc:  # noqa: BLE001
            ctx.out.problem("could not add the encumbrance note to a new lead (%s)" % str(exc)[:80])
    return wrote


# ---------------------------------------------------------------------- the lead entries
def lead_entries(since: datetime, wanted: set) -> dict:
    """record uuid -> its latest ENTRY into a lead status since `since`: a change into one of `wanted`
    from a status that is no lead at all. A move from one lead status to another is the same lead."""
    leads_all = set(wanted) | lanes.LEAD_STATUSES
    out = {}
    for e in activity.events("property.status.updated", since=since):
        to = str(e["data"].get("to") or "").strip().lower()
        frm = str(e["data"].get("from") or "").strip().lower()
        if e["resource"] and to in wanted and frm not in leads_all:
            out[e["resource"]] = e["when"]           # oldest first, so the one kept is the latest entry
    return out


def _is_entity(owner: dict) -> bool:
    import deep_prospect_batch
    return deep_prospect_batch.is_entity(owner or {})


def _parse(v) -> datetime | None:
    try:
        return datetime.fromisoformat(str(v)) if v else None
    except ValueError:
        return None


# ---------------------------------------------------------------------- the run
def _one(ctx: Ctx, st: dict, u: str, entry: str, memo: dict, state: dict, leads_all: set) -> str:
    """One new lead: "done" (filled, or decided not to), or "wait" (try again next run)."""
    memo_key = MEMO if ctx.live else MEMO_PLAN
    tries = int(memo.get("tries") or 0) if memo.get("entry") == entry else 0

    def remember(**kw) -> None:
        ledger.set_fact(u, memo_key, dict({"entry": entry, "at": ctx.now.isoformat(timespec="seconds")}, **kw))

    def retry(why: str) -> str:
        n = tries + 1
        if n < int(st["max_tries"]):
            remember(done=False, tries=n)
            ctx.out.tally["new leads tried again next run"] += 1
            return "wait"
        remember(done=True, tries=n, skipped=why)
        ctx.out.problem("gave up on a new lead after %d tries: %s" % (n, why))
        return "done"

    try:
        rec = ctx.crm.record(u)
    except budget.BudgetExhausted:
        raise
    except Exception as exc:  # noqa: BLE001
        return retry("the record could not be read (%s)" % str(exc)[:60])
    if not rec or not rec.get("uuid"):
        return retry("the record could not be read")
    status = lanes.status_of(rec)
    if status not in leads_all:
        remember(done=True, skipped="no longer a lead (%s)" % (status or "no status"))
        ctx.out.tally["no longer a lead when it was checked"] += 1
        return "done"
    if _is_entity(rec.get("owner") or {}):
        remember(done=True, skipped="a company or a trust owns it")
        ctx.out.tally["owned by a company or a trust (people only)"] += 1
        return "done"
    try:
        custom = ctx.crm.custom_fields(u) or {}
    except budget.BudgetExhausted:
        raise
    except Exception as exc:  # noqa: BLE001
        return retry("its custom fields could not be read (%s)" % str(exc)[:60])
    ledger.set_fact(u, "custom_fields", custom)      # the gate's week-long cache, refreshed with what was just read
    if not ctx.passes(rec, "research"):
        remember(done=True, skipped="held by the gate")
        return "done"
    tax = _tax(ctx, rec, state)
    if tax["status"] == "down":
        if not tax.get("tried"):
            ctx.out.tally["new leads waiting for the county tax site"] += 1
            return "wait"                            # the site failed on an earlier lead this run: no try spent
        n = tries + 1
        if n < int(st["max_tries"]):
            remember(done=False, tries=n)
            ctx.out.tally["new leads waiting for the county tax site"] += 1
            return "wait"
        tax = {"status": "unreadable", "why": "The county tax site did not answer after %d tries" % n}
        ctx.out.problem("gave up on the county tax for %s after %d tries: its note says Unknown" % (addr(rec), n))
    elif tax["status"] == "unreadable":
        ctx.out.problem("the county tax bills for %s could not be read: back taxes left blank and Unknown" % addr(rec))
    items = encumbrances(rec, custom, tax)
    wrote = _write(ctx, st, rec, custom, items, state)
    found = {it["key"]: it["value"] for it in items}
    remember(done=True, tax=tax.get("status"), found=found, wrote=wrote)
    if ctx.live:
        ledger.event(ctx.run_id, ctx.stage.id, u, "encumbrance", {"entry": entry, "tax": tax.get("status"), "wrote": wrote},
                     now=ctx.now, tz=ctx.policy.tz)
    ctx.out.tally["back taxes %s" % {"ok": "read from the county", "none": "with no parcel at the street",
                                     "unwired": "in a county no roll is read for",
                                     "unreadable": "unreadable"}.get(tax["status"], tax["status"])] += 1
    ctx.out.line(summary_line(rec, items))
    state["filled"] += 1
    return "done"


def run_encumbrance(ctx: Ctx) -> None:
    st = settings(ctx.stage)
    wanted = {s.strip().lower() for s in st["lead_statuses"]}
    leads_all = wanted | lanes.LEAD_STATUSES
    if st["sync"]:
        leadclock.sync_logs(ctx, ("state",))
    mark_key = (KV if ctx.live else KV_PLAN) % ctx.stage.id
    memo_key = MEMO if ctx.live else MEMO_PLAN
    mark = _parse(store.kv_get(mark_key))
    since = (mark - activity.OVERLAP) if mark else ctx.now - timedelta(days=int(st["first_window_days"]))
    entries = sorted(lead_entries(since, wanted).items(), key=lambda kv: kv[1])
    state = {"county_down": "", "fields": None, "fields_failed": False, "missing": [], "filled": 0}
    pending: list[datetime] = []
    read = 0
    for u, when in entries:
        entry = when.isoformat(timespec="seconds")
        memo = ledger.fact(u, memo_key) or {}
        if memo.get("entry") == entry and memo.get("done"):
            continue                                  # this lead entry is done (the overlap re-reads a few hours)
        ctx.out.found += 1
        if read >= int(st["max_records"]):
            pending.append(when)
            ctx.out.tally["new leads waiting for the next run (read cap)"] += 1
            continue
        read += 1
        if _one(ctx, st, u, entry, memo, state, leads_all) == "wait":
            pending.append(when)
    if state["missing"]:
        ctx.out.problem("not in DataSift yet, so these went into the note only: %s. Create them as text fields and the "
                        "next new lead fills them" % ", ".join(state["missing"]))
    through = activity.synced_through("state") if st["sync"] else None
    through = min(through or ctx.now, ctx.now)
    store.kv_set(mark_key, (min(pending) if pending else through).isoformat(timespec="seconds"))
    ctx.out.acted = state["filled"]
    if not ctx.out.found:
        ctx.out.head = "No record became a lead since the last check."
        return
    head = "%s the encumbrances on %d of %d new lead(s)." % ("Filled" if ctx.live else "Would fill", state["filled"],
                                                            ctx.out.found)
    if pending:
        head += " %d left for the next run." % len(pending)
    ctx.out.head = head

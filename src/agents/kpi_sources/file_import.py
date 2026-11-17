"""Files as a KPI source: any CSV or Excel export with a column map, and the calling agency's files.

Most tools without a read API still export: a dialer's call history, a texting tool's campaign report, a report
scheduled by email. One adapter reads them all through a column map in the connection's settings, with the common
header names built in (the way agency_results.FIELDS reads an agency's export), so most files need no map at all.

Two kinds of file (settings `rows`):
  * ROWS are events (call, text, phone_status, mail, stage or spend, or a kind column that names one per row): one
    row, one act. A row's id column becomes its source id; a row with no id is known by a hash of its content, so a
    re-sent file, or a weekly export that overlaps last week's, adds nothing. A row with no usable date is left
    out and counted, never placed on the day it was imported.
  * TOTALS are tallies (rows: tally): a figure per period, per person when the file names one. A header that is a
    vendor's own word (connects, contacts, conversations, talk time, transfers, any rate or percent) is kept as
    as_reported under that word, never as one of ours. A re-sent report for the same period replaces its figure.

THE AGENCY (preset agency, a built-in connection). The agency results workflow reads the calling agency's
per-record files and keeps every one it parsed (DATA_DIR/agency_returns). This source reads those kept returns, so
the agency's history counts with no re-post: each number dialed is a call (export grain, the agency's caller as
the person), a number found wrong, dead, correct or asked to be removed is a phone status, and a lead passed is a
lead stage. A corrected file posted again replaces a house's rows with the newest file's (agency_results'
merge rule), so nothing counts twice. The agency's summary PDF becomes tallies: dials, leads, appointments and
money keep our names with the agency's own word as the definition; connects, transfers, talk time and every rate
are kept as reported. A row with no call date is placed on the month the agency called in, never on the day the
file was posted (the scorecard files it the same way).
"""
from __future__ import annotations

import csv
import hashlib
import io
import re
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .. import store
from ..autopilot.gate import digits
from . import (BORROWED, KINDS, STAGES, TZ, Adapter, Batch, Event, Provides, Tally, answered_of, direction_of, ensure,
               local_day, money_of, parse_when, save_events, save_tallies, seconds_of, setting, stored_events,
               stored_tallies, words)

HEADER_ROWS = 15
# Our field -> the header names exports use, first found wins. A connection's `columns` replaces a field's list.
COLUMNS = {
    "at": ("date time", "datetime", "call date", "call time", "call start", "start time", "started", "started at",
           "date", "time", "timestamp", "created", "created at", "sent at", "date sent", "sent date"),
    "tool_id": ("call id", "message id", "sms id", "event id", "id", "uuid"),
    "direction": ("direction", "call direction", "call type", "type"),
    "from": ("from", "from number", "from phone"),
    "to": ("to", "to number", "to phone"),
    "phone": ("phone", "phone number", "number", "number dialed", "phone dialed", "dialed number", "contact number",
              "customer number", "contact phone"),
    "line": ("line", "our number", "caller id", "caller id number", "sending number"),
    "seconds": ("duration", "talk time", "talk seconds", "seconds", "call duration", "call length", "length",
                "duration seconds", "billsec"),
    "answered": ("answered", "was answered"),
    "outcome": ("disposition", "dispo", "outcome", "call result", "result", "call outcome", "call status"),
    "status": ("status", "delivery status", "message status"),
    "person": ("agent email", "user email", "agent", "agent name", "user", "rep", "caller", "called by", "sent by",
               "email"),
    "record": ("record id", "record_id", "datasift id", "datasift record id", "sift record id", "record uuid"),
    "owner": ("owner id", "owner uuid"),
    "amount": ("amount", "cost", "price", "charge", "total"),
    "campaign": ("campaign", "campaign name", "list"),
    "stage": ("stage", "pipeline stage", "lead stage"),
}
# A column that names each row's kind is read only when the connection asks for it (kind_column, or columns.kind):
# a header like "Event" in an ordinary call history must never turn it into a mixed file.
KIND_COLUMN = "kind"
TOTAL_KEYS = {
    "period": ("date", "day", "period", "month", "report date"),
    "person": ("agent", "agent name", "user", "rep", "caller", "agent email", "user email", "email"),
}
METRIC_COLUMNS = {
    "dials": ("dials", "calls", "calls made", "outbound calls", "total calls", "total dials", "dial count"),
    "answered": ("answered", "answered calls", "calls answered"),
    "texts_sent": ("texts sent", "sms sent", "messages sent"),
    "replies": ("replies", "responses", "texts received", "inbound texts"),
    "mail_pieces": ("mail pieces", "pieces mailed", "pieces", "mailed"),
    "leads": ("leads", "new leads"),
    "appointments": ("appointments", "appts", "appointments set"),
    "offers": ("offers", "offers made"),
    "contracts": ("contracts", "contracts signed"),
    "closed": ("closed", "deals closed", "closings"),
    "correct_numbers": ("correct numbers", "right numbers", "verified numbers"),
    "spend": ("spend", "cost", "total cost", "amount spent"),
}
# Words that make a header the vendor's own figure, never ours.
BORROWED_HINTS = ("convers", "contact", "connect", "rate", "talk time", "transfer", "rpc", "right party", "percent")
KIND_WORDS = {"call": ("call", ""), "calls": ("call", ""), "dial": ("call", ""), "phone call": ("call", ""),
              "text": ("text", ""), "sms": ("text", ""), "message": ("text", ""), "phone status": ("phone_status", ""),
              "number status": ("phone_status", ""), "mail": ("mail", ""), "letter": ("mail", ""),
              "postcard": ("mail", ""), "spend": ("spend", ""), "charge": ("spend", ""), "lead": ("stage", "lead"),
              "new lead": ("stage", "lead"), "appointment": ("stage", "appointment"), "appt": ("stage", "appointment"),
              "offer": ("stage", "offer"), "contract": ("stage", "contract"), "under contract": ("stage", "contract"),
              "closed": ("stage", "closed"), "closing": ("stage", "closed"), "deal closed": ("stage", "closed")}
STAGE_WORDS = {w: st for w, (k, st) in KIND_WORDS.items() if k == "stage"}
PRESETS = ("", "agency")
# What an agency disposition class says about the call: someone spoke (True), nobody could (False), cannot say (None).
ANSWERED_CLASS = {"lead": True, "not_interested": True, "listed": True, "remove": True, "deceased": True,
                  "callback": True, "wrong": True, "person": True, "dead": False}
PHONE_FROM_CLASS = {"dnc": "dnc", "correct": "correct", "dead": "dead", "wrong": "wrong"}


def _decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def read_tables(name: str, data: bytes) -> list[tuple[str, list[list]]]:
    """(sheet name, rows) for every table in the file: each sheet of a workbook, or the one CSV. Excel cells keep
    their dates as dates, so a time of day is never lost to a string."""
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext in ("xlsx", "xlsm"):
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        try:
            return [(ws.title, [list(r) for r in ws.iter_rows(values_only=True)]) for ws in wb.worksheets]
        finally:
            wb.close()
    text = _decode(data)
    if ext == "tsv":
        delim = "\t"
    else:
        try:
            delim = csv.Sniffer().sniff(text[:4096], delimiters=",\t;|").delimiter
        except csv.Error:
            delim = ","
    return [("", [row for row in csv.reader(io.StringIO(text), delimiter=delim)])]


def _text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))                # a phone number Excel stored as a number
    return str(v).strip()


def _find(header: list, wanted: dict) -> dict:
    names = [words(h) for h in header]
    cols = {}
    for fld, syns in wanted.items():
        for s in syns:
            w = words(s)
            if w and w in names:
                cols[fld] = names.index(w)
                break
    return cols


def _wanted(defaults: dict, given) -> dict:
    out = {k: tuple(v) for k, v in defaults.items()}
    for k, v in (given or {}).items():
        if k in out:
            out[k] = tuple(v) if isinstance(v, (list, tuple)) else (str(v),)
    return out


def _number(v) -> tuple[float | None, bool]:
    """(value, is a percent)."""
    if v is None or isinstance(v, bool):
        return None, False
    if isinstance(v, (int, float)):
        return float(v), False
    t = str(v).strip()
    pct = t.endswith("%")
    t = t.replace("$", "").replace(",", "").replace("%", "").strip()
    try:
        return (float(t), pct) if t else (None, False)
    except ValueError:
        return None, False


def _period(v, tz: str) -> str:
    if v is None:
        return ""
    t = _text(v) if not isinstance(v, (datetime, date)) else v
    if isinstance(t, str):
        m = re.fullmatch(r"(\d{4})-(\d{1,2})", t)
        if m:
            return "%04d-%02d" % (int(m.group(1)), int(m.group(2)))
        m = re.fullmatch(r"([A-Za-z]{3,9})\.?\s+(\d{4})", t)
        if m:
            k = next((i for i, x in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
                                                  "nov", "dec")) if m.group(1).lower().startswith(x)), None)
            if k is not None:
                return "%s-%02d" % (m.group(2), k + 1)
    at, _exact = parse_when(t, tz)
    return local_day(at, tz) if at else ""


def _metric_of(header: str, metrics: dict) -> str:
    """Which of our metrics a totals column holds, as_reported for a vendor's own word, or '' to ignore it."""
    w = words(header)
    for metric, syns in metrics.items():
        if w in {words(s) for s in syns}:
            return metric
    if w in BORROWED or any(h in w for h in BORROWED_HINTS):
        return "as_reported"
    return ""


def _kind_column(s: dict) -> tuple:
    """The header(s) naming each row's kind, when the connection asked for one."""
    v = s.get("kind_column") or ((s.get("columns") or {}) if isinstance(s.get("columns"), dict) else {}).get(KIND_COLUMN)
    if not v:
        return ()
    return tuple(v) if isinstance(v, (list, tuple)) else (str(v),)


def _month_end(month: str) -> str:
    y, m = (int(x) for x in month.split("-"))
    nxt = date(y + (m // 12), m % 12 + 1, 1)
    return (nxt - timedelta(days=1)).isoformat()


class FileImport(Adapter):
    id = "file_import"
    tool = ""
    provides = tuple(Provides(k, "file", frozenset({"tool_id", "person_ref", "direction", "phone", "line", "record",
                                                     "answered", "seconds", "outcome", "status", "amount", "campaign",
                                                     "stage"}), per_person=True, scope="all", latency="daily",
                              backfill_days=None) for k in KINDS)

    # ------------------------------------------------------------------ settings
    def kinds(self, conn=None) -> set:
        s = (conn or {}).get("settings") or {}
        if s.get("preset") == "agency":
            return {"call", "phone_status", "stage"}
        rows = s.get("rows") or "call"
        if rows == "tally" or _kind_column(s):
            return set(KINDS)
        return {rows} if rows in KINDS else set(KINDS)

    def settings_problems(self, settings: dict) -> list:
        s, out = settings or {}, []
        if s.get("preset", "") not in PRESETS:
            out.append("preset is agency or nothing")
        rows = s.get("rows", "call")
        if rows not in KINDS + ("tally",):
            out.append("rows is one of %s, or tally for a totals report" % ", ".join(KINDS))
        known_columns = dict(COLUMNS, **TOTAL_KEYS, **{KIND_COLUMN: ()})
        for key, known in (("columns", known_columns), ("metrics", METRIC_COLUMNS)):
            given = s.get(key)
            if given is None:
                continue
            if not isinstance(given, dict):
                out.append("%s maps a field to the header (or headers) that hold it" % key)
                continue
            for k in given:
                if k not in known:
                    out.append("%s has %r (it takes %s)" % (key, k, ", ".join(known)))
        if s.get("direction") and not direction_of(s["direction"]):
            out.append("direction is out or in")
        if s.get("stage") and words(s["stage"]) not in STAGE_WORDS and s["stage"] not in STAGES:
            out.append("stage is one of %s" % ", ".join(STAGES))
        if s.get("period") and not _period(s["period"], TZ):
            out.append("period is a day written YYYY-MM-DD or a month written YYYY-MM")
        if s.get("tz"):
            try:
                ZoneInfo(str(s["tz"]))
            except Exception:  # noqa: BLE001
                out.append("tz %r is not a time zone name (for example America/New_York)" % s["tz"])
        return out

    # ------------------------------------------------------------------ reading what is kept
    def pull(self, conn, since, until, kinds, now=None) -> Batch:
        s = conn.get("settings") or {}
        if s.get("preset") == "agency":
            return self._agency(conn, since, until, kinds)
        b = Batch(events=[e for e in stored_events(conn["id"], since, until) if e.kind in kinds],
                  tallies=stored_tallies(conn["id"]))
        ensure()
        with store._LOCK, store._db() as c:
            r = c.execute("SELECT MAX(at) AS at FROM kpisrc_file WHERE source=?", (conn["id"],)).fetchone()
        if r and r["at"]:
            b.last_at = datetime.fromisoformat(r["at"])
        return b

    # ------------------------------------------------------------------ a new file
    def on_file(self, conn, name: str, data: bytes, now: datetime | None = None) -> Batch:
        """Read one export into the source's events or tallies. The same file twice adds nothing."""
        ensure()
        now = now or datetime.now(timezone.utc)
        s = conn.get("settings") or {}
        if s.get("preset") == "agency":
            return Batch(problems=["the agency's files are read by the agency results workflow: post the file in Slack "
                                   "and ask Quinn. This source counts every file that workflow keeps."])
        digest = hashlib.sha256(data or b"").hexdigest()
        with store._LOCK, store._db() as c:
            seen = c.execute("SELECT 1 FROM kpisrc_file WHERE source=? AND digest=?", (conn["id"], digest)).fetchone()
        if seen:
            return Batch(notes=["%s was read before, so nothing new was counted." % name])
        if not data:
            return Batch(problems=["%s is empty" % name])
        try:
            tables = read_tables(name, data)
        except Exception as exc:  # noqa: BLE001  (a damaged or locked file is one file for a person, not a crash)
            return Batch(problems=["%s could not be opened (%s)" % (name, type(exc).__name__)])
        tz = str(s.get("tz") or setting("AGENTS_KPI_TZ", TZ))
        if (s.get("rows") or "call") == "tally":
            tallies, problems = self._totals(conn, s, tables, name, tz)
            events = []
            save_tallies(tallies)
        else:
            events, problems = self._rows(conn, s, tables, name, digest, tz)
            tallies = []
            save_events(events)
        if events or tallies:
            with store._LOCK, store._db() as c:
                c.execute("INSERT OR REPLACE INTO kpisrc_file (source, digest, name, at, events, tallies) "
                          "VALUES (?,?,?,?,?,?)", (conn["id"], digest, name[:120], now.isoformat(timespec="seconds"),
                                                   len(events), len(tallies)))
        return Batch(events=events, tallies=tallies, problems=problems, last_at=now)

    def _rows(self, conn, s, tables, name, digest, tz) -> tuple[list, list]:
        wanted = _wanted(COLUMNS, {k: v for k, v in (s.get("columns") or {}).items() if k in COLUMNS})
        if _kind_column(s):
            wanted[KIND_COLUMN] = _kind_column(s)
        default_kind = s.get("rows") or "call"
        default_stage = STAGE_WORDS.get(words(s.get("stage")), "") or str(s.get("stage") or "")
        events, problems = [], []
        why = "no row near the top names a date column (looked for %s)" % ", ".join(wanted["at"][:4])
        for _label, table in tables:
            hi, cols = None, {}
            for i, row in enumerate(table[:HEADER_ROWS]):
                c = _find(row, wanted)
                if "at" in c and len(c) >= 2:
                    hi, cols = i, c
                    break
            if hi is None:
                continue
            no_date, bad_kind, occur = 0, Counter(), Counter()
            for k, raw in enumerate(table[hi + 1:], start=hi + 2):
                if not any(_text(x) for x in raw):
                    continue

                def cell(fld, raw=raw):
                    i = cols.get(fld)
                    return raw[i] if i is not None and i < len(raw) else None

                kind, stage = default_kind, default_stage
                if KIND_COLUMN in cols:
                    word = words(cell(KIND_COLUMN))
                    if "convers" in word:
                        bad_kind["a conversation is counted from calls and texts, never read from a file"] += 1
                        continue
                    kind, stage = KIND_WORDS.get(word, ("", ""))
                    if not kind:
                        bad_kind["a kind I do not know: %s" % (word[:30] or "blank")] += 1
                        continue
                at, exact = parse_when(cell("at"), tz)
                if at is None:
                    no_date += 1
                    continue
                if kind == "stage":
                    stage = STAGE_WORDS.get(words(cell("stage")), "") or stage
                    if stage not in STAGES:
                        bad_kind["stage " + (words(cell("stage")) or "blank")[:20]] += 1
                        continue
                direction = direction_of(cell("direction")) or direction_of(s.get("direction"))
                frm, to = digits(cell("from")), digits(cell("to"))
                phone = digits(cell("phone")) or (to if direction == "out" else frm if direction == "in" else "")
                line = digits(cell("line")) or (frm if direction == "out" else to if direction == "in" else "")
                outcome, status = _text(cell("outcome")), _text(cell("status"))
                answered = None
                if kind == "call":
                    answered = answered_of(cell("answered"))
                    if answered is None:
                        answered = answered_of(outcome) if answered_of(outcome) is not None else answered_of(status)
                tool_id = _text(cell("tool_id"))
                if tool_id:
                    sid = "id:%s" % tool_id
                else:
                    content = "|".join(words(_text(x)) for x in raw)
                    h = hashlib.sha1(content.encode("utf-8")).hexdigest()[:16]
                    occur[h] += 1
                    sid = "row:%s#%d" % (h, occur[h])
                try:
                    events.append(Event(kind=kind, at=at, exact=exact, source=conn["id"], source_id=sid,
                                        tool=conn.get("tool") or "", tool_id=tool_id, person_ref=_text(cell("person")),
                                        direction=direction, phone=phone, line=line, record=_text(cell("record")),
                                        owner=_text(cell("owner")), answered=answered, seconds=seconds_of(cell("seconds")),
                                        outcome=outcome, stage=stage, status=status, amount=money_of(cell("amount")),
                                        campaign=_text(cell("campaign")), grain="export",
                                        raw_ref="file:%s:%d" % (digest[:12], k)))
                except ValueError as exc:
                    bad_kind[str(exc)[:40]] += 1
            if no_date:
                problems.append("%s: %d row(s) had no date I could read and were left out" % (name, no_date))
            for what, n in bad_kind.most_common(3):
                problems.append("%s: %d row(s) were left out (%s)" % (name, n, what or "blank"))
            if events:
                return events, problems
            why = "it has the right columns but no rows I could read under them"
        return events, problems or ["%s: %s" % (name, why)]

    def _totals(self, conn, s, tables, name, tz) -> tuple[list, list]:
        metrics = _wanted(METRIC_COLUMNS, s.get("metrics"))
        keys = _wanted(TOTAL_KEYS, s.get("columns"))
        fixed = _period(s.get("period"), tz) if s.get("period") else ""
        tallies, problems = [], []
        for _label, table in tables:
            for i, row in enumerate(table[:HEADER_ROWS]):
                found = _find(row, keys)
                measures = [(j, _metric_of(h, metrics), _text(h)) for j, h in enumerate(row)
                            if j not in found.values() and _metric_of(h, metrics)]
                if not measures or ("period" not in found and not fixed):
                    continue
                no_period = 0
                for raw in table[i + 1:]:
                    if not any(_text(x) for x in raw):
                        continue
                    pi = found.get("period")
                    period = (_period(raw[pi], tz) if pi is not None and pi < len(raw) else "") or fixed
                    if not period:
                        no_period += 1
                        continue
                    who = found.get("person")
                    person = _text(raw[who]) if who is not None and who < len(raw) else ""
                    for j, metric, header in measures:
                        v, pct = _number(raw[j] if j < len(raw) else None)
                        if v is None:
                            continue
                        m = "as_reported" if pct else metric
                        try:
                            tallies.append(Tally(metric=m, period=period, value=v, source=conn["id"],
                                                 tool=conn.get("tool") or "", person_ref=person, definition=header))
                        except ValueError as exc:
                            problems.append("%s: %s" % (name, str(exc)[:120]))
                if no_period:
                    problems.append("%s: %d row(s) had no period I could read and were left out" % (name, no_period))
                return tallies, problems
        return [], ["%s: no row near the top names a figure and its period (set period in the connection when the "
                    "report covers one period)" % name]

    # ------------------------------------------------------------------ the agency
    def _agency(self, conn, since, until, kinds) -> Batch:
        b = Batch()
        try:
            from ..workflows import agency_results as ar
            returns = ar.load_returns()
        except Exception as exc:  # noqa: BLE001
            b.problems.append("the agency's kept files could not be read (%s)" % str(exc)[:100])
            return b
        s = conn.get("settings") or {}
        tz = str(s.get("tz") or setting("AGENTS_KPI_TZ", TZ))
        lo, hi = local_day(since, tz)[:7], local_day(until - timedelta(seconds=1), tz)[:7]
        houses: dict = defaultdict(dict)        # month -> record -> (entry, the return it came from)
        pdfs: dict = {}
        for ret in sorted(returns, key=lambda r: str(r.get("received") or "")):
            month = str(ret.get("month") or "")[:7]
            if not month or not (lo <= month <= hi):
                continue
            try:
                got = datetime.fromisoformat(str(ret.get("received")))
                got = got if got.tzinfo else got.replace(tzinfo=timezone.utc)
                b.last_at = max(b.last_at, got) if b.last_at else got
            except ValueError:
                pass
            if ret.get("kind") == "pdf":
                pdfs[month] = ret                       # the newest summary for a month wins
                continue
            for u, e in (ret.get("records") or {}).items():
                houses[month][u] = (e, ret)             # each house once, as the newest file says it
        for month, recs in houses.items():
            for u, (e, ret) in recs.items():
                b.events += self._house(conn, u, e, ret, month, kinds, tz, ar)
        for month, ret in pdfs.items():
            b.tallies += self._pdf(conn, ret, month)
        return b

    def _house(self, conn, u, e, ret, month, kinds, tz, ar) -> list:
        rows = e.get("rows") or []
        posted = str(ret.get("day") or "")
        fallback = posted if posted.startswith(month) else _month_end(month)
        tool = conn.get("tool") or "agency"
        out = []

        def when(day):
            return parse_when(day or fallback, tz)

        if "call" in kinds:
            occur = Counter()
            for r in rows:
                key = "|".join((month, u, str(r.get("phone") or ""), str(r.get("call_date") or ""),
                                words(r.get("disposition"))))
                occur[key] += 1
                at, exact = when(r.get("call_date"))
                if at is None:
                    continue
                out.append(Event(kind="call", at=at, exact=exact, source=conn["id"],
                                 source_id="call:%s#%d" % (hashlib.sha1(key.encode("utf-8")).hexdigest()[:16], occur[key]),
                                 tool=tool, direction="out", phone=r.get("phone") or "", record=u,
                                 answered=ANSWERED_CLASS.get(str(r.get("class") or "")),
                                 outcome=words(r.get("disposition")), person_ref=str(r.get("agent") or ""),
                                 grain="export", raw_ref="agency:%s:%s" % (ret.get("id"), r.get("row"))))
        if "phone_status" in kinds:
            for number, said in (e.get("phones") or {}).items():
                status = PHONE_FROM_CLASS.get(str(said))
                if not status:
                    continue                            # the calls on this number disagree: a person decides
                days = [str(r.get("call_date")) for r in rows if r.get("phone") == number and r.get("call_date")]
                at, exact = when(max(days) if days else "")
                if at is None:
                    continue
                out.append(Event(kind="phone_status", at=at, exact=exact, source=conn["id"],
                                 source_id="phone:%s:%s:%s" % (u, number, status), tool=tool, phone=number, record=u,
                                 outcome=status, grain="export", raw_ref="agency:%s" % ret.get("id")))
        if "stage" in kinds and e.get("top") == "lead":
            days = [str(r.get("call_date")) for r in rows if r.get("class") == "lead" and r.get("call_date")]
            at, exact = when(max(days) if days else "")
            if at is not None:
                out.append(Event(kind="stage", stage="lead", at=at, exact=exact, source=conn["id"],
                                 source_id="lead:%s" % u, tool=tool, record=u, outcome="lead", grain="export",
                                 raw_ref="agency:%s" % ret.get("id")))
        return out

    def _pdf(self, conn, ret, month) -> list:
        out = []
        for x in ret.get("numbers") or []:
            try:
                out.append(Tally(metric=pdf_metric(x), period=month, value=float(x.get("value") or 0),
                                 source=conn["id"], tool=conn.get("tool") or "agency", definition=str(x.get("label") or "")))
            except ValueError:
                continue
        return out


def pdf_metric(x: dict) -> str:
    """Which of our metrics an agency PDF figure is, or as_reported. A ratio, a rate, a percent, a connect, a contact,
    a transfer or talk time is the agency's own word (its "connects" count voicemail, and one "dials to connect" on a
    real report was dials per lead)."""
    lab = " %s " % words(x.get("label"))
    if x.get("kind") == "percent" or " to " in lab or " per " in lab or "rate" in lab:
        return "as_reported"
    if any(h in lab for h in ("convers", "contact", "connect", "transfer", "talk", " hour", "minute", "rpc", "answer")):
        return "as_reported"
    if x.get("kind") == "money":
        return "spend" if any(w in lab for w in ("spend", "spent", "cost", "invoice", "price", " fee")) else "as_reported"
    if "dial" in lab or lab.strip() in ("calls", "calls made", "total calls"):
        return "dials"
    if " lead" in lab:
        return "leads"
    if "appointment" in lab or "appt" in lab:
        return "appointments"
    if "offer" in lab:
        return "offers"
    return "as_reported"


ADAPTER = FileImport()

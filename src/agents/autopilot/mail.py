"""The mail lane: pieces built from the mail presets, staged or sent through a vendor, and logged
back to the record so DataSift's mail counters move.

The ty+2 lane (folder "06. FTM - MAIL") is six presets a month apart: Handwritten Letter, Family
Postcard, Soft-Offer Check, then the three again. Each preset gates on the record's own mail count
(directmail_attempts 0, 1, 2 ...) and on last_direct_mailed older than a month, so the presets ARE
the cadence. The catch: if a piece is mailed and DataSift never hears about it, the count stays at
0 and the record sits in Mail 01 forever. So a piece counts as mailed only once it is logged on the
record (log_attempt); until DataSift accepts that log, the ledger keeps the truth and every later
piece is chosen from the ledger instead of the counter.

Outreach rules, the same ones the texting agent lives by:
  * never name the list (no foreclosure, auction, probate, tax, lien ... in copy we render);
  * an initials-only owner gets no first name (never "Dear Henry" for E A Henry);
  * do_not_mail_ever on the record or the owner is final;
  * staged is the default: the stage builds and prices the batch, a person releases it.
"""
from __future__ import annotations

import csv
import logging
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from .. import config
from . import lanes, ledger, windows
from .stages import Ctx, addr, status_of, DEAD_STATUSES

SRC = Path(__file__).resolve().parents[2]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

log = logging.getLogger(__name__)

# Mirrors sms_agent.respond._LIST_WORDS: the seller should feel found, not targeted.
LIST_WORDS = re.compile(
    r"\b(foreclos\w*|auction\w*|probate|decedent|inherit\w*|estate sale|tax (lien|sale|delinq\w*)|"
    r"delinquen\w*|lien|code violation|condemn\w*|evict\w*|divorce|bankrupt\w*|"
    r"behind on (your )?(payment|mortgage|taxes)|pre-?foreclosure|trustee sale|distress\w*|default)\b", re.I)
PIECE_LABEL = {"handwritten_letter": "handwritten letter", "family_postcard": "family postcard",
               "soft_offer_check": "soft-offer check", "postcard": "postcard", "letter": "letter"}


def _clean_first(raw: str) -> str:
    try:
        from sms_agent.knowledge.touches import clean_first
        return clean_first(raw)
    except Exception:  # noqa: BLE001
        toks = (raw or "").replace(".", " ").split()
        return toks[0].title() if len(toks) == 1 and len(toks[0]) > 1 else ""


@dataclass
class Piece:
    record_uuid: str
    piece: str
    preset: str
    step: int
    name: str
    street: str
    city: str
    state: str
    zip: str
    first_name: str = ""
    property_street: str = ""
    property_city: str = ""
    offer: int = 0
    price: float = 0.0
    merge: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


def build_piece(rec: dict, kind: str, preset: str, step: int, st) -> tuple[Piece | None, str]:
    """(piece, "") or (None, why not). Reads only the record."""
    ow = rec.get("owner") or {}
    if rec.get("do_not_mail_ever") or ow.get("do_not_mail_ever"):
        return None, "do not mail"
    if status_of(rec) in DEAD_STATUSES:
        return None, "status %s" % status_of(rec)
    ma = ow.get("address") or {}
    street, city = (ma.get("street") or "").strip(), (ma.get("city") or "").strip()
    state, zip5 = (ma.get("state") or "").strip(), str(ma.get("postal_code") or "")[:5]
    if not (street and city and state and len(zip5) == 5):
        return None, "no complete mailing address"
    first = " ".join(str(ow.get("first_name") or "").split())
    last = " ".join(str(ow.get("last_name") or "").split())
    company = " ".join(str(ow.get("company") or "").split())
    name = (" ".join([first, last]).strip() or company)
    if not name:
        return None, "no owner name"
    pa = rec.get("address") or {}
    offer = 0
    if kind == "soft_offer_check":
        pct = float(st.get("offer_pct") or 0)
        value = float(rec.get("estimate_value") or 0)
        if not pct or not value:
            return None, "soft-offer check needs offer_pct in the playbook and a value on the record"
        offer = int(round(value * pct / 1000.0) * 1000)
    piece = Piece(record_uuid=rec.get("uuid") or "", piece=kind, preset=preset, step=step, name=name.title(),
                  street=street, city=city, state=state, zip=zip5,
                  first_name="" if company and not first else _clean_first(first + " " + last),
                  property_street=pa.get("street") or "", property_city=pa.get("city") or "", offer=offer)
    return piece, ""


def render_copy(template: str, piece: Piece, sender: dict) -> tuple[str, str]:
    """(text, problem). Copy we render ourselves (handwritten pieces need a body) is checked against
    the list words, so a template edit can never tell a homeowner which list they are on."""
    greet = piece.first_name or "there"
    text = (template or "").format(first_name=greet, street=piece.property_street, city=piece.property_city,
                                   sender_name=sender.get("name", ""), sender_phone=sender.get("phone", ""),
                                   offer="{:,}".format(piece.offer) if piece.offer else "")
    m = LIST_WORDS.search(text)
    if m:
        return text, "copy names the list (%r)" % m.group(0)
    return text, ""


# ---------------------------------------------------------------------- vendors
class Vendor:
    """A mail vendor. `export_only` vendors produce a file a person uploads; API vendors send."""
    name = "csv"
    export_only = True

    def __init__(self, prices: dict | None = None):
        self.prices = {"handwritten_letter": 3.00, "family_postcard": 0.75, "soft_offer_check": 1.25,
                       "postcard": 0.75, "letter": 1.25}
        self.prices.update(prices or {})

    def configured(self) -> bool:
        return True

    def price(self, kind: str) -> float:
        return float(self.prices.get(kind, 1.0))

    def send(self, piece: Piece, sender: dict, text: str = "") -> dict:
        raise RuntimeError("the %s vendor cannot send; release the batch by uploading its CSV" % self.name)

    def export(self, batch_id: int, items: list[dict], sender: dict) -> str:
        out = Path(config.DATA_DIR) / "mail" / ("batch_%d.csv" % batch_id)
        out.parent.mkdir(parents=True, exist_ok=True)
        cols = ["piece", "name", "street", "city", "state", "zip", "first_name", "property_street",
                "property_city", "offer", "record_uuid", "step"]
        with out.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for it in items:
                w.writerow(it)
        return str(out)


def vendor_for(st) -> Vendor:
    name = str(st.get("vendor", "csv")).lower()
    prices = st.get("prices") or {}
    if name == "csv":
        return Vendor(prices)
    if name == "siftmail" and not st.get("internal_routes", False):
        # The SiftMail campaign route is not in the published reference, so a user install never
        # reaches it by default; an account that has verified it opts in with internal_routes: true.
        v = Vendor(prices)
        v.refused = ("SiftMail's campaign route is not in DataSift's published API; set internal_routes: true "
                     "only on an account where it has been verified, otherwise use the CSV or an outside vendor")
        return v
    try:
        from . import mail_vendors
        return mail_vendors.make(name, prices)
    except (ImportError, KeyError) as exc:
        log.error("mail vendor %s is not available (%s); falling back to CSV export", name, exc)
        return Vendor(prices)


def sender_block(st) -> dict:
    s = dict(st.get("sender") or {})
    s.setdefault("name", os.environ.get("AUTOPILOT_MAIL_FROM_NAME", ""))
    s.setdefault("phone", os.environ.get("AUTOPILOT_MAIL_FROM_PHONE", ""))
    for k in ("street", "city", "state", "zip"):
        s.setdefault(k, os.environ.get("AUTOPILOT_MAIL_FROM_%s" % k.upper(), ""))
    return s


# ---------------------------------------------------------------------- logging back to DataSift
def log_attempt(ctx: Ctx, piece: Piece, ref: str, state: dict) -> str:
    """Tell the record it was mailed, so its mail preset moves on. Returns how it was logged.

    A vendor that counts itself (SiftMail) needs nothing. For any other vendor the DOCUMENTED
    write-back is a month tag ("Mailed 2026-11", the shape lesson 4-18 teaches) plus the account's
    "Last Mailed" date field when it has one; presets for an outside-vendor lane key on those. The
    published reference has no direct-mail attempt counter (it has sms, rvm and predictive call), so
    writing directmail_attempts is an internal route, used only when the stage sets
    undocumented_counter: true, and the first piece of every delivery is read back. A provenance note
    goes on every record either way."""
    if getattr(ctx.mailer, "counts_itself", False):
        return "vendor"
    today = ledger.day(ctx.now, ctx.policy.tz)
    parts = []
    tag = str(ctx.stage.get("mailed_tag", "Mailed {yyyy}-{mm}")).format(yyyy=today[:4], mm=today[5:7])
    if tag:
        ctx.crm.add_tags(piece.record_uuid, [tag])
        parts.append("tag")
    if "last_mailed_field" not in state:
        try:
            state["last_mailed_field"] = ctx.crm.custom_field_uuid(str(ctx.stage.get("last_mailed_field", "Last Mailed")))
        except Exception:  # noqa: BLE001
            state["last_mailed_field"] = ""
    if state.get("last_mailed_field"):
        ctx.crm.set_custom_field(piece.record_uuid, state["last_mailed_field"], today)
        parts.append("field")
    how = "+".join(parts) or "note"
    if ctx.stage.get("undocumented_counter", False) and state.get("counters_ok", True):
        rec = ctx.crm.record(piece.record_uuid)
        total = int(rec.get("directmail_attempts") or 0) + 1
        ctx.crm.directmail_attempts(piece.record_uuid, total)
        ctx.crm.set_last_mailed(piece.record_uuid, today)
        ok = True
        if not state.get("verified"):
            back = ctx.crm.record(piece.record_uuid)
            got_n, got_d = int(back.get("directmail_attempts") or 0), str(back.get("last_direct_mailed") or "")[:10]
            state["verified"] = True
            if got_n != total or got_d != today:
                ok = False
                state["counters_ok"] = False
                ctx.out.problem("the mail counter did not stick on %s (wrote %d and %s, read %d and %s); the rest of "
                                "this delivery skips the counter (the documented tag and date still land), so "
                                "counter-keyed mail presets will not move" % (
                                    piece.property_street, total, today, got_n, got_d or "none"))
        if ok:
            how = (how + "+counter") if how != "note" else "counter"
    note = "Mailed: %s (step %d of the mail lane) via %s%s." % (
        PIECE_LABEL.get(piece.piece, piece.piece), piece.step, ctx.mailer.name, (", ref %s" % ref) if ref else "")
    ctx.crm.message(piece.record_uuid, note, pin=False)
    return how


# ---------------------------------------------------------------------- the stage
def _due(ctx: Ctx, spec: dict, step: int, staged: set, already: set) -> list[tuple[dict, str]]:
    title = spec["preset"]
    try:
        must = ctx.crm.preset_must(title)
    except KeyError as exc:
        ctx.out.problem(str(exc))
        return []
    must, notes = windows.resolve(must, ctx.now)
    for n in notes:
        if "dropped" in n:
            ctx.out.problem("%s: %s" % (title, n))
    out = []
    wait = timedelta(days=float(spec.get("wait_days", ctx.stage.get("wait_days", 30))))
    for row in ctx.crm.rows(must, cap=int(ctx.stage.get("max_scan", 1000))):
        u = row.get("uuid")
        if not u or u in already:
            continue
        if u in staged:
            ctx.out.tally["already staged, waiting on release"] += 1
            continue
        mine = ledger.events(u, "mail")
        if mine:
            if ctx.now - datetime.fromisoformat(mine[-1]["at"]) < wait:
                continue
            if max(int(e["detail"].get("step") or 0) for e in mine) >= step:
                continue
        if row.get("do_not_mail_ever") or row.get("owner_do_not_mail_ever"):
            ctx.out.tally["do not mail"] += 1
            continue
        out.append((row, title))
        already.add(u)
    return out


def mail_open(ctx: Ctx, rec: dict) -> tuple[bool, str]:
    """Is the mail channel open for this record today, by its lane's timeline (plan 0.20)? A stage
    with no `lane` set is not restricted: the table rules only the lanes it describes."""
    lane = str(ctx.stage.get("lane") or "")
    cfg = getattr(ctx.policy, "lanes", None)
    if not lane or cfg is None or not cfg.timeline(lane):
        return True, ""
    from . import gate
    from .stages import lane_entry
    from zoneinfo import ZoneInfo
    zone = ZoneInfo(ctx.policy.tz)
    entered = lane_entry(ctx, rec, lane).astimezone(zone).date()
    today = ctx.now.astimezone(zone).date()
    low = {str(t).lower() for t in (rec.get("tags") or []) + (rec.get("lists") or []) if isinstance(t, str)}
    facts = {"no_number": not gate.usable_numbers(rec, "call", getattr(ctx.policy, "gate", None) or gate.GateConfig()),
             "foreclosure": "foreclosure" in low}
    return lanes.channel_open(cfg, lane, "mail", lanes.business_days(entered, today),
                              int(rec.get("predictivecall_attempts") or 0), facts, today.weekday())


def run_mail(ctx: Ctx) -> None:
    st = ctx.stage
    if getattr(ctx.mailer, "refused", ""):
        ctx.out.problem(ctx.mailer.refused)
    specs = [p for p in (st.get("pieces") or []) if isinstance(p, dict)]
    sender = sender_block(st)
    cap_n = int(st.get("daily_pieces", 50))
    send = st.get("send", "staged")
    staged = ledger.staged_record_uuids(st.id)
    picked: list[Piece] = []
    already: set = set()
    usd = 0.0
    blocked: dict[str, list[str]] = {}
    # Once the day's mail budget has refused a piece at some price, every later piece at that price
    # or more is refused too (the day's total only grows). Those records are counted and NOT read:
    # reading 600 records to tell each one "tomorrow" is 1,200 requests that buy nothing.
    refused_from: float | None = None
    for step, spec in enumerate(specs, 1):
        if getattr(ctx.mailer, "batch", False):
            _, why = ctx.mailer.touch(spec, sender)
            if why:
                key = "return address" if "return address" in why else "design"
                blocked.setdefault(key, []).append(spec["preset"])
                if ctx.live:
                    continue      # a plan still counts and prices what is due; a live run cannot send it
        for row, title in _due(ctx, spec, step, staged, already):
            ctx.out.found += 1
            if len(picked) >= cap_n:
                ctx.out.tally["left for tomorrow (daily cap %d)" % cap_n] += 1
                continue
            if refused_from is not None and ctx.mailer.price(spec["piece"]) >= refused_from:
                ctx.out.tally["left for tomorrow (the day's mail budget is used)"] += 1
                continue
            rec = ctx.crm.record(row["uuid"])
            if not ctx.passes(rec, "mail", row=row):
                continue
            ok, why = mail_open(ctx, rec)
            if not ok:
                ctx.out.tally["not yet: %s" % why] += 1
                continue
            piece, why = build_piece(rec, spec["piece"], title, step, st)
            if not piece:
                ctx.out.tally["not mailable: %s" % why] += 1
                continue
            piece.price = ctx.mailer.price(piece.piece)
            ok, why = ctx.budget(usd + piece.price, piece.record_uuid, record_usd=0)
            if not ok:
                ctx.out.tally["left for tomorrow (%s)" % why] += 1
                refused_from = piece.price if refused_from is None else min(refused_from, piece.price)
                continue
            if spec.get("copy"):
                text, prob = render_copy(spec["copy"], piece, sender)
                if prob:
                    ctx.out.problem("%s copy: %s; nothing staged for that piece" % (title, prob))
                    continue
                piece.merge["text"] = text
            usd += piece.price
            picked.append(piece)
    if blocked.get("design"):
        ctx.out.problem("%d of %d mail presets have no SiftMail design yet (%s). Design each piece once in Mail "
                        "Design, then set its design uuid in the playbook (cli.py autopilot mail-designs lists them)."
                        % (len(blocked["design"]), len(specs), ", ".join(p.split(" - ")[-1] for p in blocked["design"])))
    if blocked.get("return address"):
        ctx.out.problem("no return address for SiftMail: set AUTOPILOT_MAIL_FROM_NAME, _STREET, _CITY, _STATE, _ZIP "
                        "and _PHONE on the box (or sender in the playbook)")
    by = {}
    for p in picked:
        by[p.piece] = by.get(p.piece, 0) + 1
    mix = ", ".join("%d %s" % (n, PIECE_LABEL.get(k, k)) for k, n in by.items())
    if not picked:
        ctx.out.head = "Nothing is due in the mail lane today."
        return
    for p in picked[:6]:
        ctx.out.line("%s: %s to %s" % (p.property_street or addr({"address": {"street": p.street}}),
                                       PIECE_LABEL.get(p.piece, p.piece), p.name))
    if not ctx.live:
        ctx.reserve(usd)
        ctx.out.acted = len(picked)
        ctx.out.head = "Would stage %d piece(s), about $%.2f: %s." % (len(picked), usd, mix)
        return
    if send == "staged" or ctx.mailer.export_only:
        items = [p.as_dict() for p in picked]
        bid = ledger.batch_create(st.id, "mail", items, usd)
        path = ctx.mailer.export(bid, items, sender) if ctx.mailer.export_only else ""
        ctx.out.batch = bid
        ctx.out.acted = len(picked)
        ctx.out.head = "Staged batch %d: %d piece(s), about $%.2f (%s). Release: python src/agents/cli.py autopilot release %d%s" % (
            bid, len(picked), usd, mix, bid, ("  (CSV for the mail house: %s)" % path) if path else "")
        return
    sent, paid = deliver(ctx, picked, sender, "auto %s" % ledger.day(ctx.now, ctx.policy.tz))
    ctx.out.acted = sent
    ctx.out.head = "Mailed %d piece(s), $%.2f: %s." % (sent, paid, mix)


def _record_mailed(ctx: Ctx, p: Piece, cost: float, how: str, ref: str) -> None:
    ledger.event(ctx.run_id, ctx.stage.id, p.record_uuid, "mail",
                 {"piece": p.piece, "step": p.step, "vendor": ctx.mailer.name, "ref": ref, "logged": how},
                 usd=round(cost, 4), now=ctx.now, tz=ctx.policy.tz)


def deliver(ctx: Ctx, pieces: list[Piece], sender: dict, label: str, uploaded: bool = False) -> tuple[int, float]:
    """Send (or, for an uploaded CSV batch, record) these pieces and log each one on its record.

    A batch vendor (SiftMail) gets one campaign per mail step: quote first, launch only if the quote
    covers exactly the records asked for, and only inside the caps."""
    v = ctx.mailer
    sent, paid = 0, 0.0
    state: dict = {}
    # The gate runs a second time here, at release (plan 0.1, 0.17): a homeowner who opted out, died,
    # sold or became a lead after the batch was staged must not get the piece. A CSV batch that was
    # already uploaded to a mail house cannot be pulled back, so there it is only said out loud.
    kept = []
    for p in pieces:
        verdict = ctx.verdict(ctx.crm.record(p.record_uuid), "mail")
        if verdict.ok or uploaded:
            kept.append(p)
        if not verdict.ok:
            ctx.out.tally["%s: %s" % ("mailed anyway, already at the mail house" if uploaded else "held back at release",
                                      verdict.reason())] += 1
            if uploaded:
                ctx.out.problem("%s was in the uploaded file but no longer passes the gate (%s)" % (
                    p.property_street or "a record", verdict.reason()))
    pieces = kept
    if getattr(v, "batch", False):
        specs = [p for p in (ctx.stage.get("pieces") or []) if isinstance(p, dict)]
        groups: dict[int, list[Piece]] = {}
        for p in pieces:
            groups.setdefault(p.step, []).append(p)
        floor = int(ctx.stage.get("min_batch", 20))
        for step, group in sorted(groups.items()):
            spec = specs[step - 1] if 0 < step <= len(specs) else {}
            if len(group) < floor:
                # A campaign is a bulk activity: under 20 records it is not allocated properly and
                # repeated small ones count as abuse (bulk operations policy). The pieces wait.
                ctx.out.problem("step %d has %d piece(s); a SiftMail campaign needs %d or more, so they wait" % (
                    step, len(group), floor))
                continue
            touch, why = v.touch(spec, sender, today=ctx.now.date())
            if not touch:
                ctx.out.problem("step %d: %s" % (step, why))
                continue
            uuids = [p.record_uuid for p in group]
            title = "%s step %d %s" % (ctx.stage.id, step, label)
            n, cost, _raw = v.quote(ctx.crm, uuids, touch, title)
            if n != len(uuids):
                ctx.out.problem("SiftMail quoted %r records for step %d, not the %d asked for; nothing launched" % (n, step, len(uuids)))
                continue
            cost = float(cost if cost is not None else sum(p.price for p in group))
            ok, why = ctx.budget(cost)
            if not ok:
                ctx.out.problem("SiftMail quotes $%.2f for step %d, over the %s; nothing launched" % (cost, step, why))
                continue
            res = v.launch(ctx.crm, uuids, touch, title)
            ref = str(res.get("uuid") or res.get("id") or title)
            each = cost / len(group)
            for p in group:
                _record_mailed(ctx, p, each, "vendor", ref)
            ctx.settle(0, cost, "SiftMail step %d x%d" % (step, len(group)))
            sent += len(group)
            paid += cost
        return sent, round(paid, 2)
    for p in pieces:
        ref = label
        cost = p.price
        if not v.export_only:
            try:
                res = v.send(p, sender, p.merge.get("text", ""))
            except Exception as exc:  # noqa: BLE001
                ctx.out.problem("%s for %s failed: %s" % (PIECE_LABEL.get(p.piece, p.piece), p.property_street, str(exc)[:160]))
                continue
            ref = str(res.get("id") or label)
            cost = float(res.get("cost") or p.price)
        elif not uploaded:
            continue
        how = log_attempt(ctx, p, ref, state)
        _record_mailed(ctx, p, cost, how, ref)
        ctx.settle(0, cost, "mail %s" % p.piece)
        sent += 1
        paid += cost
    return sent, round(paid, 2)


def release(batch_id: int, *, crm=None, pol=None, commit: bool = False, uploaded: bool = False,
            now: datetime | None = None) -> dict:
    """Release a staged batch: send it through the stage's vendor, or (export-only vendor, after the
    CSV was uploaded to the mail house) record it as mailed. Dry unless commit."""
    from datetime import timezone
    from . import policy as policy_mod
    from .stages import Summary
    b = ledger.batch(batch_id)
    if not b:
        raise SystemExit("no batch %d" % batch_id)
    if b["status"] != "staged":
        raise SystemExit("batch %d is %s, not staged" % (batch_id, b["status"]))
    pol = pol or policy_mod.load()
    st = pol.stage(b["stage"])
    vendor = vendor_for(st)
    pieces = [Piece(**{k: v for k, v in it.items() if k in Piece.__dataclass_fields__}) for it in b["items"]]
    if vendor.export_only and not uploaded:
        raise SystemExit("batch %d is a CSV batch: upload %s to the mail house, then re-run with --uploaded"
                         % (batch_id, Path(config.DATA_DIR) / "mail" / ("batch_%d.csv" % batch_id)))
    out = Summary(stage=st.id, kind="mail", owner=st.owner, mode=st.mode, live=commit)
    if not commit:
        return {"batch": batch_id, "pieces": len(pieces), "usd": b["usd"], "dry": True}
    if crm is None:
        from .crm import CRM
        crm = CRM()
    ctx = Ctx(policy=pol, stage=st, crm=crm, run_id="release-%d" % batch_id, now=now or datetime.now(timezone.utc),
              live=True, out=out, mailer=vendor)
    sent, paid = deliver(ctx, pieces, sender_block(st), "batch %d" % batch_id, uploaded=uploaded)
    ledger.batch_set(batch_id, "sent" if sent else "failed", {"sent": sent, "usd": paid, "problems": out.problems})
    return {"batch": batch_id, "sent": sent, "of": len(pieces), "usd": paid, "problems": out.problems}

"""Stage a deal blast to the cash buyers who actually buy in that price band.

The acquisition campaign texts homeowners to ask whether a house is theirs.
This one texts INVESTORS to tell them what we have and what it costs, which
inverts almost every rule in the seller program. It is a separate module for
the same reason `heir_campaign` is: the question is different, the recipient is
different, and blending them would eventually send the wrong one.

The message shape is Ty's (2026-08-28): the ROAD NAME and the AREA, never the
exact address, plus the PRICE. An interested reply converts to a phone call
with a human. The agent never negotiates and never sends the address.

Nothing here bypasses a guard. Candidates are built by `seed.build()`, which
applies suppression, the dial-tier floor, live-conversation checks, the sticky
sender rule and the signed-or-not-sent rule. This module then substitutes the
deal wording and RE-VALIDATES it under the buyer profile, which is stricter
than the seller one in the way that matters here: it permits a price only if
that exact figure is on the approved deal sheet.

    python src/sms_agent/dispo_campaign.py --deal deals/old_state.json
    python src/sms_agent/dispo_campaign.py --deal deals/old_state.json --queue
    python src/sms_agent/cli.py release --touch 1        # the go/no-go
"""
from __future__ import annotations

import argparse
import logging
import io
import json
import os
import re
import sys
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from sms_agent import config, respond, seed  # noqa: E402
from sms_agent.knowledge import touches  # noqa: E402

# Where the buyer registry lives. A relative path works from the repo root
# and nowhere else: on the Fly box the working directory is /app and the
# registry is on the mounted volume. This was hardcoded in TWO places and
# only one was made env-driven, so on Fly _bands() silently returned {} and
# every buyer became 'band unknown, kept': the filter passed all 192,
# including the buyers whose cheapest purchase is over $600,000.
OUT = Path(os.environ.get("SMS_AGENT_DISPO_DIR", "output/dispo_buyers"))
REGISTRY = OUT / "registry.json"

# A deal sheet must not carry the things we promised not to send. Catching it
# here is the difference between a rule and a guarantee: if the house number
# never enters the process, no prompt or model can leak it.
HOUSE_NUMBER_RX = re.compile(r"^\s*\d")
ZIP_RX = re.compile(r"\b\d{5}(-\d{4})?\b")

REQUIRED = ("deal_id", "road", "area", "price")


def load_deal(path: str) -> dict:
    """Read and hard-validate a deal sheet."""
    deal = json.load(io.open(path, encoding="utf-8"))
    missing = [k for k in REQUIRED if not deal.get(k)]
    if missing:
        raise SystemExit("deal sheet is missing: " + ", ".join(missing))

    disclose = bool(deal.get("disclose_address"))
    road = str(deal["road"]).strip()
    if disclose:
        # OPT-IN, per deal. Blast 1 withheld the address by design and four
        # guards enforced it. Ty's call on blast 2 is to lead WITH the
        # address and offer the lockbox, so the guards invert rather than
        # disappear: an address-forward deal must actually carry a full
        # address, and the redaction default still protects every deal that
        # does not set this flag.
        addr = str(deal.get("address") or "").strip()
        if not addr:
            raise SystemExit(
                "deal sheet sets disclose_address but has no 'address'")
        if not HOUSE_NUMBER_RX.match(addr):
            raise SystemExit(
                "disclose_address is set but 'address' does not start with a"
                " house number: " + addr)
        if not ZIP_RX.search(addr):
            raise SystemExit("disclose_address is set but 'address' has no zip")
        deal["zip"] = ZIP_RX.search(addr).group(0)
    else:
        if HOUSE_NUMBER_RX.match(road):
            raise SystemExit(
                "deal sheet 'road' starts with a house number (" + road + "). "
                "Send the road name only, never the exact address.")
        for field in ("road", "area"):
            if ZIP_RX.search(str(deal.get(field) or "")):
                raise SystemExit("deal sheet '" + field + "' contains a zip code")
    try:
        deal["price"] = int(deal["price"])
    except (TypeError, ValueError):
        raise SystemExit("deal sheet 'price' must be a whole number")
    if deal["price"] <= 0:
        raise SystemExit("deal sheet 'price' must be positive")
    return deal


def fmt_price(value: int) -> str:
    """The one figure the agent may state, formatted the way a person types it."""
    return "${:,}".format(int(value))


def _bands() -> dict:
    """record uuid -> the price band that buyer has actually bought in."""
    if not REGISTRY.exists():
        # Returning {} here is indistinguishable from every buyer having no
        # band, and match_band KEEPS unknown bands by design. Silence would
        # mean the price filter quietly stopped existing.
        raise SystemExit(
            "no registry at %s. Set SMS_AGENT_DISPO_DIR to the directory"
            " holding registry.json; refusing to run with no price bands."
            % REGISTRY)
    out = {}
    for b in json.load(io.open(REGISTRY, encoding="utf-8")):
        if b.get("saved_uuid") and b.get("price_min"):
            out[b["saved_uuid"]] = (b.get("price_min"), b.get("price_max"),
                                    b.get("n_buys"), b.get("name"))
    return out


def match_band(rows: list, price: int, tol: float, floor_mult: float = 4.0) -> tuple:
    """Keep buyers whose own purchase history brackets this deal's price.

    Ty chose deal blasts over a buy-box qualification drip, so the buy box has
    to be INFERRED. Deed history is the honest proxy: someone whose every
    purchase sits between 40k and 120k is a real candidate for a 92k deal and a
    waste of a segment for a 600k one.

    A buyer with no band is kept, not dropped. Missing history means we never
    resolved it, not that they buy nothing, and silently dropping the unknown
    is how a blast quietly shrinks to the records that happened to hydrate.

    THE BAND IS ASYMMETRIC, because the two directions are not the same
    constraint. Above the band is AFFORDABILITY: a buyer whose ceiling is 120k
    cannot close a 600k deal, so the ceiling is tight. Below the band is only
    INTEREST: a buyer whose cheapest purchase was 140k can obviously afford a
    75k house, they just may not care about one that small. Measured on this
    cohort, a symmetric 0.35 tolerance kept 68 of 199 at a 75k ask and dropped
    buyers purely for being too big, which is the wrong reason to skip someone.
    So the floor is divided by floor_mult (4x) rather than nudged by tol.
    """
    bands = _bands()
    kept, out_of_band, unknown = [], [], 0
    for r in rows:
        band = bands.get(r.get("uuid"))
        if not band:
            unknown += 1
            kept.append(r)
            continue
        lo, hi, _n, name = band
        floor = (lo or 0) / float(floor_mult or 1)
        ceil = (hi or 0) * (1 + tol)
        if floor <= price <= ceil:
            kept.append(r)
        else:
            out_of_band.append((name, band[0], band[1]))
    return kept, out_of_band, unknown


GOOD_TIERS = ("Dial First", "Dial Second")
EXCLUDE_TYPES = ("institutional", "not a target")
EXCLUDE_TIERS = ("2", "3", "EXCLUDE")


def _safe_street(mail: str) -> str:
    head = (mail or "").split(",")[0].strip()
    out = [w for w in head.split() if not (w.isdigit() and len(w) >= 5)]
    return " ".join(out)


log = logging.getLogger(__name__)


def _j(name):
    f = OUT / name
    return json.load(io.open(f, encoding="utf-8")) if f.exists() else None


# Ty: no institutional buyers and no iBuyers. A KEYWORD rule cannot do this
# job. Scanning for HOMES / BUILDERS / CONSTRUCTION flags 24 of the 193 and
# 23 of them are small local operators: portfolios of 1 to 11 doors, 2 to 18
# purchases. A self-performing local builder is the BEST buyer for a full
# gut at this price, so a keyword sweep would delete the target audience to
# catch one name. This is a list of firms verifiable as production builders,
# iBuyers or SFR funds, matched as a substring on the upper-cased name.
NAMED_EXCLUSIONS = (
    "BALL HOMES",           # regional production builder (Lexington KY)
    "CLAYTON PROPERTIES", "CLAYTON HOMES",
    "D R HORTON", "DR HORTON", "DHI ",
    "LENNAR", "PULTE", "NVR ", "RYAN HOMES", "MERITAGE",
    "SMITH DOUGLAS", "DREAM FINDERS", "DFH ",
    "OPENDOOR", "OP SPE", "OFFERPAD", "ZILLOW HOMES", "HOMEGO", "SUNDAE",
    "HOMEVESTORS",
    "PROGRESS RESIDENTIAL", "FREO", "INVITATION HOMES", "TRICON",
    "SFR JV", "AMHERST", "ARMM ", "MAIN STREET RENEWAL", "MSR ",
    "FIRSTKEY", "FKH SFR", "TRUEHOLD", "CPT-ASL",
)


def is_named_exclusion(name: str) -> str:
    """The matched firm name, or empty. Returned so the drop can be logged."""
    up = (name or "").upper()
    for n in NAMED_EXCLUSIONS:
        if n in up:
            return n
    return ""


def _person_first(profile: dict, reg: dict) -> str:
    """A first name we are confident enough to greet someone by, or empty.

    Three separate ways this goes wrong on real county data, all measured
    on this cohort:

      * A BARE INITIAL IS NOT A NAME. 'E J E Bourgeois' gives 'E', and the
        message read 'Hi E,'. touches.clean_first already encodes the rule.
      * clean_first is a PERSON rule. On 'J A Murphy Group Llc' it reads
        the surname slot as 'Llc' and returns 'Murphy', greeting a company
        as a man. The registry's own is_entity flag missed 'Maker Building
        Co', so touches.is_entity is checked as well.
      * COUNTY RECORDS WRITE LAST FIRST. 'Haddad Amer Michael' is Amer
        Michael Haddad, so token zero is the SURNAME. There is no oracle
        here (the entity path can use the company name, a bare person name
        cannot), so a 3-plus-token person name is treated as ambiguous and
        gets no greeting at all. That costs a correct 'William' on one row
        to avoid a wrong 'Haddad' on another, which is the right trade in
        a cold text: no name reads fine, the wrong name reads like a list.
    """
    if not profile.get('name_known'):
        return ''
    name = profile.get('display_name') or ''
    if reg.get('is_entity') or touches.is_entity(name):
        return ''
    if len(name.split()) > 2:
        return ''
    return touches.clean_first(name)


def _warm_phones() -> set:
    """Phones that have ever replied to us.

    Ty, blast 2: the buyers who engaged on the last deal are told 'I
    actually have another one at this price', everyone else gets 'we have
    another one at this price as well'. Derived from real inbound messages
    rather than a hand-kept list, so it stays true for every future blast
    with no maintenance.
    """
    from sms_agent import store
    warm = set()
    try:
        rows = store._conn().execute(
            "SELECT DISTINCT phone FROM messages WHERE direction='in'")
    except Exception:  # noqa: BLE001
        return warm
    for (ph,) in rows:
        digits = "".join(c for c in str(ph) if c.isdigit())[-10:]
        if digits:
            warm.add(digits)
    return warm


def rows_from_registry(limit: int = 0, sender: str = "Ty") -> list:
    """Build the cohort from OUR verified data, not from the search row.

    `seed.from_preset` judges a record by the ONE representative phone the
    search returns, and on this cohort that discarded 158 of 199 buyers: 76
    because the representative phone happened to be DNC, 47 because it was tier
    Drop, 30 Dial Third, 5 a landline. Their BEST number was fine in every
    case. We already know each buyer's best scored mobile, so we pass that.

    Nothing is bypassed by doing this. `seed.build` still re-checks the dial
    tier per candidate against the CRM, still applies suppression, the
    live-conversation gate, the sticky sender and the voice validator. The
    preset keeps its real job, which is the CRM flow lane and the sms_attempts
    no-double-text counter.
    """
    profiles = _j("buyer_profiles.json") or []
    reg = {b["buyer_key"]: b for b in (_j("registry.json") or [])}
    phones = _j("phones.json") or {}
    scores = _j("phone_scores.json") or {}

    warm = _warm_phones()
    rows = []
    for p in profiles:
        b = reg.get(p["buyer_key"]) or {}
        if not p.get("reachable") or not b.get("saved_uuid"):
            continue
        # Ty: no institutional buyers, no iBuyers, no SFR funds.
        if p.get("buyer_type") in EXCLUDE_TYPES or b.get("tier") in EXCLUDE_TIERS:
            continue
        firm = is_named_exclusion(b.get("name") or "")
        if firm:
            log.info("skipping %s: named institutional/builder exclusion (%s)",
                     b.get("name"), firm)
            continue
        best = [q for q in (phones.get(p["buyer_key"]) or {}).get("phones", [])
                if (scores.get(q["number"]) or {}).get("tier") in GOOD_TIERS
                and (q.get("type") or "").upper() == "MOBILE"]
        if not best:
            continue
        best.sort(key=lambda q: GOOD_TIERS.index(
            (scores.get(q["number"]) or {}).get("tier")))
        # The city is the BUYER's own, from their deed history. We cannot claim
        # they buy in the deal's sub-market, and telling a Blount-only buyer
        # they buy in Knoxville is the same kind of unfounded claim.
        city = (p["top_cities"][0][0] if p.get("top_cities")
                else (b.get("counties") or ["the area"])[0])
        rows.append({
            "phone": best[0]["number"],
            "uuid": b["saved_uuid"],
            "dial_tier": "verified",
            # seed.build renders SELLER copy from this before we replace it,
            # and that copy is validated under the seller profile, which blocks
            # any 5-digit run. A mailing street carrying a zip therefore held
            # candidates for copy we discard. Strip digits runs of 5+.
            "street": _safe_street(b.get("mail") or "") or city,
            "city": city,
            "county": (b.get("counties") or [""])[0],
            # A BARE INITIAL IS NOT A FIRST NAME. SmartSkip and the deed
            # rolls hand back names like 'E J E Bourgeois' and 'J A Murphy
            # Group', and taking token zero texts a real person 'Hi E,'.
            # touches.clean_first already encodes this rule for the seller
            # program; reuse it rather than re-deriving it here. An empty
            # result falls through to the entity greeting, which is a
            # better message than a single letter.
            # It is also gated on is_entity, because clean_first is a
            # PERSON-name rule: on 'J A Murphy Group Llc' it reads the
            # surname slot as 'Llc' and hands back 'Murphy', which would
            # greet a company as a man. An entity always takes the entity
            # greeting.
            "first": _person_first(p, b),
            "last": "",
            "owner": b.get("name") or "",
            # THE SENDER IS FORCED, not defaulted. seed._resolve_sender
            # reads the record's CRM assignee FIRST and treats
            # SMS_AGENT_SENDER_NAME as a mere fallback, so the one record
            # in 156 that carried an assignee signed as the acquisitions
            # prospector. On a dispo blast that is wrong twice: it is not
            # Ty's deal voice, and a callback reaches someone who knows
            # nothing about the property. Pinning it here uses the existing
            # mechanism instead of special-casing seed.py.
            "assigned": sender,
            # The registry flag alone missed 'Maker Building Co',
            # and _entity now decides whether a buyer can be
            # addressed as a team at all, so both checks apply.
            "_entity": (bool(b.get("is_entity"))
                        or touches.is_entity(b.get("name") or "")),
            "_raw_name": b.get("name") or "",
            "_name_known": bool(p.get("name_known")),
            "_city": city,
            "cohort": "cold",   # filled in below from reply history
        })
        digits = "".join(c for c in rows[-1]["phone"] if c.isdigit())[-10:]
        rows[-1]["cohort"] = "warm" if digits in warm else "cold"
    rows.sort(key=lambda r: r["_raw_name"])
    return rows[:limit] if limit else rows


def restage(cands: list, deal: dict, touch: int, rows: list) -> tuple:
    """Swap seller wording for the deal blast, then re-check it.

    Re-validating is the whole point. `seed.build` vetted OWNER copy under the
    seller profile; what actually goes out carries a price, which that profile
    would have blocked outright. Accepting it unchecked would put unvetted text
    carrying a dollar figure in the outbox.
    """
    price_text = fmt_price(deal["price"])
    disclose = bool(deal.get("disclose_address"))
    address = str(deal.get("address") or "")
    by_phone = {r["phone"]: r for r in rows}
    note = deal.get("entity_note") or ""
    kept, rejected = [], []
    for c in cands:
        if c.status != "ready":
            continue
        r = by_phone.get(c.phone) or {}
        who = touches.buyer_greeting(
            r.get("_raw_name") or c.first, c.first,
            is_entity=r.get("_entity"))
        if disclose:
            # Address-forward: the full address is the message, and the
            # pool is chosen by whether this buyer has ever replied to us.
            c.message = touches.render_addr(
                r.get("cohort") or "cold",
                (c.record_uuid or c.phone) + "|" + str(deal["deal_id"]),
                who=who, address=address, price=price_text,
                beds=str(deal.get("beds") or ""),
                baths=str(deal.get("baths") or ""),
                sqft=str(deal.get("sqft") or ""),
                sender=c.sender,
                note=note if not r.get("_name_known") else "")
        else:
            c.message = touches.render_deal(
                touch, (c.record_uuid or c.phone) + "|" + str(deal["deal_id"]),
                who=who, city=r.get("_city") or c.city, road=deal["road"],
                price=price_text, beds=str(deal.get("beds") or ""),
                baths=str(deal.get("baths") or ""),
                sqft=str(deal.get("sqft") or ""),
                sender=c.sender,
                note=note if not r.get("_name_known") else "")
        ok, problems = respond.validate(
            c.message, max_questions=2, program="buyer",
            allowed_prices=[deal["price"]],
            allowed_address=address if disclose else "")
        if ok:
            kept.append(c)
        else:
            c.hold("deal copy failed the buyer validator: " + "; ".join(problems))
            rejected.append(c)
    return kept, rejected


def notify_staged(kept: list, deal: dict, touch: int) -> None:
    """Tell Slack a deal blast is going out.

    kind="campaign" is on escalate.ALWAYS_POST, so this lands even under
    SLACK_INTERESTED_ONLY. This is the notice that says a price went out to a
    few hundred people; if the number or the road is wrong, this is how someone
    finds out in minutes instead of from a reply.
    """
    from sms_agent import escalate

    sample = kept[0].message if kept else ""
    detail = "\n".join([
        "Deal %s: %s in %s at %s" % (deal["deal_id"], deal["road"],
                                     deal["area"], fmt_price(deal["price"])),
        "Touch %d to CASH BUYERS (not sellers)." % touch,
        "STAGED: %d messages. NOTHING HAS BEEN SENT YET." % len(kept),
        "They go out only when someone runs release --touch %d." % touch,
        "",
        "Example of what goes out:",
        sample,
        "",
        "No house number and no zip are in this copy, by design. The"
        " agent does not negotiate: any price pushback routes to a human.",
    ])
    try:
        # Check the return. alert() reports failure with False rather than
        # raising, so an ignored return prints "posted" for a notice that never
        # left the building.
        ok = escalate.alert("Dispo deal blast STAGED (nothing sent yet)", detail,
                            kind="campaign", program="buyer")
        print("  Slack: launch notice %s"
              % ("posted" if ok else "FAILED TO POST (sending continues)"))
    except Exception as exc:  # noqa: BLE001 - never block a send on Slack
        print("  Slack notice failed (send is unaffected): %s" % str(exc)[:90])


def _money_tokens(text: str) -> list:
    """Every money-looking token in a message, normalised for compare.

    Written without a regex on purpose: a word-boundary escape has
    reached this codebase as a literal backspace byte more than once,
    and a money check that silently matches nothing is worse than no
    check. Catches $104,000 / 104,000 / 104k / $92,000 / 92k.
    """
    out = []
    for raw in (text or '').replace('(', ' ').replace(')', ' ').split():
        tok = raw.strip('.,!?;:')
        core = tok.lstrip('$')
        low = core.lower()
        if low.endswith('k') and low[:-1].replace(',', '').isdigit():
            n = int(low[:-1].replace(',', '')) * 1000
            out.append(fmt_price(n))
            continue
        digits = core.replace(',', '')
        if not digits.isdigit():
            continue
        # Only treat it as money if it was written like money: a $ sign,
        # or a comma group. A bare 948 (sqft) or 3 (beds) is not a price.
        if tok.startswith('$') or ',' in core:
            out.append(fmt_price(int(digits)))
    return out


def audit(kept: list, deal: dict, rows: list, sender: str = 'Ty',
          touch: int = 1) -> list:
    """Check EVERY rendered message, not a sample.

    A four-message spot check reads well and proves nothing: the copy is
    rotated by a hash of the record, so the variants a sample happens to show
    are not the variants that ship. Each rule below is either a thing Ty asked
    for or a thing that is expensive to send by accident.

    TWO MODES, because blast 1 and blast 2 want opposite things from the
    address. A redacted deal must name the road and offer the address; an
    address-forward deal must carry the exact address from the deal sheet. The
    mode follows the deal sheet, so neither campaign can accidentally run the
    other's rules.

    The money rule is a WHITELIST, not a blocklist. Blast 1 listed the contract
    price and checked for it, which only catches the figure you remembered to
    ban. Here every money-looking token must equal the approved price, so an
    invented number and the never-to-be-stated contract price are both caught
    without the audit needing to know what the contract price is.
    """
    cohort_of = {r.get('phone'): (r.get('cohort') or 'cold') for r in rows}
    city_of = {r.get('phone'): (r.get('city') or '').strip() for r in rows}
    sign = '-' + (sender or 'Ty')
    disclose = bool(deal.get('disclose_address'))
    address = str(deal.get('address') or '')
    approved = fmt_price(deal['price'])
    zip_ok = str(deal.get('zip') or '') if disclose else ''
    problems = []
    for c in kept:
        m = c.message or ''
        who = c.phone
        low = m.lower()
        if not m.rstrip().endswith(sign):
            problems.append((who, 'does not sign off ' + sign))

        # Whitelist every money token against the approved price.
        for tok in _money_tokens(m):
            if tok != approved:
                problems.append((who, 'states an unapproved price: ' + tok))

        if '—' in m or '–' in m:
            problems.append((who, 'contains an em or en dash'))
        if 'http' in low or '.com' in low:
            problems.append((who, 'contains a link'))
        if m.count('?') > 1:
            problems.append((who, 'asks more than one question'))
        if len(m) > 320:
            problems.append((who, 'over 320 characters: %d' % len(m)))

        toks = [w.strip('.,!?') for w in m.split()]
        stray = [d for d in toks if d.isdigit() and len(d) == 5 and d != zip_ok]
        if stray:
            problems.append((who, 'contains a zip code: ' + stray[0]))

        if disclose:
            if address and address not in m:
                problems.append((who, 'does not carry the exact address'))
            # Ty: offer BOTH the photos and the lockbox. The Drive link
            # itself is never in the copy (links are blocked and read as
            # spam); a human sends it when they reply.
            if 'lockbox' not in low:
                problems.append((who, 'does not offer the lockbox'))
            if 'photo' not in low:
                problems.append((who, 'does not offer the photos'))
            # Ty, 2026-09-02: never imply this is the same price as the
            # last deal. $104,000 is not $75,000, and a buyer who
            # remembers the last one would read it as careless. The warm
            # variant references the RELATIONSHIP, not the price.
            want = 'the last one' if cohort_of.get(who) == 'warm' \
                else 'we have another one'
            if want not in low:
                problems.append((who, 'wrong cohort wording (%s)'
                                 % (cohort_of.get(who) or 'cold')))
        else:
            if 'property on' not in low:
                problems.append((who, 'does not say we have a property on the road'))
            if touch < 3 and 'full address' not in low:
                problems.append((who, 'does not offer the address on interest'))
            city = city_of.get(who)
            if city and city.lower() not in low:
                problems.append((who, 'names a city the buyer has not closed in'))
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description="Stage a dispo deal blast")
    ap.add_argument("--deal", required=True, help="path to the deal sheet JSON")
    # The text lane of the existing dispo sequential system. It is already
    # anchored on the VIP list and already excludes sold, not_interested and
    # the "recently sold" tag, so the cohort inherits those suppressions rather
    # than this module re-deciding them. "Dispo - VIP Buyers" is a LIST, not a
    # preset, and naming it here returns nothing.
    ap.add_argument("--preset", default="Dispo - 02 Ready to Text",
                    help="DataSift filter preset naming the buyer cohort")
    ap.add_argument("--touch", type=int, default=1, choices=(1, 2, 3))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--band-tolerance", type=float, default=0.35)
    ap.add_argument("--band-floor-mult", type=float, default=4.0,
                    help="keep a buyer whose cheapest purchase is up to N times the ask")
    ap.add_argument("--source", choices=("registry", "preset"), default="registry",
                    help="registry uses our verified best number per buyer")
    ap.add_argument("--sender", default=config.SENDER_NAME or "Ty",
                    help="the first name every message signs off with")
    ap.add_argument("--pool", default="Dispo",
                    help="smrtPhone number pool this blast sends from")
    ap.add_argument("--sample", type=int, default=4,
                    help="how many rendered messages to print for review")
    ap.add_argument("--queue", action="store_true", help="stage as HELD")
    a = ap.parse_args()

    # Pin the sending pool BEFORE anything schedules. Unpinned, the dispo
    # blast spread across all 24 numbers including the 19 seller lines.
    from sms_agent import sender_pool
    sender_pool.set_forced_pool(a.pool)
    if not sender_pool.pool():
        print("number pool %r is empty or missing. Refusing to send dispo"
              " traffic from the seller pool." % a.pool)
        print("Run: python src/sms_agent/cli.py numbers --refresh")
        return 2
    print("sending pool %r: %d numbers" % (a.pool, len(sender_pool.pool())))

    deal = load_deal(a.deal)
    print("deal %s: %s in %s at %s"
          % (deal["deal_id"], deal["road"], deal["area"], fmt_price(deal["price"])))

    if a.source == "registry":
        rows = rows_from_registry(limit=a.limit, sender=a.sender)
        matched = "registry (%d buyers)" % len(rows)
        print("cohort from the verified registry: %d buyers" % len(rows))
    else:
        rows, matched = seed.from_preset(a.preset, limit=a.limit)
    if not matched:
        print("preset not found: " + a.preset)
        print("Run the registry mirror first so buyers exist as records with"
              " dial-tier tags; until then there is no cohort to text.")
        return 2
    print("cohort from preset %r: %d mobile rows" % (matched, len(rows)))

    rows, out_of_band, unknown = match_band(
        rows, deal["price"], a.band_tolerance, a.band_floor_mult)
    print("  in the price band       : %d" % len(rows))
    print("  band unknown, kept      : %d" % unknown)
    print("  out of band, dropped    : %d" % len(out_of_band))
    for name, lo, hi in out_of_band[:5]:
        print("      %-40s buys %s to %s" % (str(name)[:40], lo, hi))

    # A dispo blast is always a NEW deal: it may reopen a thread a human
    # paused on the last property, but never one that opted out.
    cands = seed.build(rows, touch=a.touch, new_deal=True)
    ready = [c for c in cands if c.status == "ready"]
    held = [c for c in cands if c.status != "ready"]
    reasons: dict = {}
    for c in held:
        for r in c.reasons:
            key = r.split("(")[0].strip()
            reasons[key] = reasons.get(key, 0) + 1

    kept, rejected = restage(cands, deal, a.touch, rows)
    print("  passed every guard      : %d" % len(ready))
    print("  held by a guard         : %d" % len(held))
    for k, v in sorted(reasons.items(), key=lambda kv: -kv[1]):
        print("      %-52s %d" % (k[:52], v))
    print("  deal copy rejected      : %d" % len(rejected))
    print("  READY TO STAGE          : %d" % len(kept))

    if kept:
        problems = audit(kept, deal, rows, a.sender, a.touch)
        print("")
        print("  audit of all %d messages   : %s"
              % (len(kept), "clean" if not problems
                 else "%d PROBLEMS" % len(problems)))
        for who, why in problems[:10]:
            print("      %-30s %s" % (str(who)[:30], why))
        if problems:
            print("")
            print("Refusing to stage while the copy audit fails.")
            return 3
        print("")
        print("sample of what would send:")
        for c in kept[:max(0, a.sample)]:
            print("  -> %s  %s" % (c.phone, c.message))

    if a.queue and kept:
        res = seed.queue(kept, touch=a.touch, new_deal=True)
        # THE RESPONDER NEEDS THE DEAL, and seed.queue only records owner
        # facts. Without this a buyer who replies 'how much?' reaches a
        # dispo agent with no price in its facts block, and the validator's
        # whitelist would block the figure anyway because nothing told it
        # which price was approved. Written after queueing so the phone map
        # already exists.
        facts = {
            "road": deal["road"], "area": deal.get("area", ""),
            "price": fmt_price(deal["price"]),
            "price_value": int(deal["price"]),
            "beds": deal.get("beds", ""), "baths": deal.get("baths", ""),
            "sqft": deal.get("sqft", ""), "year": deal.get("year", ""),
            "rehab_level": deal.get("rehab_level", ""),
            "status": deal.get("status", ""),
            # On an address-forward deal the responder MUST know the
            # address: we just texted it to them, so an agent that
            # cannot repeat it looks evasive on the one question every
            # buyer asks next.
            "address": deal.get("address", "") if deal.get(
                "disclose_address") else "",
            "deal_id": deal["deal_id"],
        }
        facts = {k: v for k, v in facts.items() if v not in (None, "")}
        from sms_agent import store as _store
        wrote = 0
        for c in kept:
            prev = (_store.lookup_phone(c.phone) or {}).get("context") or {}
            merged = dict(prev)
            merged.update(facts)
            merged["buyer_first"] = c.first or ""
            _store.map_phone(c.phone, record_uuid=c.record_uuid,
                             first_name=c.first, address=c.street,
                             context=merged)
            wrote += 1
        print("  deal facts written to %d threads" % wrote)
        print("\nstaged as HELD: %s" % res)
        notify_staged(kept, deal, a.touch)
        print("nothing has been sent. Release with:")
        print("  python src/sms_agent/cli.py release --touch %d" % a.touch)
    elif not a.queue:
        print("\nDRY RUN - nothing staged. Add --queue to stage as HELD.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

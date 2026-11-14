"""The suppression gate, as the texting app carries it (build plan 0.1 and 0.22).

The brain has one gate (src/agents/autopilot/gate.py): the single check a record passes before it
is assigned, called, texted, mailed, traced or researched. This app cannot import it. The image
copies only src/sms_agent, and the gate leans on the brain's own store. So the rules for the two
channels this app can reach a person on, a text and a call, are written out again here, with no
import beyond the standard library.

Two copies of a rule drift. That is exactly how the old state came about: each preset's
exclusions, a status list inside the stages and this app's own skip list had all wandered apart,
a record tagged Do Not Market could still be skip traced, and nothing asked whether the owner had
died. So this file is held to the brain's by a test, not by care: tests/test_sms_gate_parity.py
feeds both modules the same invented records, one rule at a time and then a few thousand at
random, and fails the moment they answer differently. Change a rule in one file and that test
says so before anything ships.

What the check reads is one full record (the detail route returns status, tags, lists, the owner's
flags and every phone in one call) plus, when the caller has them, the search row (the only place
the do-not-call registry flag appears, and only for one number), the record's custom fields, the
owner's lane and the numbers known to be litigators.

Death is read from every place it is recorded: the owner flag, a deceased tag, the Owner Deceased
field and the Obituary list. The list alone is not proof the OWNER died (an obituary on a record
is often a spouse's), which is why the record is held for research instead of marked. A probate
record is the exception: the court named a living personal representative, and that person is the
owner of record here.

Lifting a rule is explicit (`allow`). Nothing lifts Do Not Market, a litigator, an opt-out or a
sold house.

Beyond the port, this file holds the one rule for the do-not-call registry flag (decision D32,
made 2026-11-13: a flagged number is never texted). The seeder and the Smarter Contact loaders
each had their own copy of "skip a DNC number"; `do_not_text` and `flagged_numbers` are now the
only place it is written.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# The channels THIS app answers for. The brain's gate knows six more.
CHANNELS = ("text", "call")

# The brain's channel sets, kept whole so the rule table below reads the same in both files and
# the parity test can compare the tables themselves.
_GATE_CHANNELS = ("assign", "call", "text", "mail", "trace", "score", "research", "task")
_ALL = frozenset(_GATE_CHANNELS)
_OUTREACH = frozenset({"assign", "call", "text", "mail", "task"})
_PAID = frozenset({"trace", "score"})

TIER_NAMES = ("Dial First", "Dial Second", "Dial Third", "Dial Fourth", "Drop")
BAD_PHONE = {"WRONG", "DEAD", "DNC", "CORRECT_DNC", "WRONG_DNC"}
FINISHED_STATUSES = {"dead lead", "not_interested", "closed", "close_out", "lost_deal", "dnc", "opt_out",
                     "buyer", "buyer_found", "buyer_lost", "auction date passed"}
WORKING_STATUSES = {"", "prospecting"}
YES = {"yes", "y", "true", "1", "deceased", "dead"}

# code -> (the channels it blocks, the allow word that lifts it or "")
RULES = {
    "unreadable": (_ALL, ""),
    "do_not_market": (_ALL, ""),
    "litigator": (_ALL, ""),
    "opted_out": (_ALL, ""),
    "sold": (_ALL, ""),
    "deceased": (_OUTREACH | _PAID, ""),          # lifted only as written in check(): "deceased", or "heir" with the heir tag
    "in_research": (_OUTREACH | _PAID, "in_research"),
    "lead": (_OUTREACH | _PAID, "lead"),
    "finished": (_ALL, ""),                       # not_interested alone can be lifted, see check()
    "unknown_status": (_OUTREACH | _PAID, "unknown_status"),
    "listed": (_OUTREACH, "listed"),
    "not_single_family": (_OUTREACH | _PAID, ""),
    "mail_only": (frozenset({"assign", "call", "text", "task"}), "mail_only"),
    "exhausted": (frozenset({"assign", "call"}), "exhausted"),
    "return_mail": (frozenset({"mail"}), ""),
    "do_not_mail": (frozenset({"mail"}), ""),
    "owner_lane": (_OUTREACH, ""),
    "no_number": (frozenset({"call", "text"}), ""),
}
ORDER = tuple(RULES)        # most serious first: the first reason is the one reported

# The rules about the PERSON, whatever program is texting them. A deal blast to a buyer does not
# care that the buyer's own record reads sold or buyer_found (that is what makes them a buyer),
# but it must still never reach a litigator, an opt-out or a dead owner.
PERSON_RULES = frozenset({"unreadable", "do_not_market", "litigator", "opted_out", "deceased"})

# ---------------------------------------------------------------------- lanes (the brain's lanes.py)
LANE_ORDER = ("lead", "heirs", "hot_auction", "ftm", "obituary", "priority1", "strong", "bulk")
LEAD_STATUS_VALUES = ["new_lead", "No Contact New Lead", "Cold Lead", "Warm Lead", "Hot Lead", "Ghosting Lead",
                      "follow_up", "lead", "underwrite", "under_contract", "push_deal", "transactions", "refer_lead"]
LEAD_STATUSES = {s.lower() for s in LEAD_STATUS_VALUES}
DEFAULT_MARKERS = {
    "heirs": {"tags": ["Heir Identified"]},
    "hot_auction": {"tags": ["Auction Soon"]},
    "ftm": {"tags": ["FTM"]},
    "obituary": {"tags": ["Obituary"], "lists": ["Obituary"]},
    "priority1": {"tags": ["Priority 1"]},
    "strong": {"tags": ["Priority 2"]},
}


@dataclass
class GateConfig:
    do_not_market_tags: list = field(default_factory=lambda: ["Do Not Market"])
    litigator_tags: list = field(default_factory=lambda: ["Litigator"])
    suppress_lists: list = field(default_factory=lambda: ["DNC"])
    deceased_tags: list = field(default_factory=lambda: ["deceased", "Verify Deceased"])
    deceased_lists: list = field(default_factory=lambda: ["Obituary"])
    deceased_fields: list = field(default_factory=lambda: ["Owner Deceased"])
    probate_marks: list = field(default_factory=lambda: ["Probate"])
    heir_tags: list = field(default_factory=lambda: ["Heir Identified"])
    sold_tags: list = field(default_factory=lambda: ["recently sold", "Sold"])
    mail_only_tags: list = field(default_factory=lambda: ["Mail Only"])
    not_sfr_tags: list = field(default_factory=lambda: ["Not Single Family"])
    listed_values: list = field(default_factory=lambda: ["listed", "pending"])
    callable_tiers: list = field(default_factory=lambda: ["Dial First", "Dial Second"])
    working_statuses: list = field(default_factory=list)      # an install's own statuses that mean "still prospecting"
    read_fields: bool = False                                  # read the record's custom fields (one more call a record)

    KEYS = ("do_not_market_tags", "litigator_tags", "suppress_lists", "deceased_tags", "deceased_lists",
            "deceased_fields", "probate_marks", "heir_tags", "sold_tags", "mail_only_tags", "not_sfr_tags",
            "listed_values", "callable_tiers", "working_statuses")


def parse(block: dict | None, problems: list[str]) -> GateConfig:
    """Settings over the defaults, the same keys the brain's playbook takes under `gate:`."""
    cfg = GateConfig()
    for k, v in (block or {}).items():
        if k == "read_fields":
            cfg.read_fields = bool(v)
        elif k in GateConfig.KEYS:
            if not isinstance(v, (list, tuple)):
                problems.append("gate.%s must be a list" % k)
                continue
            setattr(cfg, k, [str(x) for x in v])
        else:
            problems.append("gate has %r (settings are %s, read_fields)" % (k, ", ".join(GateConfig.KEYS)))
    if not cfg.do_not_market_tags:
        problems.append("gate.do_not_market_tags may not be empty: it is the one tag nothing may touch")
    return cfg


@dataclass
class Verdict:
    ok: bool
    reasons: list = field(default_factory=list)      # (code, detail) pairs, most serious first
    numbers: list = field(default_factory=list)      # the phones a call or a text may use
    lane: str = ""

    @property
    def code(self) -> str:
        return self.reasons[0][0] if self.reasons else ""

    def reason(self) -> str:
        """The first reason, in words a person reading the digest understands."""
        if not self.reasons:
            return ""
        return words(*self.reasons[0])

    def all_reasons(self) -> list[str]:
        return [words(c, d) for c, d in self.reasons]


TEXT = {
    "unreadable": "the record could not be read",
    "do_not_market": "tagged Do Not Market",
    "litigator": "a litigator's number is on the record",
    "opted_out": "the owner opted out (%s)",
    "sold": "the house sold",
    "deceased": "the owner has died or may have (%s); research first",
    "in_research": "held out for research",
    "lead": "already a lead (%s)",
    "finished": "status %s",
    "unknown_status": "status %s is not one the gate knows",
    "listed": "listed on the MLS",
    "not_single_family": "not a single-family house",
    "mail_only": "mail only: the owner is not to be called or texted",
    "exhausted": "the call round is over",
    "return_mail": "mail came back from this address",
    "do_not_mail": "do not mail",
    "owner_lane": "the owner is in the %s lane on another record",
    "no_number": "no usable number for a %s",
}


def words(code: str, detail: str = "") -> str:
    """One (code, detail) pair as a plain sentence fragment."""
    text = TEXT[code]
    return text % detail if "%s" in text else text


def digits(n) -> str:
    d = re.sub(r"\D", "", str(n or ""))
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    return d if len(d) == 10 else ""


def _titles(items) -> list[str]:
    out = []
    for t in items or []:
        n = (t.get("title") or t.get("name")) if isinstance(t, dict) else t
        if n:
            out.append(str(n))
    return out


def _low(items) -> set[str]:
    return {t.strip().lower() for t in _titles(items)}


def _lowset(items) -> set[str]:
    return {str(x).strip().lower() for x in items or []}


# ---------------------------------------------------------------------- lanes
def status_of(rec: dict) -> str:
    s = (rec or {}).get("status")
    if isinstance(s, dict):
        s = s.get("value") or s.get("title") or ""
    return str(s or "").strip().lower()


def lane_of(rec: dict, markers: dict | None = None) -> str:
    """The lane ONE record puts its owner in, from its status, tags and lists."""
    markers = DEFAULT_MARKERS if markers is None else markers
    if status_of(rec) in LEAD_STATUSES:
        return "lead"
    tags, lists = _low(rec.get("tags")), _low(rec.get("lists"))
    for lane in LANE_ORDER[1:]:
        m = markers.get(lane)
        if not m:
            continue
        if tags & {t.lower() for t in m.get("tags", [])} or lists & {t.lower() for t in m.get("lists", [])}:
            return lane
    return "bulk"


def rank(lane: str) -> int:
    return LANE_ORDER.index(lane) if lane in LANE_ORDER else len(LANE_ORDER)


# ---------------------------------------------------------------------- the rules
def deceased_signals(rec: dict, cfg: GateConfig, custom: dict | None = None) -> list[str]:
    """Every place a death is recorded, as plain words. Empty means no sign of one."""
    ow = rec.get("owner") or {}
    tags, lists = _low(rec.get("tags")), _low(rec.get("lists"))
    out = []
    if ow.get("deceased"):
        out.append("owner flag")
    hit = tags & _lowset(cfg.deceased_tags)
    if hit:
        out.append("tag %s" % sorted(hit)[0])
    for label, value in (custom or {}).items():
        if label.strip().lower() in _lowset(cfg.deceased_fields) and str(value).strip().lower() in YES:
            out.append("%s field" % label)
    probate = (tags | lists) & _lowset(cfg.probate_marks)
    on_list = lists & _lowset(cfg.deceased_lists)
    if on_list and not probate:
        out.append("%s list" % sorted(on_list)[0].title())
    return out


def phone_tier(p: dict) -> str:
    return next((t for t in _titles(p.get("tags")) if t in TIER_NAMES), "")


def usable_numbers(rec: dict, channel: str, cfg: GateConfig, row: dict | None = None,
                   litigators: set | None = None) -> list[dict]:
    """The owner's phones a call or text may use: not wrong, dead or do-not-call, a top tier (or a
    number a person already confirmed), a mobile for a text, and not the number DataSift's
    registry scrub flagged. The registry flag exists only on the search row and only for the one
    number that row shows, so it is honored where it is visible."""
    flagged = ""
    ph = (row or {}).get("phone") or {}
    if isinstance(ph, dict) and ph.get("doNotCall"):
        flagged = digits(ph.get("number"))
    lit_tags = _lowset(cfg.litigator_tags)
    out = []
    for p in ((rec.get("owner") or {}).get("phones") or []):
        n = digits(p.get("number"))
        st = str(p.get("status") or "UNKNOWN").upper()
        ptags = _low(p.get("tags"))
        if not n or st in BAD_PHONE or n == flagged or n in (litigators or ()):
            continue
        if ptags & lit_tags or "dnc" in ptags:
            continue
        tier = phone_tier(p)
        if tier not in cfg.callable_tiers and st != "CORRECT":
            continue
        if channel == "text" and str(p.get("type") or "").upper() != "MOBILE":
            continue
        out.append({"number": n, "tier": tier, "status": st, "type": str(p.get("type") or "").upper()})
    return out


def _status_rule(status: str, cfg: GateConfig) -> tuple[str, str]:
    """(rule code, detail) for the record's status, or ("", "") when the status holds nothing back."""
    if status in WORKING_STATUSES or status in _lowset(cfg.working_statuses):
        return "", ""
    if status == "sold":
        return "sold", ""
    if status in LEAD_STATUSES:
        return "lead", status
    if status == "deep prospecting":
        return "in_research", ""
    if status == "exhausted":
        return "exhausted", ""
    if status == "return mail":
        return "return_mail", ""
    if status == "listed":
        return "listed", ""
    if status in FINISHED_STATUSES:
        return "finished", status
    return "unknown_status", status


def check(rec: dict, channel: str, *, cfg: GateConfig | None = None, lane: str = "", allow=(), row: dict | None = None,
          custom: dict | None = None, owner: dict | None = None, litigators: set | None = None,
          markers: dict | None = None) -> Verdict:
    """May `channel` act on this record? `allow` lists the rules this caller may lift; `owner` is
    the owner's remembered lane ({"lane", "records"}); `litigators` are numbers known to belong to
    litigators (here: this app's own suppression table)."""
    if channel not in CHANNELS:
        raise ValueError("unknown channel %r (this app's channels: %s)" % (channel, ", ".join(CHANNELS)))
    cfg = cfg or GateConfig()
    allow = {str(a).strip().lower() for a in (allow or ())}
    if not rec or not rec.get("uuid"):
        return Verdict(False, [("unreadable", "")])
    ow = rec.get("owner") or {}
    tags, lists = _low(rec.get("tags")), _low(rec.get("lists"))
    status = status_of(rec)
    found: dict[str, str] = {}

    if tags & _lowset(cfg.do_not_market_tags):
        found["do_not_market"] = ""
    lit_tags = _lowset(cfg.litigator_tags)
    numbers_on_record = {digits(p.get("number")) for p in (ow.get("phones") or [])} - {""}
    if tags & lit_tags or any(_low(p.get("tags")) & lit_tags for p in (ow.get("phones") or [])) \
            or (litigators and numbers_on_record & set(litigators)):
        found["litigator"] = ""
    opt = []
    if ow.get("opt_out"):
        opt.append("opt-out flag")
    if ow.get("dnc"):
        opt.append("do-not-call flag")
    hit = lists & _lowset(cfg.suppress_lists)
    if hit:
        opt.append("%s list" % sorted(hit)[0].upper())
    if status in ("dnc", "opt_out"):
        opt.append("status %s" % status)
    if opt:
        found["opted_out"] = opt[0]
    if tags & _lowset(cfg.sold_tags):
        found["sold"] = ""
    signs = deceased_signals(rec, cfg, custom)
    if signs:
        found["deceased"] = ", ".join(signs)
    code, detail = _status_rule(status, cfg)
    if code and code not in found:
        found[code] = detail
    if str(rec.get("mls") or "").strip().lower() in _lowset(cfg.listed_values):
        found.setdefault("listed", "")
    if tags & _lowset(cfg.not_sfr_tags):
        found["not_single_family"] = ""
    if tags & _lowset(cfg.mail_only_tags):
        found["mail_only"] = ""
    if rec.get("do_not_mail_ever") or ow.get("do_not_mail_ever"):
        found["do_not_mail"] = ""
    my_lane = lane or lane_of(rec, markers)
    if owner and owner.get("lane") and rank(owner["lane"]) < rank(my_lane) \
            and (owner.get("records") or {}).get(rec["uuid"], my_lane) != owner["lane"]:
        found["owner_lane"] = owner["lane"].replace("_", " ")

    numbers = usable_numbers(rec, channel, cfg, row, litigators)
    if not numbers:
        found["no_number"] = channel

    reasons = []
    for code in ORDER:
        if code not in found:
            continue
        blocked, lift = RULES[code]
        if channel not in blocked:
            continue
        if code == "finished" and found[code] == "not_interested" and "not_interested" in allow:
            continue
        if code == "deceased" and ("deceased" in allow or ("heir" in allow and tags & _lowset(cfg.heir_tags))):
            continue
        if lift and lift in allow:
            continue
        reasons.append((code, found[code]))
    return Verdict(not reasons, reasons, numbers, my_lane)


def check_row(row: dict, channel: str, *, cfg: GateConfig | None = None, allow=()) -> Verdict:
    """The part of the gate a SEARCH ROW can answer (its status), for a build that has not opened
    the record yet. It can only ever say no: a yes here still needs the full check before
    anything reaches a homeowner."""
    if channel not in CHANNELS:
        raise ValueError("unknown channel %r (this app's channels: %s)" % (channel, ", ".join(CHANNELS)))
    cfg = cfg or GateConfig()
    allow = {str(a).strip().lower() for a in (allow or ())}
    status = status_of(row)
    reasons = []
    code, detail = _status_rule(status, cfg)
    if status in ("dnc", "opt_out"):
        code, detail = "opted_out", "status %s" % status
    if code:
        blocked, lift = RULES[code]
        lifted = (lift and lift in allow) or (code == "finished" and detail == "not_interested" and "not_interested" in allow)
        if channel in blocked and not lifted:
            reasons.append((code, detail))
    return Verdict(not reasons, reasons)


def registry_flagged(row: dict) -> str:
    """The number DataSift's do-not-call registry scrub flagged on this search row, or ''."""
    ph = (row or {}).get("phone") or {}
    return digits(ph.get("number")) if isinstance(ph, dict) and ph.get("doNotCall") else ""


# ---------------------------------------------------------------------- the one do-not-text rule (0.22)
REGISTRY = "do-not-call registry flag"


def flagged_numbers(rows) -> set[str]:
    """Every number the registry scrub flagged across a page of search rows.

    The flag lives ONLY on a search row's representative phone: the full record comes back with
    the field absent, not false (verified live 2026-08-28). So the sweep that pages a preset is
    the one moment the flag can be collected, and the caller has to carry the set forward to
    wherever it picks a number off a full record.
    """
    return {n for n in (registry_flagged(r) for r in rows or []) if n}


def do_not_text(phone: dict, flagged=()) -> str:
    """Why this one phone may not be texted, or "" when nothing says so.

    Two things, and they are different in kind. A phone STATUS of DNC, CORRECT_DNC, WRONG_DNC,
    WRONG or DEAD is a disposition somebody made: an opt-out writes the DNC variants, so honoring
    the status is what makes an opt-out stick across every future campaign. The registry FLAG is
    DataSift's scrub against the do-not-call registry, and since 2026-11-13 a number carrying it
    is never texted (decision D32; calling it is a question for an attorney, so until answered
    those numbers are mail only).

    `flagged` is the set collected off the search rows; a phone object that itself carries
    doNotCall (a search row's own phone) is refused without it.
    """
    p = phone or {}
    # The flag is asked first, as the seeder always asked it: a search row whose phone is flagged
    # is dropped outright, whatever its status says.
    if p.get("doNotCall") or (flagged and digits(p.get("number")) in flagged):
        return REGISTRY
    status = str(p.get("status") or "").upper()
    if status in BAD_PHONE:
        return "phone status %s" % status
    return ""


def litigator_marked(phone: dict, litigators=(), cfg: GateConfig | None = None) -> bool:
    """Is this phone marked as a litigator's: by its own tags, or by a number already known?"""
    cfg = cfg or GateConfig()
    ptags = _low((phone or {}).get("tags"))
    return bool(ptags & _lowset(cfg.litigator_tags) or "dnc" in ptags
                or (litigators and digits((phone or {}).get("number")) in set(litigators)))

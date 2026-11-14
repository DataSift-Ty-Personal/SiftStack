"""Preset patch (build plan 0.2 and 0.6): the exclusions every marketing preset should carry.

The account describes its whole prospecting flow as saved presets, and each preset repeats its own
list of who to leave out. Those lists drifted: no preset skips the Do Not Market tag, the call lanes
dial a lead that nobody reached yet, a dead owner can be dialed and mailed by name, the mail lanes
skip the Exhausted status although exhausted records are exactly who gets mail, and the exhausted
preset overlaps the last call attempt. This tool works out, preset by preset, what is missing and
writes the change list for the owner to approve.

Three rules hold it together:

  * It only ADDS. A preset keeps every exclusion the owner gave it, in the order it was written;
    the tool appends what is missing and never adds a thing twice. The two exceptions are named in
    the table below and shown as their own lines in the plan: mail presets stop skipping the
    Exhausted status, and the exhausted preset moves to the end of the five-attempt round.
  * A rule counts only when a count moves. The records search silently ignores a key it does not
    know, so acceptance proves nothing. Before a rule is proposed the search is asked with and
    without it; a rule whose count cannot move is held back and reported, never applied on faith.
  * `plan` reads and counts, nothing else. Its only door to the account is a reader that can list
    and count. `apply` exists for the day the plan is approved: it is dry unless BOTH --commit is
    passed and PRESET_PATCH_ALLOW=1 is set, it sends exactly the filter the plan recorded, reads
    each preset back for an exact match and stops on the first difference.

Two rules are counted on every preset and NOT added, because the filter panel cannot say them
without taking the wrong records too (measured on the live account, 2026-11-12). "Skip a deceased
owner" can only be said as "deceased: no", which also drops every record whose mark is blank: it
waits until the blank marks are set. "Skip the Obituary list" would take the probate records out
of the first-to-market lane, where the court has named a living representative: that lane keeps
them. Both are switches in the table, and the plan prints what each would remove.

Every route here is in the published API. The account's statuses come from the published listing
(which names only the lead pipeline) and from the table below.

The client is the autopilot's own (crm.CRM), built on the Open API key alone. On a workstation
whose .env also holds a login, a bare CRM() would mint a token first, which is a POST to the token
route; this tool never logs in.

    python src/agents/autopilot/preset_patch.py plan
    python src/agents/autopilot/preset_patch.py plan --date 2026-11-13 --cache <file> --max-requests 450
    python src/agents/autopilot/preset_patch.py apply --plan output/autopilot/preset_patch/plan_<day>.json
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path

SRC = Path(__file__).resolve().parents[2]
ROOT = SRC.parent
_HERE = str(Path(__file__).resolve().parent)
if __name__ == "__main__":
    # Run as a script, this folder would be first on the path and its modules (mail, policy,
    # windows) would shadow anything else of the same name. Only src belongs there.
    sys.path[:] = [p for p in sys.path if os.path.abspath(p or os.getcwd()) != _HERE]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agents.autopilot import windows                     # noqa: E402
from agents.autopilot.crm import rows_of, search_shape   # noqa: E402

OUT_DIR = ROOT / "output" / "autopilot" / "preset_patch"
ALLOW_ENV = "PRESET_PATCH_ALLOW"

# ==================================================================================================
# THE TABLE. Every rule this tool knows is in this block. Edit it, run `plan` again, read the plan.
# ==================================================================================================

# The call round (decision D1): Ready to Call is counter 0, Call Attempt 1 to 4 are counters 1 to
# 4, and a record is exhausted when the counter reads 5.
ROUND_ENDS_AT = 5

# Statuses exactly as the account stores them (a search needs the stored spelling). These three
# lists are also everything the tool knows about the account's statuses: the published API lists
# only the statuses of the lead pipeline (8 of this account's 30 on 2026-11-13), so that listing is
# read as a live check of spellings and this table is the rest. A status that is in neither is
# reported as unknown wherever a preset names it. Add a new custom status to the list it belongs in.
LEAD_STATUSES = ["new_lead", "No Contact New Lead", "Cold Lead", "Warm Lead", "Hot Lead", "Ghosting Lead",
                 "follow_up", "lead", "underwrite", "under_contract", "push_deal", "transactions", "refer_lead"]
FINISHED_STATUSES = ["Dead Lead", "not_interested", "closed", "close_out", "lost_deal", "dnc", "opt_out",
                     "listed", "sold", "buyer", "buyer_found", "buyer_lost", "Auction Date Passed"]
WORKING_STATUSES = ["prospecting", "Deep Prospecting", "Exhausted", "Return Mail"]

# A number still worth dialing, in the owner's own words: the exhausted preset already says "every
# number wrong or dead" as "no number in one of these four states".
LIVE_NUMBER = ["CORRECT", "CORRECT_DNC", "NO_ANSWER", "UNKNOWN"]

# The research preset the exhausted preset is split into (see exhausted_rewrite).
NO_LIVE_NUMBER_TITLE = "03b No Live Number -> DP"

# One pack is one reason to leave a record out. Exactly one of these per pack:
#   skip_tags / skip_lists / skip_statuses   added to the preset's exclusions
#   require                                  a filter the record must pass (added only when absent)
#   skip                                     any other excluding filter
#   stop_skipping_statuses                   the one deliberate removal
PACKS = {
    "do_not_market": {
        "skip_tags": ["Do Not Market"],
        "say": "Skip records tagged Do Not Market",
        "why": "A person who asked to be left alone is not called, texted, mailed or researched."},
    "litigator": {
        "skip_tags": ["Litigator"],
        "say": "Skip records tagged Litigator",
        "why": "A known litigator's number on a record takes the whole record out of every channel."},
    "dnc_list": {
        "skip_lists": ["DNC"],
        "say": "Skip records on the DNC list",
        "why": "A texted stop lands on this list; no marketing preset looked at it."},
    "lead_statuses": {
        "skip_statuses": LEAD_STATUSES,
        "say": "Skip every lead status",
        "why": "A lead belongs to the lead manager. Without No Contact New Lead and Follow Up an unreached "
               "lead falls back into the dialer and the mail."},
    "finished_statuses": {
        "skip_statuses": FINISHED_STATUSES,
        "say": "Skip every finished status",
        "why": "A record that is sold, dead, opted out or past its auction is not prospected again."},
    "dead_owner": {
        "require": {"deceased": 0},
        "say": "Skip records whose owner is marked deceased",
        "why": "A dead owner is never dialed or mailed by name; the record waits for the heir lane.",
        # The filter panel can only say "deceased: no", and a record whose mark is BLANK (neither
        # yes nor no) fails that test too. Measured 2026-11-12: of 255 preset-records the rule would
        # remove, 68 were marked deceased and 187 only had a blank mark, most of them living owners
        # whose skip trace found nothing. So the rule waits while any mark is blank, and the plan
        # shows what it would remove. Set False to add it anyway.
        "hold_if_blank": True},
    "heir_tag": {
        "skip_tags": ["Heir Identified"],
        "say": "Skip records tagged Heir Identified",
        "why": "Once research names a signer the record is worked by the heir lane only."},
    "obituary_list": {
        "skip_lists": ["Obituary"],
        "say": "Skip records on the Obituary list",
        "why": "Obituary records have their own lane and their own timing; the cold lanes do not call them."},
    "probate_list": {
        "skip_lists": ["Probate"],
        "say": "Skip records on the Probate list",
        "why": "A probate filing moves a record out of the obituary lane (register item 12.05, decision D24)."},
    "not_single_family": {
        "skip_tags": ["Not Single Family"],
        "say": "Skip records tagged Not Single Family",
        "why": "The buy box is single family; only the first-to-market presets were missing this."},
    "mail_tags": {
        "skip_tags": ["Return Mail", "Do Not Mail"],
        "say": "Skip records tagged Return Mail or Do Not Mail",
        "why": "Mail that came back, or a person who asked for no mail, is not mailed again."},
    "do_not_mail_flag": {
        "require": {"do_not_mail_ever": False},
        "say": "Skip records with the do-not-mail switch on",
        "why": "The switch is the account's own never-mail mark; no mail preset read it."},
    "returned_mail": {
        "skip": {"any_mail_campaign_situation": ["returned", "undeliverable"]},
        "say": "Skip records whose mail piece came back",
        "why": "The same two values the Return Mail research preset already reads."},
    "one_per_owner": {
        "require": {"ownerPropertiesOwned": {"show_properties": "one_per_owner"}},
        "say": "Show one property per owner",
        "why": "One call per owner a day: without it an owner of three houses is dialed three times."},
    "live_number": {
        "require": {"any_phone_status": LIVE_NUMBER},
        "say": "Keep only records that still have a number not marked wrong or dead",
        "why": "A record whose every number is wrong or dead is exhausted; it leaves the call step for research."},
    "mail_exhausted": {
        "stop_skipping_statuses": ["Exhausted"],
        "say": "Stop skipping the Exhausted status",
        "why": "Exhausted records are exactly who gets mail."},
}

NEVER_TOUCH = ["do_not_market", "litigator", "dnc_list"]
STATUSES = ["lead_statuses", "finished_statuses"]
HEIR_LANE = ["dead_owner", "heir_tag"]

# Role -> what it gets. A role with `leave` is not touched and the plan says why; `measure` on a
# left-alone role only counts how many of its records those packs would catch, for the owner to see.
ROLES = {
    "call_step": {
        "label": "call step",
        "packs": NEVER_TOUCH + STATUSES + HEIR_LANE + ["obituary_list", "not_single_family", "one_per_owner",
                                                      "live_number"]},
    "pre_call": {
        "label": "before the first call",
        "packs": NEVER_TOUCH + STATUSES + HEIR_LANE + ["obituary_list", "not_single_family"]},
    "mail_step": {
        "label": "mail step",
        "packs": NEVER_TOUCH + STATUSES + HEIR_LANE + ["not_single_family", "mail_tags", "do_not_mail_flag",
                                                      "returned_mail", "mail_exhausted"]},
    "hot_auction": {
        "label": "hot auction",
        "packs": NEVER_TOUCH + STATUSES + HEIR_LANE + ["obituary_list", "not_single_family"]},
    "dp_queue": {
        "label": "research queue",
        "packs": NEVER_TOUCH,
        "note": "Only the three never-touch rules. A research queue reads lead and working statuses, dead owners "
                "and the Obituary list on purpose."},
    "exhausted": {
        "label": "exhausted after the call round",
        "packs": NEVER_TOUCH + STATUSES,
        "note": "Statuses are added here because five attempts alone would also catch leads and finished records."},
    "no_live_number": {
        "label": "exhausted because no number is left",
        "packs": NEVER_TOUCH + STATUSES},
    "caller_queue": {
        "label": "caller queue",
        "packs": NEVER_TOUCH + HEIR_LANE,
        "note": "Statuses are left as they are: a caller's own leads belong in the caller's queue."},
    "reactivation": {
        "label": "second chance",
        "leave": "built to read Not Interested, so the status rules do not belong here",
        "measure": ["do_not_market", "dnc_list"]},
    "qc_board": {
        "label": "lead, task and quality view",
        "leave": "built to read lead statuses and open tasks"},
    "dispo": {
        "label": "buyer program",
        "leave": "a different program (these are buyers, not sellers)"},
    "other": {
        "label": "housekeeping view",
        "leave": "not an outreach preset"},
}

# A lane changes its role's packs. `drop` takes a pack off the lane; with `why` the plan says so on
# every preset and counts what the pack would have removed there.
#   obituary: the lane cannot skip the list it is built on, and a probate filing takes a record out.
#   ftm: first-to-market outranks the obituary lane (lead > heirs > hot auction > first to market >
#        obituary), and a probate record sits on the Obituary list by nature while the court has
#        named a living representative as its owner. Measured 2026-11-12: 62 of the 64 Ready to Call
#        records the Obituary rule would remove were probate records, and the probate rule would
#        then keep them out of the obituary lane too. "Unless it is probate" cannot be said in a
#        preset the app shows, so the rule is not applied in this lane. Delete this line to apply it.
LANE_PACKS = {
    "obituary": {"add": ["probate_list"], "drop": ["obituary_list"]},
    "ftm": {"drop": ["obituary_list"],
            "why": "first-to-market outranks the obituary lane, and its probate records are on the Obituary list "
                   "by nature with a living representative as the owner"},
}

# Folder title -> what kind of folder it is. First match wins; case does not matter.
FOLDER_KINDS = [
    (r"dispo", "dispo"),
    (r"-\s*call\s*$", "call"),
    (r"-\s*mail\s*$", "mail"),
    (r"deep prospecting", "deep"),
    (r"reactivation", "reactivation"),
    (r"hot auction", "hot_auction"),
    (r"caller queues?", "caller_queue"),
    (r"(^|[^a-z])qc([^a-z]|$)|lead management|acquisitions|transactions|my tasks", "qc_board"),
]
# "priority" is the two-track restructure's Priority folder (first-to-market plus the Top 1,000, 2026-11-13):
# it follows the first-to-market lane's rules, which leave the Obituary-list rule off on purpose.
LANE_WORDS = [("hottest", "hottest"), ("strong", "strong"), ("ftm", "ftm"), ("priority", "ftm"), ("bulk", "bulk"),
              ("obituary", "obituary")]

# Numbers worth knowing beside the plan. Tags and lists are named by title.
WATCH = [
    ("Records tagged Heirs Resolved, an older tag that means nearly the same as Heir Identified",
     {"any_tags": ["Heirs Resolved"]}),
    ("Records that carry the Obituary tag but are not on the Obituary list (the list rule would miss them)",
     {"any_tags": ["Obituary"], "must_not": {"any_lists": ["Obituary"]}}),
    ("First-to-market records on both the Obituary list and the Probate list (the court named a living "
     "representative; they are why the Obituary rule is not applied in the first-to-market lane)",
     {"any_tags": ["FTM"], "all_lists": ["Obituary", "Probate"]}),
    ("First-to-market records in the status Auction Date Passed",
     {"any_tags": ["FTM"], "any_property_status": ["Auction Date Passed"]}),
    ("Records with a phone that were dialed %d times or more" % ROUND_ENDS_AT,
     {"phone": 1, "predictivecall_attempts": [ROUND_ENDS_AT, None]}),
]

# What the filter language cannot say, learned by asking the search (2026-11-12). Printed in every plan.
STANDING_NOTES = [
    "'Five attempts OR every number wrong or dead' cannot be one preset in a way the app shows. The search itself "
    "understands an either-or (asked for two groups of 318 and 63 records with 1 in both, it answered 380), but the "
    "filter panel has no control for it, so a preset built that way could not be read or edited on screen. The plan "
    "uses two presets instead.",
    "'Skip a deceased owner' can only be said as 'deceased: no', and a record whose mark is blank (neither yes nor "
    "no) fails that test too. So the rule waits while blank marks exist, and each preset says what it would remove.",
    "'Skip the Obituary list unless the record is a probate record' cannot be said in a preset the app shows either, "
    "which is why the first-to-market lane keeps its Obituary-list records.",
    "The owner-level do-not-mail switch is read by the search but the filter panel does not show it, so only the "
    "property-level switch is used.",
]

# ==================================================================================================
# End of the table.
# ==================================================================================================

PROOF_BASE = {"phone": 1}
STATUS_WORDS = {"dnc": "DNC", "opt_out": "Opt Out", "new_lead": "New Lead", "follow_up": "Follow Up"}


class PlanError(RuntimeError):
    pass


class Refused(SystemExit):
    """A write that was not allowed, or a read-back that did not match. Never caught on the way out."""


# -------------------------------------------------------------------------------------------- roles
def folder_kind(folder: str) -> str:
    low = (folder or "").strip().lower()
    for pattern, kind in FOLDER_KINDS:
        if re.search(pattern, low):
            return kind
    return "other"


def lane_of(folder: str, title: str) -> str:
    low_f, low_t = (folder or "").lower(), (title or "").lower()
    for word, lane in LANE_WORDS:
        if word in low_t:
            return lane
    for word, lane in LANE_WORDS:
        if word in low_f:
            return lane
    return ""


def attempt_of(title: str) -> int | None:
    """The call counter a call step stands for: Ready to Call is 0, Call Attempt N is N."""
    low = (title or "").lower()
    m = re.search(r"call attempt\s*(\d+)", low)
    if m:
        return int(m.group(1))
    if "ready to call" in low:
        return 0
    return None


def classify(folder: str, title: str) -> dict:
    """{"role", "lane", "attempt"} for one preset, from its folder and its title alone."""
    kind, low = folder_kind(folder), (title or "").lower()
    lane, attempt = lane_of(folder, title), attempt_of(title)
    if kind == "call":
        if "needs skipped" in low or "no numbers" in low:
            role = "pre_call"
        elif attempt is not None:
            role = "call_step"
        else:
            role = "other"
    elif kind == "mail":
        role = "mail_step"
    elif kind == "deep":
        if attempt is not None:
            role = "call_step"
        elif "ready to mail" in low:
            role = "mail_step"
        elif "no live number" in low:
            role = "no_live_number"
        elif "exhausted" in low:
            role = "exhausted"
        else:
            role = "dp_queue"
    elif kind in ROLES:
        role = kind
    else:
        role = "other"
    if role != "call_step":
        attempt = None
    return {"role": role, "lane": lane, "attempt": attempt}


def packs_for(role: str, lane: str) -> list[str]:
    packs = list(ROLES[role].get("packs") or [])
    extra = LANE_PACKS.get(lane) or {}
    packs = [p for p in packs if p not in (extra.get("drop") or [])]
    if packs:
        packs += [p for p in (extra.get("add") or []) if p not in packs]
    return packs


def lane_skips(role: str, lane: str) -> dict:
    """Packs the role would get but the lane leaves out on purpose, with the reason to print."""
    extra = LANE_PACKS.get(lane) or {}
    if not extra.get("why"):
        return {}
    return {p: extra["why"] for p in ROLES[role].get("packs") or [] if p in (extra.get("drop") or [])}


# -------------------------------------------------------------------------------------------- index
class Index:
    """Titles to ids and back for tags and lists, and the statuses the account really has."""

    def __init__(self, tags: list, lists: list, statuses: list):
        self.tags = self._table(tags)
        self.lists = self._table(lists)
        self.statuses = [str(s.get("title") or "") for s in statuses or [] if s.get("title")]
        self._tag_title = {v: k for k, v in self.tags.items()}
        self._list_title = {v: k for k, v in self.lists.items()}

    @staticmethod
    def _table(rows) -> dict:
        out = {}
        for r in rows or []:
            title = str(r.get("title") or "").strip()
            if title and r.get("uuid"):
                out.setdefault(title, r["uuid"])
        return out

    @staticmethod
    def _find(table: dict, title: str):
        if title in table:
            return table[title]
        hits = [v for k, v in table.items() if k.lower() == title.strip().lower()]
        return hits[0] if len(hits) == 1 else None      # two spellings of one name: do not guess

    def tag(self, title: str):
        return self._find(self.tags, title)

    def lst(self, title: str):
        return self._find(self.lists, title)

    def status(self, title: str):
        for s in self.statuses:
            if s.lower() == str(title).strip().lower():
                return s
        return None

    def near_status(self, title: str):
        """A known status that differs from `title` only in spacing or underscores: a hint for the
        owner, never a match (the search treats 'New Lead' and new_lead as two different things)."""
        want = re.sub(r"[^a-z0-9]", "", str(title).lower())
        for s in self.statuses:
            if re.sub(r"[^a-z0-9]", "", s.lower()) == want:
                return s
        return None

    def tag_title(self, uuid: str) -> str:
        return self._tag_title.get(uuid, "a deleted tag")

    def list_title(self, uuid: str) -> str:
        return self._list_title.get(uuid, "a deleted list")


def status_word(s: str) -> str:
    if s in STATUS_WORDS:
        return STATUS_WORDS[s]
    return s.replace("_", " ").title() if s == s.lower() else s


def sha(must: dict) -> str:
    return hashlib.sha1(json.dumps(must, sort_keys=True).encode()).hexdigest()[:16]


# ------------------------------------------------------------------------------------- the patching
def _low(values) -> set:
    return {str(v).strip().lower() for v in values or []}


def _required(must: dict, key: str) -> set:
    """What the preset itself asks for under `key` (any_ or all_), so it is never excluded too."""
    return set(must.get("any_" + key) or []) | set(must.get("all_" + key) or [])


def apply_pack(must: dict, code: str, ix: Index) -> dict:
    """Add one pack to `must` in place. Returns {"added", "waiting", "conflicts"}: plain items the
    plan prints. Nothing already there is moved, rewritten or repeated."""
    pack = PACKS[code]
    out = {"added": [], "waiting": [], "conflicts": []}

    def exclusions(key: str) -> list:
        mn = must.setdefault("must_not", {})
        if not isinstance(mn.get(key), list):
            mn[key] = list(mn.get(key) or [])
        return mn[key]

    for kind, key, find in (("tag", "tags", ix.tag), ("list", "lists", ix.lst)):
        for title in pack.get("skip_" + key) or []:
            uuid = find(title)
            if not uuid:
                out["waiting"].append({"kind": kind, "title": title})
            elif uuid in _required(must, key):
                out["conflicts"].append("the preset is built on the %s %s, so it cannot also skip it" % (kind, title))
            elif uuid not in ((must.get("must_not") or {}).get("any_" + key) or []):
                exclusions("any_" + key).append(uuid)
                out["added"].append({"kind": "skip_" + kind, "what": title})

    for title in pack.get("skip_statuses") or []:
        stored = ix.status(title)
        if not stored:
            out["waiting"].append({"kind": "status", "title": title})
        elif stored.lower() in _low(must.get("any_property_status")):
            out["conflicts"].append("the preset reads the status %s on purpose" % status_word(stored))
        elif stored.lower() not in _low((must.get("must_not") or {}).get("any_property_status")):
            exclusions("any_property_status").append(stored)
            out["added"].append({"kind": "skip_status", "what": stored})

    for key, want in (pack.get("require") or {}).items():
        if isinstance(want, dict):                      # one level of nesting: ownerPropertiesOwned
            have = must.get(key)
            if have is None:
                must[key] = copy.deepcopy(want)
                out["added"].append({"kind": "require", "what": key})
                continue
            for k2, v2 in want.items():
                if k2 not in have:
                    have[k2] = copy.deepcopy(v2)
                    out["added"].append({"kind": "require", "what": "%s.%s" % (key, k2)})
                elif have[k2] != v2:
                    out["conflicts"].append("%s is already set to %r here; left as it is" % (k2, have[k2]))
        elif key not in must:
            must[key] = copy.deepcopy(want)
            out["added"].append({"kind": "require", "what": key})
        elif must[key] != want:
            out["conflicts"].append("%s is already set to %r here; left as it is" % (key, must[key]))

    for key, values in (pack.get("skip") or {}).items():
        if set(values) & set(must.get(key) or []):
            out["conflicts"].append("the preset reads %s on purpose" % key.replace("_", " "))
            continue
        for v in values:
            if v not in ((must.get("must_not") or {}).get(key) or []):
                exclusions(key).append(v)
                out["added"].append({"kind": "skip_value", "what": "%s %s" % (key, v)})

    for title in pack.get("stop_skipping_statuses") or []:
        have = (must.get("must_not") or {}).get("any_property_status") or []
        for s in [x for x in have if str(x).lower() == title.lower()]:
            have.remove(s)
            out["added"].append({"kind": "stop_skipping", "what": s})
    return out


def patch(must: dict, packs: list[str], ix: Index, held: dict | None = None) -> dict:
    """A patched copy of `must` and what was done to it, pack by pack. `held` names packs that are
    not applied (their filter could not be proven) with the reason."""
    new = copy.deepcopy(must or {})
    out = {"must": new, "by_pack": {}, "waiting": [], "conflicts": [], "held": []}
    for code in packs:
        if held and code in held:
            out["held"].append(code)
            continue
        r = apply_pack(new, code, ix)
        if r["added"]:
            out["by_pack"][code] = r["added"]
        out["waiting"] += [dict(w, pack=code) for w in r["waiting"]]
        out["conflicts"] += r["conflicts"]
    return out


def exhausted_rewrite(must: dict) -> list[dict]:
    """The two deliberate edits to the exhausted preset (plan 0.6), in place. It moves to the end of
    the round, and its 'every number wrong or dead' test leaves for the preset of its own."""
    done = []
    rng = must.get("predictivecall_attempts")
    if isinstance(rng, list) and len(rng) == 2 and isinstance(rng[0], int) and rng[0] < ROUND_ENDS_AT:
        done.append({"kind": "set", "what": "predictivecall_attempts", "from": list(rng),
                     "to": [ROUND_ENDS_AT, rng[1]]})
        must["predictivecall_attempts"] = [ROUND_ENDS_AT, rng[1]]
    mn = must.get("must_not") or {}
    if "any_phone_status" in mn:
        done.append({"kind": "drop", "what": "must_not.any_phone_status", "from": mn.pop("any_phone_status")})
    return done


def next_title(src: str, src_attempt: int, attempt: int) -> str:
    """'Hottest - 05 Call Attempt 3' -> 'Hottest - 06 Call Attempt 4': the step number and the
    attempt both move by the same amount."""
    m = re.match(r"^(.*?)(\d{2})(\D.*?call attempt\s*)(\d+)(.*)$", src, re.I)
    if not m:
        return re.sub(r"(call attempt\s*)\d+", lambda x: x.group(1) + str(attempt), src, flags=re.I)
    step = int(m.group(2)) + (attempt - src_attempt)
    return "%s%02d%s%d%s" % (m.group(1), step, m.group(3), attempt, m.group(5))


def observe(e: dict, must: dict, ix: Index) -> list[str]:
    """Things in a preset the owner should look at, whether or not the tool changes it."""
    flags = []
    mn = must.get("must_not") or {}
    mail_only = ix.tag("Mail Only")
    if e["role"] == "mail_step" and mail_only and mail_only in (mn.get("any_tags") or []):
        flags.append("This mail preset skips the Mail Only tag, which marks people who may be mailed but not called. "
                     "The tool removes nothing, so it is still there.")
    if e["attempt"] is not None and e["attempt"] >= ROUND_ENDS_AT:
        flags.append("This step is past the end of the round (%d attempts): once the exhausted preset starts at %d "
                     "the two overlap. Retire it by hand in the app; this tool does not delete."
                     % (ROUND_ENDS_AT, ROUND_ENDS_AT))
    rng = must.get("predictivecall_attempts")
    if (e["role"] not in ("exhausted", "call_step") and isinstance(rng, list) and len(rng) == 2
            and isinstance(rng[0], int) and rng[1] is None and 0 < rng[0] < ROUND_ENDS_AT):
        flags.append("It reads records from %d call attempts up, with no upper end. The round now ends at %d, so it "
                     "overlaps Call Attempt %d to %d. Decide whether it should start at %d."
                     % (rng[0], ROUND_ENDS_AT, rng[0], ROUND_ENDS_AT - 1, ROUND_ENDS_AT))
    for s in list(must.get("any_property_status") or []) + list(mn.get("any_property_status") or []):
        if not ix.status(s):
            near = ix.near_status(s)
            flags.append("It names the status %r, which is not a status this account is known to have%s. Statuses are "
                         "matched by their stored spelling, so that entry most likely matches nothing."
                         % (s, " (the stored spelling is %r)" % near if near else ""))
    return flags


# -------------------------------------------------------------------------------------- the reader
class Reader:
    """The only door `plan` has to the account: listings and counted searches. It cannot write,
    because it has no method that does. Counts are cached by filter when a cache file is given, and
    a request budget stops a run before it leans on the limit the production jobs share."""

    def __init__(self, crm, *, cache_path: str | None = None, max_requests: int = 450, now: date | None = None):
        self._crm = crm
        self.max_requests = max_requests
        self.today = now or date.today()
        self.cache_path = Path(cache_path) if cache_path else None
        self.cache = {"listings": {}, "counts": {}}
        if self.cache_path and self.cache_path.exists():
            try:
                self.cache.update(json.loads(self.cache_path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                pass
        self._kept = set(self.cache["listings"]) | set(self.cache["counts"])    # answers the file already held
        self._start = int(getattr(crm, "calls", 0) or 0)
        self.from_cache = 0
        self.oldest = ""
        self.stopped = ""
        self.skipped = 0
        self.errors: list[str] = []
        self.notes: list[str] = []

    @property
    def made(self) -> int:
        return int(getattr(self._crm, "calls", 0) or 0) - self._start

    def _save(self) -> None:
        if self.cache_path:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(self.cache, indent=1), encoding="utf-8")

    def _used(self, key: str, at: str) -> None:
        if key in self._kept:
            self.from_cache += 1
        if at and (not self.oldest or at < self.oldest):
            self.oldest = at

    def rows(self, path: str, optional: bool = False) -> list:
        """A listing. Listings are structure: they are read even when the budget is spent. An
        optional listing that cannot be read is an empty one with a note, never a failed plan."""
        hit = self.cache["listings"].get(path)
        if hit is None:
            try:
                rows = rows_of(self._crm.call(path))
            except Exception as exc:  # noqa: BLE001
                if not optional:
                    raise
                self.notes.append("The listing %s could not be read (%s); the plan went on without it."
                                  % (path.split("?")[0], type(exc).__name__))
                return []
            hit = {"rows": rows, "at": datetime.now().isoformat(timespec="seconds")}
            self.cache["listings"][path] = hit
            self._save()
        self._used(path, hit.get("at") or "")
        return hit["rows"]

    def count(self, must: dict):
        """Records matching a SAVED preset filter today, or None when it could not be counted (the
        reason is kept). Relative date windows are resolved the way the app does it."""
        shaped, notes = windows.resolve(search_shape(must), self.today)
        self.notes += ["A date filter could not be read and was left out of a count (%s)." % n
                       for n in notes if "dropped" in n]
        key = sha(shaped)
        hit = self.cache["counts"].get(key)
        if hit is not None:
            self._used(key, hit.get("at") or "")
            return hit["n"]
        if self.stopped or self.made >= self.max_requests:
            self.skipped += 1
            return None
        try:
            n = int(self._crm.count(shaped))
        except Exception as exc:  # noqa: BLE001  (one refused filter must not lose the whole plan)
            if type(exc).__name__ == "BudgetExhausted":
                # The client's own shared budget for the hour is used. Asking again would only be
                # refused again, so the rest of this run is left uncounted and said so.
                self.stopped = str(exc)[:240]
                self.skipped += 1
                return None
            self.errors.append("%s: %s" % (type(exc).__name__, str(exc)[:240]))
            return None
        self.cache["counts"][key] = {"n": n, "at": datetime.now().isoformat(timespec="seconds")}
        self._used(key, self.cache["counts"][key]["at"])
        self._save()
        return n


def read_account(reader: Reader) -> tuple[list[dict], Index, int]:
    """Every saved records preset with its filter, the title index, and how many folders there are.
    Folder listings default to ten rows, so every listing asks for all of them."""
    presets = []
    folders = reader.rows("/api/internal/filter-preset-folder/?type=properties&limit=999")
    for f in folders:
        for p in reader.rows("/api/internal/filter-preset-folder/%s/filter-preset/?limit=999" % f["uuid"]):
            presets.append({"uuid": p.get("uuid"), "title": p.get("title") or "", "folder": f.get("title") or "",
                            "folder_uuid": f.get("uuid"), "quick_filter": bool(p.get("quick_filter")),
                            "filters": p.get("filters") or {}})
    statuses, notes = known_statuses(reader.rows("/api/internal/global-status/", optional=True))
    reader.notes += notes
    ix = Index(reader.rows("/api/internal/tag/?offset=0&limit=10000&ordering=title"),
               reader.rows("/api/internal/list/?offset=0&limit=1000"), statuses)
    return presets, ix, len(folders)


def known_statuses(published: list) -> tuple[list[dict], list[str]]:
    """Every status the tool knows the account to have, as index rows, and what to tell the owner.

    The published listing groups the lead-pipeline statuses under seven fixed headings and names
    nothing else (no sold, no prospecting, no custom status), so it cannot be the whole list. It
    is the live check: a spelling it gives is the stored one and wins over the table's. Everything
    else comes from the three families in the table."""
    def flat(s: str) -> str:
        return re.sub(r"[^a-z0-9]", "", s.lower())

    live = [s.strip() for row in published or [] for s in (row.get("statuses") or [])
            if isinstance(s, str) and s.strip()]
    table = LEAD_STATUSES + FINISHED_STATUSES + WORKING_STATUSES
    out, seen, notes = [], set(), []
    for s in live:
        if s.lower() not in seen:
            seen.add(s.lower())
            out.append(s)
        twin = next((t for t in table if flat(t) == flat(s) and t.lower() != s.lower()), None)
        if twin:
            notes.append("The account stores the status %r and the tool's table spells it %r. The table's spelling "
                         "is not used; correct the table." % (s, twin))
        elif s.lower() not in {t.lower() for t in table}:
            notes.append("The account has a lead-pipeline status %r that the tool's table does not list. Add it to "
                         "the family it belongs in, or it is never skipped." % s)
    stored = {flat(s) for s in live}
    for t in table:
        if t.lower() not in seen and flat(t) not in stored:
            seen.add(t.lower())
            out.append(t)
    return [{"title": s} for s in out], notes


# ------------------------------------------------------------------------------------------ proofs
def _named(must: dict, ix: Index) -> dict | None:
    """A WATCH filter with its tags and lists turned from titles into ids; None when one is missing."""
    out = copy.deepcopy(must)
    for block in (out, out.get("must_not") or {}):
        for key, find in (("any_tags", ix.tag), ("all_tags", ix.tag), ("any_lists", ix.lst), ("all_lists", ix.lst)):
            if key in block:
                ids = [find(t) for t in block[key]]
                if not all(ids):
                    return None
                block[key] = ids
    return out


def prove(reader: Reader, ix: Index) -> tuple[list[dict], dict, dict]:
    """Ask the search with and without each kind of rule. Returns (proofs, held, waiting): a pack is
    held back when the filter it rests on did not move the count, and it waits (held, but measured
    on every preset) when the filter works and would also remove records whose mark is blank."""
    base = reader.count(PROOF_BASE)
    proofs, held, seen, waiting = [], {}, {}, {}

    def ask(form: str, what: str, must: dict, opposite: dict | None = None):
        """True when the count moved, False when it did not, None when it could not be asked."""
        if form in seen:
            return seen[form]
        n = reader.count(must)
        if base is None or n is None:
            proofs.append({"form": form, "what": what, "without": base, "with": n, "moved": False,
                           "note": "Not checked: the count was not available in this run."})
            seen[form] = None
            return None
        moved = n != base
        row = {"form": form, "what": what, "without": base, "with": n, "moved": moved}
        if not moved and opposite is not None:
            m = reader.count(opposite)
            row["with_opposite"] = m
            if m is not None and m != base:
                moved = row["moved"] = True
                row["note"] = ("Nobody carries it today, so skipping it changes nothing; asking for the records that "
                               "DO carry it changes the count, which shows the search reads it.")
        proofs.append(row)
        seen[form] = moved
        return moved

    for code, pack in PACKS.items():
        answers = []
        tags = [ix.tag(t) for t in pack.get("skip_tags") or [] if ix.tag(t)]
        if tags:
            answers.append(ask("skip a tag", "Skip a tag (tried with %s)" % ix.tag_title(tags[0]),
                               dict(PROOF_BASE, must_not={"any_tags": tags[:1]})))
        lists = [ix.lst(t) for t in pack.get("skip_lists") or [] if ix.lst(t)]
        if lists:
            answers.append(ask("skip a list", "Skip a list (tried with %s)" % ix.list_title(lists[0]),
                               dict(PROOF_BASE, must_not={"any_lists": lists[:1]})))
        if any(ix.status(s) for s in (pack.get("skip_statuses") or []) + (pack.get("stop_skipping_statuses") or [])):
            answers.append(ask("skip a status", "Skip statuses (tried with the lead statuses)",
                               dict(PROOF_BASE, must_not={"any_property_status": [ix.status(s) for s in LEAD_STATUSES
                                                                                  if ix.status(s)]})))
        for key, want in (pack.get("require") or {}).items():
            opposite = None
            if isinstance(want, bool):
                opposite = dict(PROOF_BASE, **{key: not want})
            elif isinstance(want, int):
                opposite = dict(PROOF_BASE, **{key: 1 - want})
            answers.append(ask("require " + key, pack["say"], dict(PROOF_BASE, **{key: copy.deepcopy(want)}), opposite))
        for key, values in (pack.get("skip") or {}).items():
            answers.append(ask("skip " + key, pack["say"], dict(PROOF_BASE, must_not={key: list(values)})))
        if None in answers:
            held[code] = "it could not be checked in this run (the count was not available), so it is not proposed"
        elif False in answers:
            held[code] = ("the search did not change its count for this filter. Either no record matches it today or "
                          "the search does not read it; it is not added until a count moves")
        elif pack.get("hold_if_blank"):
            key = next(iter(pack["require"]))
            yes, no = reader.count(dict(PROOF_BASE, **{key: 1})), reader.count(dict(PROOF_BASE, **{key: 0}))
            if yes is None or no is None:
                held[code] = "it could not be checked in this run (the count was not available), so it is not proposed"
            elif base - yes - no > 0:
                held[code] = waiting[code] = (
                    "the app can only say it as '%s: no', and a record whose mark is blank fails that test too. Of "
                    "the %s records with a phone, %s are marked yes and %s have a blank mark. Set the blank marks to "
                    "no on those owners, run the plan again, and the rule is added"
                    % (key, _n(base), _n(yes), _n(base - yes - no)))
    return proofs, held, waiting


# -------------------------------------------------------------------------------------------- plan
def _say(code: str, added: list[dict]) -> str:
    """One plain sentence for what a pack adds to one preset."""
    say = PACKS[code]["say"]
    statuses = [status_word(a["what"]) for a in added if a["kind"] == "skip_status"]
    if statuses:
        return "%s (adds %s)" % (say, ", ".join(statuses))
    names = [a["what"] for a in added if a["kind"] in ("skip_tag", "skip_list")]
    total = len(PACKS[code].get("skip_tags") or []) + len(PACKS[code].get("skip_lists") or [])
    if names and len(names) < total:
        return "%s (adds %s)" % (say, ", ".join(names))
    return say


def _entry(p: dict) -> dict:
    c = classify(p["folder"], p["title"])
    return {"folder": p["folder"], "title": p["title"], "uuid": p["uuid"], "role": c["role"], "lane": c["lane"],
            "attempt": c["attempt"], "role_label": ROLES[c["role"]]["label"], "action": "", "changes": [],
            "waiting": [], "conflicts": [], "held": [], "not_applied": [], "flags": [], "count_now": None,
            "count_after": None, "by_reason": {}}


def _new_preset(src: dict, title: str, role: str, lane: str, must: dict, why: str, first_line: str,
                ix: Index, held: dict) -> dict:
    res = patch(must, packs_for(role, lane), ix, held)
    return {"title": title, "folder": src["folder"], "folder_uuid": src["folder_uuid"], "clone_of": src["title"],
            "source_sha": sha((src["filters"] or {}).get("must") or {}), "quick_filter": src["quick_filter"],
            "role_label": ROLES[role]["label"], "why": why,
            "says": [first_line] + [_say(code, added) for code, added in res["by_pack"].items()],
            "waiting": res["waiting"], "held": res["held"], "flags": [], "must_after": res["must"],
            "after_sha": sha(res["must"]), "account": (src["filters"] or {}).get("account"), "count": None}


def build_plan(reader: Reader, *, reasons: bool = True, day: str = "") -> dict:
    presets, ix, n_folders = read_account(reader)
    proofs, held, waiting = prove(reader, ix)
    titles = {p["title"] for p in presets}
    entries, creates, work = [], [], []

    for p in presets:
        e = _entry(p)
        must = (p["filters"] or {}).get("must") or {}
        role = ROLES[e["role"]]
        e["before_sha"] = sha(must)
        e["flags"] = observe(e, must, ix)
        entries.append(e)
        if role.get("leave"):
            e["action"], e["left_alone_because"] = "left_alone", role["leave"]
            if role.get("measure"):
                work.append(("measure", e, must, patch(must, role["measure"], ix, held)["must"], must))
            continue
        res = patch(must, packs_for(e["role"], e["lane"]), ix, held)
        new, reason_base = res["must"], must
        if e["role"] == "exhausted":
            rewritten = copy.deepcopy(must)
            if exhausted_rewrite(rewritten):
                reason_base = rewritten                  # the packs are then measured on top of the rewrite
            for ch in exhausted_rewrite(new):
                if ch["kind"] == "set":
                    e["changes"].append({
                        "pack": "round", "detail": [ch],
                        "say": "Start at %d call attempts instead of %d" % (ch["to"][0], ch["from"][0]),
                        "why": "Five full attempts end the round. Today it starts at the last call attempt and "
                               "overlaps it."})
                else:
                    e["changes"].append({
                        "pack": "split", "detail": [ch],
                        "say": "Drop the 'every number wrong or dead' test from this preset",
                        "why": "That half moves to its own preset (%s)." % NO_LIVE_NUMBER_TITLE})
        for code, added in res["by_pack"].items():
            e["changes"].append({"pack": code, "say": _say(code, added), "why": PACKS[code]["why"], "detail": added})
        e["waiting"], e["conflicts"] = res["waiting"], res["conflicts"]
        e["held"] = [c for c in res["held"] if c not in waiting]
        # Rules that belong to this preset and are NOT in its new filter, each measured for the owner:
        # a rule waiting on blank marks, and a rule its lane leaves out on purpose.
        e["not_applied"] = ([{"pack": c, "kind": "waiting", "say": PACKS[c]["say"]} for c in res["held"] if c in waiting]
                            + [{"pack": c, "kind": "lane", "say": PACKS[c]["say"], "because": why}
                               for c, why in lane_skips(e["role"], e["lane"]).items()])
        if role.get("note"):
            e["note"] = role["note"]
        if e["changes"]:
            e["action"], e["must_after"], e["after_sha"] = "change", new, sha(new)
        else:
            e["action"] = "already_complete"
        work.append(("preset", e, must, new, reason_base))

    # ---- new presets: the missing call attempts, and the exhausted preset's second half
    lanes: dict = {}
    for p, e in zip(presets, entries):
        if e["role"] == "call_step":
            lanes.setdefault((p["folder"], e["lane"]), []).append((e["attempt"], p))
    for (folder, lane), steps in lanes.items():
        top_n, top = max(steps, key=lambda s: s[0])
        if top_n < 1:
            continue                                    # a lane with no call attempts at all is not grown here
        for n in range(top_n + 1, ROUND_ENDS_AT):
            must = copy.deepcopy((top["filters"] or {}).get("must") or {})
            must["predictivecall_attempts"] = [n, n]
            c = _new_preset(top, next_title(top["title"], top_n, n), "call_step", lane, must,
                            "The call round is %d attempts; this lane stops at attempt %d today."
                            % (ROUND_ENDS_AT, top_n),
                            "Same filter as %s, with the call counter at %d" % (top["title"], n), ix, held)
            step = re.match(r"^\s*(\d{2})\b", c["title"])
            if c["title"] in titles:
                c["flags"].append("A preset with this title already exists; pick another title.")
            elif step and any(q["folder"] == folder and re.match(r"^\s*%s\b" % step.group(1), q["title"])
                              for q in presets):
                c["flags"].append("The number %s is already used by another preset in this folder; rename as you like."
                                  % step.group(1))
            creates.append(c)
    for p, e in zip(presets, entries):
        src = (p["filters"] or {}).get("must") or {}
        if e["role"] != "exhausted" or NO_LIVE_NUMBER_TITLE in titles:
            continue
        if "any_phone_status" not in (src.get("must_not") or {}):
            continue
        must = copy.deepcopy(src)
        must["predictivecall_attempts"] = [0, ROUND_ENDS_AT - 1]
        creates.append(_new_preset(
            p, NO_LIVE_NUMBER_TITLE, "no_live_number", e["lane"], must,
            "A record is exhausted at %d attempts OR when every number is wrong or dead. One preset cannot say 'or' "
            "in a way the app shows, so the second half gets its own preset." % ROUND_ENDS_AT,
            "Same filter as %s today, for records with 0 to %d call attempts whose every number is wrong or dead"
            % (p["title"], ROUND_ENDS_AT - 1), ix, held))

    # ---- counts, cheapest and most useful first
    for kind, e, must, new, _ in work:
        e["count_now"] = reader.count(must)
        if kind == "measure":
            caught = reader.count(new)
            if e["count_now"] is not None and caught is not None:
                e["would_catch"] = e["count_now"] - caught
        elif e["action"] == "change":
            e["count_after"] = reader.count(new)
        else:
            e["count_after"] = e["count_now"]
    for c in creates:
        c["count"] = reader.count(c["must_after"])
    watch = []
    for label, must in WATCH:
        named = _named(must, ix)
        watch.append({"what": label, "count": reader.count(named) if named else None,
                      "note": "" if named else "a tag or list it names does not exist"})
    for kind, e, must, new, _ in work:
        if kind == "preset":
            _would_remove(reader, e, must, ix)
    if reasons:
        for kind, e, must, new, reason_base in work:
            if kind == "preset" and e["action"] == "change" and None not in (e["count_now"], e["count_after"]):
                _reasons(reader, e, must, reason_base, ix, held)

    return _assemble(reader, presets, n_folders, entries, creates, proofs, held, waiting, watch, day)


def _would_remove(reader: Reader, e: dict, must: dict, ix: Index) -> None:
    """For each rule that is NOT applied to this preset, how many of its records the rule would have
    removed. For the deceased rule the number is split: marked deceased, and merely blank."""
    for item in e["not_applied"]:
        if not e["count_now"]:
            item["would_remove"] = 0 if e["count_now"] == 0 else None
            continue
        n = reader.count(patch(must, [item["pack"]], ix)["must"])
        if n is None:
            item["would_remove"] = None
            continue
        item["would_remove"] = e["count_now"] - n
        key = next(iter(PACKS[item["pack"]].get("require") or {}), None)
        if item["kind"] == "waiting" and key and item["would_remove"] > 0:
            marked = reader.count(dict(copy.deepcopy(must), **{key: 1}))
            if marked is not None:
                item["marked"], item["blank"] = marked, item["would_remove"] - marked


def _reasons(reader: Reader, e: dict, must: dict, base: dict, ix: Index, held: dict) -> None:
    """How many records each pack moves on its own, for one preset. A record caught by two packs is
    counted under both. For the exhausted preset the packs are measured on top of its rewrite."""
    countable = [c for c in e["changes"] if c["pack"] in PACKS]
    base_n = e["count_now"]
    if base is not must:
        base_n = reader.count(base)
        e["count_rewritten"] = base_n
        if base_n is None:
            return
    # The rule that brings records back is counted first: only when nothing comes back does "the
    # same number before and after" mean that nothing left either.
    countable.sort(key=lambda c: not PACKS[c["pack"]].get("stop_skipping_statuses"))
    may_return = False                                   # a record came back, or we could not tell
    for c in countable:
        code = c["pack"]
        stop = bool(PACKS[code].get("stop_skipping_statuses"))
        if base_n == e["count_after"] and not may_return and not stop:
            e["by_reason"][code] = 0
            continue
        n = e["count_after"] if len(countable) == 1 else reader.count(patch(base, [code], ix, held)["must"])
        if n is None:
            may_return = may_return or stop
            continue
        e["by_reason"][code] = base_n - n                # negative: records come back
        may_return = may_return or n > base_n
        if code == "dead_owner" and e["by_reason"][code] > 0:
            marked = reader.count(dict(copy.deepcopy(base), deceased=1))
            if marked is not None:
                e["dead_owner_marked"] = marked
                e["dead_owner_blank"] = e["by_reason"][code] - marked


def _assemble(reader, presets, n_folders, entries, creates, proofs, held, waiting, watch, day) -> dict:
    changed = [e for e in entries if e["action"] == "change"]
    left = [e for e in entries if e["action"] == "left_alone"]
    skipped: dict = {}
    for e in entries:
        for item in e["not_applied"]:
            row = skipped.setdefault((item["pack"], item["kind"]), {
                "pack": item["pack"], "kind": item["kind"], "say": item["say"], "presets": 0, "would_remove": 0,
                "marked": 0, "blank": 0, "not_counted": 0,
                "because": waiting.get(item["pack"]) if item["kind"] == "waiting" else item.get("because")})
            row["presets"] += 1
            if item.get("would_remove") is None:
                row["not_counted"] += 1
            else:
                row["would_remove"] += item["would_remove"]
                row["marked"] += item.get("marked") or 0
                row["blank"] += item.get("blank") or 0
    prereq: dict = {}
    for item in entries + creates:
        for w in item["waiting"]:
            row = prereq.setdefault((w["kind"], w["title"]), {"kind": w["kind"], "title": w["title"],
                                                              "rule": PACKS[w["pack"]]["say"], "presets": 0})
            row["presets"] += 1
    by_reason: dict = {}
    for e in changed:
        for c in e["changes"]:
            if c["pack"] not in PACKS:
                continue
            row = by_reason.setdefault(c["pack"], {"pack": c["pack"], "say": PACKS[c["pack"]]["say"], "presets": 0,
                                                   "leaving": 0, "returning": 0, "counted": 0})
            row["presets"] += 1
            n = e["by_reason"].get(c["pack"])
            if n is not None:
                row["counted"] += 1
                row["leaving" if n >= 0 else "returning"] += abs(n)
    counted = [e for e in changed if e["count_now"] is not None and e["count_after"] is not None]
    groups: dict = {}
    for e in left:
        g = groups.setdefault(e["role"], {"role": e["role_label"], "because": e["left_alone_because"], "presets": 0})
        g["presets"] += 1
    return {
        "tool": "preset_patch", "plan_date": day or date.today().isoformat(),
        "generated_at": datetime.now().isoformat(timespec="seconds"), "counted_as_of": reader.today.isoformat(),
        "round_ends_at": ROUND_ENDS_AT,
        "requests": {"made": reader.made, "from_cache": reader.from_cache, "oldest_answer": reader.oldest,
                     "budget": reader.max_requests, "not_counted": reader.skipped, "stopped": reader.stopped,
                     "errors": reader.errors[:20],
                     "notes": sorted(set(reader.notes))[:20]},
        "summary": {
            "presets_read": len(presets), "folders": n_folders,
            "changed": len(changed), "already_complete": sum(1 for e in entries if e["action"] == "already_complete"),
            "left_alone": len(left), "to_create": len(creates),
            "records_now": sum(e["count_now"] for e in counted), "records_after": sum(e["count_after"] for e in counted),
            "presets_counted": len(counted),
            "by_reason": sorted(by_reason.values(), key=lambda r: -(r["leaving"] + r["returning"])),
            "left_alone_groups": list(groups.values()),
        },
        "table": {role: packs_for(role, "") for role in ROLES},
        "prerequisites": sorted(prereq.values(), key=lambda r: r["title"]),
        "held_back": [{"pack": c, "say": PACKS[c]["say"], "because": why} for c, why in held.items()
                      if c not in waiting],
        "not_applied": list(skipped.values()),
        "standing_notes": list(STANDING_NOTES),
        "proofs": proofs, "watch": watch, "creates": creates, "presets": entries,
    }


# ------------------------------------------------------------------------------------------ report
def _n(v) -> str:
    return "not counted" if v is None else "{:,}".format(v)


def _presets(n: int, word: str = "preset") -> str:
    return "1 %s" % word if n == 1 else "%d %ss" % (n, word)


def _records(v) -> str:
    return "not counted" if v is None else ("1 record" if v == 1 else "{:,} records".format(v))


def _mid(sentence: str) -> str:
    """A rule sentence for the middle of a line: first letter down, the names in it left alone."""
    return sentence[:1].lower() + sentence[1:]


def _moved(n) -> str:
    """': 3 leave', ': 1 comes back', ': none today', or nothing when the rule was not counted."""
    if n is None:
        return ""
    if n == 0:
        return ": none today"
    if n > 0:
        return ": %s %s" % (_n(n), "leaves" if n == 1 else "leave")
    return ": %s %s back" % (_n(-n), "comes" if n == -1 else "come")


def _waiting_lines(item: dict) -> list[str]:
    seen, out = set(), []
    for wt in item.get("waiting") or []:
        if (wt["kind"], wt["title"]) not in seen:
            seen.add((wt["kind"], wt["title"]))
            out.append("- Waiting: the %s %s does not exist yet." % (wt["kind"], wt["title"]))
    return out


def render_md(plan: dict) -> str:
    s, out = plan["summary"], []
    w = out.append
    w("# Preset exclusions: the change list for %s" % plan["plan_date"])
    w("")
    reused = ""
    if plan["requests"]["from_cache"]:
        reused = "; %d more answers came from the cache file, each read with one request earlier, the oldest on %s" % (
            plan["requests"]["from_cache"], (plan["requests"].get("oldest_answer") or "").replace("T", " at "))
    w("Nothing has been changed in the account. This is the list to approve, written from a read-only pass on %s "
      "(%d requests to the account%s). Filters that look back in time, such as 'mailed more than a month ago', were "
      "worked out as of %s."
      % (plan["generated_at"].replace("T", " at "), plan["requests"]["made"], reused,
         plan.get("counted_as_of") or plan["generated_at"][:10]))
    w("")
    w("## The short version")
    w("")
    w("- %d presets in %d folders were read." % (s["presets_read"], s["folders"]))
    w("- %d would change, %d already carry everything, and %d are left alone on purpose."
      % (s["changed"], s["already_complete"], s["left_alone"]))
    w("- %s would be created." % _presets(s["to_create"], "new preset"))
    if s["presets_counted"]:
        w("- Across the %d changed presets that could be counted, the records they hold go from %s to %s. A record "
          "that sits in two presets is counted in both."
          % (s["presets_counted"], _n(s["records_now"]), _n(s["records_after"])))
    if plan["prerequisites"]:
        w("- Create these first: %s. Until they exist their rules wait; every other change can go ahead without them."
          % ", ".join("the %s %s" % (p["kind"], p["title"]) for p in plan["prerequisites"]))
    if plan["requests"]["not_counted"]:
        w("- %d counts were skipped because the request budget for this run was reached%s. Run the plan again to "
          "fill them in." % (plan["requests"]["not_counted"],
                             " (%s)" % plan["requests"]["stopped"] if plan["requests"].get("stopped") else ""))
    if plan["requests"]["errors"]:
        w("- %d counts were refused by the search; those presets read 'not counted' below."
          % len(plan["requests"]["errors"]))
    decide = []
    for r in plan.get("not_applied") or []:
        counted = "" if not r["not_counted"] else " (not counted on %s)" % _presets(r["not_counted"])
        if r["kind"] == "waiting":
            decide.append("**%s: waiting.** It belongs on %s. Across them it would remove %s today: %s marked "
                          "deceased and %s with a blank mark%s. Why it waits: %s. Until then the records marked "
                          "deceased stay in these presets."
                          % (r["say"], _presets(r["presets"]), _records(r["would_remove"]), _n(r["marked"]),
                             _n(r["blank"]), counted, r["because"]))
        else:
            decide.append("**%s: not applied in %s.** Reason: %s. It would have removed %s there%s. To apply it "
                          "anyway, take that lane's line out of the table at the top of the tool."
                          % (r["say"], _presets(r["presets"]), r["because"], _records(r["would_remove"]), counted))
    for c in plan["creates"]:
        decide += ["**%s (new preset).** %s" % (c["title"], f) for f in c["flags"]]
    if decide:
        w("")
        w("### Decide before you approve")
        w("")
        for line in decide:
            w("- %s" % line)
    w("")
    w("### Why records leave, and how many")
    w("")
    w("Each rule was counted on its own, preset by preset. A record that matches two rules is counted under each, "
      "so the lines add up to more than the total above.")
    w("")
    w("| Rule | Presets it changes | Records leaving | Records coming back |")
    w("|---|---|---|---|")
    for r in s["by_reason"]:
        note = "" if r["counted"] == r["presets"] else " (counted on %d)" % r["counted"]
        w("| %s | %d%s | %s | %s |" % (r["say"], r["presets"], note, _n(r["leaving"]), _n(r["returning"])))
    w("")
    w("### Left alone, and why")
    w("")
    for g in s["left_alone_groups"]:
        w("- %s of the kind '%s': %s." % (_presets(g["presets"]), g["role"], g["because"]))
    measured = [e for e in plan["presets"] if e.get("would_catch")]
    if measured:
        w("")
        w("These were not changed, but you should know that some of their records carry the Do Not Market tag or "
          "sit on the DNC list:")
        w("")
        for e in measured:
            w("- %s: %s of its %s." % (e["title"], _n(e["would_catch"]), _records(e["count_now"])))
    w("")
    w("### What could not be said in a preset")
    w("")
    for h in plan["held_back"]:
        w("- %s. Held back: %s." % (h["say"], h["because"]))
    for note in plan["standing_notes"]:
        w("- %s" % note)
    w("")
    w("### How each rule was checked")
    w("")
    w("The search ignores a filter it does not know and still answers, so each kind of rule was asked twice on the "
      "records that have a phone: without it and with it. A rule is used only when the two numbers differ.")
    w("")
    w("| Check | Without | With | Moved |")
    w("|---|---|---|---|")
    for p in plan["proofs"]:
        extra = ""
        if "with_opposite" in p:
            extra = " (asking for the records that carry it: %s)" % _n(p["with_opposite"])
        w("| %s | %s | %s%s | %s |" % (p["what"], _n(p["without"]), _n(p["with"]), extra, "yes" if p["moved"] else "NO"))
    if plan["watch"]:
        w("")
        w("### Numbers worth knowing before you approve")
        w("")
        for x in plan["watch"]:
            w("- %s: %s%s" % (x["what"], _n(x["count"]), (" (%s)" % x["note"]) if x["note"] else ""))
    flags = [(e["title"], f) for e in plan["presets"] for f in e["flags"]]
    conflicts = [(e["title"], c) for e in plan["presets"] for c in e["conflicts"]]
    notes = plan["requests"].get("notes") or []
    if flags or conflicts or notes:
        w("")
        w("### Needs your eye")
        w("")
        for title, f in flags:
            w("- %s: %s" % (title, f))
        for title, c in conflicts:
            w("- %s: %s." % (title, c))
        for note in notes:
            w("- %s" % note)
    if plan["creates"]:
        w("")
        w("## New presets")
        w("")
        for c in plan["creates"]:
            w("**%s** in %s (%s). %s It would hold %s today."
              % (c["title"], c["folder"], c["role_label"], c["why"], _records(c["count"])))
            for line in c["says"]:
                w("- %s." % line)
            out.extend(_waiting_lines(c))
            w("")
    w("")
    w("## Folder by folder")
    w("")
    w("Where a preset gets exactly the changes of one above it in the same folder, the list is not repeated: only "
      "the rules that move records in that preset are shown.")
    folder, shown = None, {}
    for e in plan["presets"]:
        if e["folder"] != folder:
            folder, shown = e["folder"], {}
            if out[-1] != "":
                w("")
            w("### %s" % folder)
            w("")
            both = [x for x in plan["presets"] if x["folder"] == folder and x["action"] == "change"
                    and None not in (x["count_now"], x["count_after"])]
            if len(both) > 1:
                w("Across the %d presets that change here: %s now, %s after." % (
                    len(both), _records(sum(x["count_now"] for x in both)), _n(sum(x["count_after"] for x in both))))
                w("")
        if e["action"] == "left_alone":
            w("**%s** (%s). Left alone: %s." % (e["title"], e["role_label"], e["left_alone_because"]))
            w("")
            continue
        if e["action"] == "already_complete":
            w("**%s** (%s). %s. Nothing to add." % (e["title"], e["role_label"], _records(e["count_now"])))
            out.extend(_waiting_lines(e))
            w("")
            continue
        w("**%s** (%s). %s now, %s after." % (e["title"], e["role_label"], _records(e["count_now"]),
                                              _n(e["count_after"])))
        same = (tuple(c["say"] for c in e["changes"]), tuple(sorted(wt["title"] for wt in e["waiting"])),
                tuple(e["held"]), tuple((x["pack"], x["kind"]) for x in e.get("not_applied") or []))
        first = shown.setdefault(same, e["title"])
        if first != e["title"]:
            w("- The same %d changes as %s." % (len(e["changes"]), first))
        for c in e["changes"]:
            n = e["by_reason"].get(c["pack"])
            if first != e["title"] and not n:
                continue
            w("- %s%s." % (c["say"], _moved(n)))
            if c["pack"] == "dead_owner" and e.get("dead_owner_blank"):
                w("  Of those, %s marked deceased and %s with the mark blank."
                  % (_n(e.get("dead_owner_marked")), _n(e["dead_owner_blank"])))
        if e.get("count_rewritten") is not None:
            w("- With the first two edits alone it would hold %s; the rules after them are counted on those."
              % _records(e["count_rewritten"]))
        for x in e.get("not_applied") or []:
            n = x.get("would_remove")
            if first != e["title"] and not n:
                continue
            if x["kind"] == "waiting":
                split = ""
                if n and x.get("marked") is not None:
                    split = " (%s marked deceased, %s with the mark blank)" % (_n(x["marked"]), _n(x["blank"]))
                w("- Waiting on the blank marks: %s. Here it would remove %s%s."
                  % (_mid(x["say"]), _n(n) if n is not None else "an uncounted number", split))
            else:
                w("- Not applied in this lane: %s. It would have removed %s here."
                  % (_mid(x["say"]), _n(n) if n is not None else "an uncounted number"))
        if first == e["title"]:
            out.extend(_waiting_lines(e))
            for h in e["held"]:
                w("- Held back: %s." % _mid(PACKS[h]["say"]))
            if e.get("note"):
                w("- Note: %s" % e["note"])
        w("")
    w("## When you approve")
    w("")
    w("The same tool sends exactly the filters recorded with this list, and nothing else. It needs two switches set "
      "by a person. Before each write it checks that the preset is still as it was read here, after the write it "
      "reads the preset back, and it stops at the first difference. New presets are created first. To drop a rule "
      "for good, take it out of the table at the top of the tool and ask for a new list.")
    return "\n".join(out).rstrip() + "\n"


def write_plan(plan: dict, out_dir: Path | str = OUT_DIR) -> tuple[Path, Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    j = out_dir / ("plan_%s.json" % plan["plan_date"])
    m = out_dir / ("plan_%s.md" % plan["plan_date"])
    j.write_text(json.dumps(plan, indent=1), encoding="utf-8", newline="\n")
    m.write_text(render_md(plan), encoding="utf-8", newline="\n")
    return j, m


# ------------------------------------------------------------------------------------------- apply
def may_write(commit: bool) -> bool:
    """Both switches, or nothing is sent."""
    return bool(commit) and os.environ.get(ALLOW_ENV) == "1"


def _write(crm, allowed: bool, path: str, method: str, body: dict):
    """The one place this module sends a write. Refuses unless both switches are on."""
    if not allowed:
        raise Refused("refused: a write needs --commit AND %s=1" % ALLOW_ENV)
    return crm.call(path, method, body)


def apply_plan(crm, plan: dict, *, commit: bool = False, only: list[str] | None = None, out=print) -> dict:
    """Send the approved plan. Dry unless both switches are on (a dry run touches nothing, not even
    to read). Each preset is read first and must still be the one the plan was written from; after
    the write it is read back and its filter must equal what was sent, exactly. The first
    difference stops the run."""
    allowed = may_write(commit)
    done = {"allowed": allowed, "created": [], "patched": [], "already": [], "skipped": [], "would": []}
    want = {t.strip().lower() for t in only or []}

    def chosen(title: str) -> bool:
        return not want or title.strip().lower() in want

    creates = [c for c in plan.get("creates") or [] if chosen(c["title"])]
    patches = [e for e in plan.get("presets") or [] if e.get("action") == "change" and chosen(e["title"])]
    if not allowed:
        done["would"] = ["create " + c["title"] for c in creates] + ["patch " + e["title"] for e in patches]
        out("DRY: nothing sent and nothing read. %d presets would be created or patched. A real run needs --commit "
            "and %s=1." % (len(done["would"]), ALLOW_ENV))
        return done

    # New presets first: each is cloned from a preset as it stands BEFORE that preset is patched.
    by_title = {e["title"]: e for e in plan.get("presets") or []}
    for c in creates:
        if any("already exists" in f for f in c.get("flags") or []):
            done["skipped"].append("%s: a preset with this title already exists" % c["title"])
            continue
        # A run that stopped part way has already made some of these. A preset of this title that
        # reads exactly as planned is finished work, not a conflict; one that reads differently is
        # somebody else's preset and stops the run.
        there = next((r for r in rows_of(crm.call("/api/internal/filter-preset-folder/%s/filter-preset/?limit=999"
                                                  % c["folder_uuid"])) if r.get("title") == c["title"]), None)
        if there is not None:
            if ((there.get("filters") or {}).get("must") or {}) == c["must_after"]:
                done["already"].append(c["title"])
                out("already  %s" % c["title"])
                continue
            raise Refused("stopped: a preset named %s already exists with a different filter" % c["title"])
        src = by_title.get(c["clone_of"]) or {}
        live = crm.call("/api/internal/filter-preset/%s/" % src["uuid"]) if src.get("uuid") else {}
        if sha(((live or {}).get("filters") or {}).get("must") or {}) != c["source_sha"]:
            # The source may already have been patched by an earlier, interrupted run of this same
            # plan. That is fine: the new preset is built from the plan's own filter, not from the
            # source as it stands. Anything else means the account moved under the plan.
            if (((live or {}).get("filters") or {}).get("must") or {}) != (src.get("must_after") or object()):
                raise Refused("stopped: %s changed since the plan was written; run plan again" % c["clone_of"])
        filters = {"must": c["must_after"]}
        if c.get("account"):
            filters["account"] = c["account"]
        made = _write(crm, allowed, "/api/internal/filter-preset/", "POST",
                      {"title": c["title"], "folder": c["folder_uuid"], "quick_filter": bool(c.get("quick_filter")),
                       "filters": filters, "type": "properties"}) or {}
        uuid = made.get("uuid") if isinstance(made, dict) else None
        if not uuid:
            rows = rows_of(crm.call("/api/internal/filter-preset-folder/%s/filter-preset/?limit=999" % c["folder_uuid"]))
            uuid = next((r.get("uuid") for r in rows if r.get("title") == c["title"]), None)
        back = crm.call("/api/internal/filter-preset/%s/" % uuid) if uuid else {}
        if ((back or {}).get("filters") or {}).get("must") != c["must_after"]:
            raise Refused("stopped: %s was created but reads back differently from what was sent" % c["title"])
        done["created"].append(c["title"])
        out("created  %s" % c["title"])

    for e in patches:
        path = "/api/internal/filter-preset/%s/" % e["uuid"]
        live = crm.call(path) or {}
        filters = copy.deepcopy(live.get("filters") or {})
        if (filters.get("must") or {}) == e["must_after"]:
            done["already"].append(e["title"])          # an earlier run of this plan got this far
            out("already  %s" % e["title"])
            continue
        if sha(filters.get("must") or {}) != e["before_sha"]:
            raise Refused("stopped: %s changed since the plan was written; run plan again" % e["title"])
        filters["must"] = e["must_after"]            # the PATCH replaces the whole filters object
        _write(crm, allowed, path, "PATCH", {"filters": filters})
        back = crm.call(path) or {}
        if (back.get("filters") or {}).get("must") != e["must_after"]:
            raise Refused("stopped: %s reads back differently from what was sent; nothing after it was touched"
                          % e["title"])
        done["patched"].append(e["title"])
        out("patched  %s" % e["title"])
    return done


# --------------------------------------------------------------------------------------------- cli
def key_only_crm(gap: float = 2.0):
    """The autopilot's CRM client on the Open API key and nothing else. No login is ever minted."""
    import datasift_api_upload as up
    from agents.autopilot.crm import CRM
    key = (up.env().get("REISIFT_API_KEY") or "").strip()
    if not key:
        raise PlanError("REISIFT_API_KEY is not set (run from the repo root); this tool uses the Open API key only")
    return CRM(api=up.Api(api_key=key), min_gap=max(gap, float(os.environ.get("AUTOPILOT_API_GAP_S") or 0)))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Preset exclusions: plan (read-only) and apply (gated).")
    sub = ap.add_subparsers(dest="cmd")
    pl = sub.add_parser("plan", help="read the presets, count, write the change list (the default)")
    pl.add_argument("--date", default="", help="the day the plan is for, YYYY-MM-DD (default today)")
    pl.add_argument("--out-dir", default=str(OUT_DIR))
    pl.add_argument("--cache", default="", help="a file that keeps listings and counts, so a re-run does not re-read")
    pl.add_argument("--max-requests", type=int, default=450)
    pl.add_argument("--gap", type=float, default=2.0, help="seconds between requests (2.0 is 30 a minute)")
    pl.add_argument("--no-reasons", action="store_true", help="skip the rule-by-rule counts (about half the requests)")
    pl.add_argument("--as-of", default="", help="the day relative date windows are worked out from, YYYY-MM-DD "
                                                "(default today; give the day of the first run to re-use its cache)")
    aq = sub.add_parser("apply", help="send an approved plan; dry unless --commit and %s=1" % ALLOW_ENV)
    aq.add_argument("--plan", required=True)
    aq.add_argument("--commit", action="store_true")
    aq.add_argument("--only", action="append", default=[], help="a preset title; repeat for more")
    aq.add_argument("--gap", type=float, default=2.0)
    raw = list(sys.argv[1:] if argv is None else argv)
    if not raw or raw[0] not in ("plan", "apply", "-h", "--help"):
        raw = ["plan"] + raw
    args = ap.parse_args(raw)
    if args.cmd == "apply":
        plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
        if not may_write(args.commit):
            apply_plan(None, plan, commit=args.commit, only=args.only)
            return 0
        done = apply_plan(key_only_crm(args.gap), plan, commit=True, only=args.only)
        print("created %d, patched %d" % (len(done["created"]), len(done["patched"])))
        return 0
    reader = Reader(key_only_crm(args.gap), cache_path=args.cache or None, max_requests=args.max_requests,
                    now=date.fromisoformat(args.as_of) if args.as_of else None)
    plan = build_plan(reader, reasons=not args.no_reasons, day=args.date)
    j, m = write_plan(plan, args.out_dir)
    s = plan["summary"]
    print("%d presets: %d change, %d already complete, %d left alone, %d to create"
          % (s["presets_read"], s["changed"], s["already_complete"], s["left_alone"], s["to_create"]))
    print("requests made %d, reused %d, not counted %d" % (plan["requests"]["made"], plan["requests"]["from_cache"],
                                                         plan["requests"]["not_counted"]))
    print(j)
    print(m)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

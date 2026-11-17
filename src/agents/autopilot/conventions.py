"""The weekly conventions check (build plan 5.4): does the account still follow its own rules.

Every preset finds its records through a list or a tag, so a convention that drifts breaks a filter
without an error. A list named for where the data came from cannot be stacked like a distress. A
tag sent as one comma string becomes one tag nobody filters on. A caller's preset that is not
starred is three clicks down the records page. None of these fails loudly, which is why they are
read once a week instead of noticed after a month.

What is checked, each from the account's own listings and a handful of counts:

  * LISTS NAME THE DISTRESS, TAGS NAME THE SOURCE. A list named for a source (a driving list, an
    import, a vendor), a list titled with an id, two lists that differ by a letter, and more
    than the two probate lists are named. So is a tag with a comma in it (an upload sent a
    string where a list of tags belonged), two tags that differ only by case, and a distress
    kept as a tag and as a list at once.
  * THE ZOMBIE SET. Vacant and in foreclosure together is its own lead; with no list or tag
    that names it, the stage counts how many records are on both lists today.
  * THE INBOUND SOURCE FIELD. A lead that called in has to say how it found us.
  * MONTH TAGS ON MAIL. Each mailed record carries "Mailed 2026-11". Records mailed lately
    with no month tag are counted, and a mail tag in another shape is named.
  * THE CALLERS' PRESETS ARE STARRED. The presets in the callers' folders are read and the
    ones not starred are named.
  * ACCOUNT SETTINGS. Auto update is on.

It is a check and nothing else: it reads, it lists what breaks a convention, and it leaves
every fix to a person. The words that mark a source, the lists that are allowed to be neither,
and the callers' folders are all settings in the playbook, because another account names its
things differently.
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import timedelta
from zoneinfo import ZoneInfo

from .. import store
from .crm import rows_of
from .stages import Ctx

log = logging.getLogger(__name__)

SOURCE_WORDS = ["d4d", "driving for dollars", "siftmap", "courthouse", "dataflik", "import", "imported", "upload",
                "uploaded", "pulled", "skip trace", "skiptrace", "tracerfy", "smartskip", "export"]
KNOWN_LISTS = ["DNC", "Dream 100"]
KNOWN_PREFIXES = ["Dispo - "]
PROBATE_LISTS = ["Probate", "Pre-Probate/Deceased"]
MAX_NAMES = 4


def _tokens(title: str) -> list[str]:
    return "".join(ch.lower() if ch.isalnum() else " " for ch in str(title or "")).split()


def _squash(title: str) -> str:
    return "".join(_tokens(title))


def _has_word(title: str, word: str) -> bool:
    """A whole word or phrase, never a piece of one: 'import' is not in 'important'."""
    want, have = _tokens(word), _tokens(title)
    return bool(want) and any(have[i:i + len(want)] == want for i in range(len(have) - len(want) + 1))


def looks_like_id(title: str) -> bool:
    parts = str(title or "").strip().lower().split("-")
    return [len(p) for p in parts] == [8, 4, 4, 4, 12] and all(c in "0123456789abcdef" for p in parts for c in p)


def one_letter_apart(a: str, b: str) -> bool:
    """Two different names one keystroke apart (a letter added, dropped or changed). A difference
    in a digit is a numbered series ("Rung 1", "Rung 2"), not a typing slip, and is not counted."""
    if a == b or abs(len(a) - len(b)) > 1 or min(len(a), len(b)) < 6:
        return False
    if len(a) > len(b):
        a, b = b, a
    i = 0
    while i < len(a) and a[i] == b[i]:
        i += 1
    if b[i].isdigit() or (len(a) == len(b) and a[i].isdigit()):
        return False
    return a[i:] == b[i + 1:] if len(a) < len(b) else a[i + 1:] == b[i + 1:]


def _names(items: list, limit: int = MAX_NAMES) -> str:
    items = list(items)
    more = len(items) - limit
    return ", ".join(str(x) for x in items[:limit]) + (" and %d more" % more if more > 0 else "")


def _size(row: dict):
    try:
        v = row.get("properties_count")
        return int(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------------------------- the checks
def check_lists(lists: list, cfg) -> list[str]:
    source = [str(w) for w in (cfg("source_words") or SOURCE_WORDS)]
    known = {str(x).strip().lower() for x in (cfg("known_lists") or KNOWN_LISTS)}
    prefixes = [str(x).lower() for x in (cfg("known_prefixes") or KNOWN_PREFIXES)]
    out = []
    titles = [str(r.get("title") or "").strip() for r in lists]
    ids = [r for r in lists if looks_like_id(r.get("title"))]
    if ids:
        out.append("%d list(s) are titled with an id instead of a name (%s). A pull sent an id where a list name "
                   "belonged." % (len(ids), _names("%s..., %s records" % (str(r["title"])[:8], _size(r) if _size(r) is not None else "unknown")
                                                 for r in ids)))
    named_source = [t for t in titles if not looks_like_id(t) and t.lower() not in known
                    and not any(t.lower().startswith(p) for p in prefixes) and any(_has_word(t, w) for w in source)]
    if named_source:
        out.append("%d list(s) name where the data came from, which is a tag's job: %s." % (len(named_source), _names(named_source)))
    by = defaultdict(list)
    for t in titles:
        if t and not looks_like_id(t):
            by[_squash(t)].append(t)
    keys = sorted(by)
    pairs = [(by[a][0], by[b][0]) for i, a in enumerate(keys) for b in keys[i + 1:] if one_letter_apart(a, b)]
    pairs += [(v[0], v[1]) for v in by.values() if len(v) > 1]
    if pairs:
        out.append("%d pair(s) of lists are the same name a keystroke apart: %s." % (len(pairs), _names("%s / %s" % p for p in pairs)))
    allowed = {_squash(x) for x in (cfg("probate_lists") or PROBATE_LISTS)}
    extra = [t for t in titles if "probate" in _tokens(t) and _squash(t) not in allowed]
    if extra:
        out.append("Probate is kept in two lists (probate and pre-probate); %d more carry the word: %s." % (len(extra), _names(extra)))
    return out


def check_tags(tags: list, lists: list, cfg) -> list[str]:
    allow = {str(x).strip().lower() for x in (cfg("allow_tags") or [])}
    out = []
    titles = [str(r.get("title") or "").strip() for r in tags]
    comma = [t for t in titles if "," in t]
    if comma:
        out.append("%d tag(s) have a comma in the name, so several tags were sent as one string: %s." % (
            len(comma), _names('"%s"' % t for t in comma)))
    by = defaultdict(set)
    for t in titles:
        if t and "," not in t:
            by[t.lower()].add(t)
    twins = [sorted(v) for v in by.values() if len(v) > 1]
    if twins:
        out.append("%d tag(s) exist twice with different capitals: %s." % (len(twins), _names(" / ".join(v) for v in sorted(twins))))
    source = [str(w) for w in (cfg("source_words") or SOURCE_WORDS)]
    distress = {_squash(str(r.get("title") or "")) for r in lists
                if r.get("title") and not looks_like_id(r["title"]) and not any(_has_word(r["title"], w) for w in source)}
    both = sorted({sorted(v)[0] for k, v in by.items() if _squash(k) in distress and k not in allow})
    if both:
        out.append("%d distress name(s) are kept as a tag and as a list at once: %s. Lists name the distress; a tag "
                   "says where a record came from." % (len(both), _names(both)))
    return out


def check_zombie(ctx: Ctx, lists: list, tags: list, cfg) -> list[str]:
    z = cfg("zombie") or {}
    names = [str(x) for x in (z.get("names") or ["Zombie"])]
    if any(_has_word(str(r.get("title") or ""), n) for r in lists + tags for n in names):
        return []
    by = {str(r.get("title") or "").strip().lower(): r for r in lists}
    a, b = by.get(str(z.get("vacant") or "Vacant").lower()), by.get(str(z.get("foreclosure") or "Foreclosure").lower())
    if not a or not b:
        missing = [n for n, r in ((z.get("vacant") or "Vacant", a), (z.get("foreclosure") or "Foreclosure", b)) if not r]
        return ["No zombie set (vacant and in foreclosure), and the account has no list titled %s to build one from."
                % " or ".join(missing)]
    # On both lists = on the first + on the second - on either. `any_lists` is the one list filter
    # the records search is proven on, so the overlap is worked out from it and never asked for by
    # a key that might be ignored.
    na = _size(a) if _size(a) is not None else ctx.crm.count({"any_lists": [a["uuid"]]})
    nb = _size(b) if _size(b) is not None else ctx.crm.count({"any_lists": [b["uuid"]]})
    either = ctx.crm.count({"any_lists": [a["uuid"], b["uuid"]]})
    overlap = na + nb - either
    if overlap < 0 or either > na + nb:
        return ["No zombie set, and the counts for %s and %s do not add up (%d, %d, %d on either), so the overlap "
                "could not be worked out." % (a["title"], b["title"], na, nb, either)]
    return ["No zombie set: %d record(s) are on both %s and %s, and no list or tag names them."
            % (overlap, a["title"], b["title"])]


def check_inbound_field(ctx: Ctx, cfg) -> list[str]:
    label = str(cfg("inbound_field") or "Inbound Source")
    return [] if ctx.crm.custom_field_uuid(label) else [
        "No custom field named %s: an inbound lead has nowhere to say how it found us." % label]


def check_mail_tags(ctx: Ctx, tags: list, cfg) -> list[str]:
    shape = str(cfg("mail_tag") or "Mailed {yyyy}-{mm}")
    prefix = shape.split("{")[0]
    out = []
    if not prefix.strip():
        return ["The mail tag %r has no fixed word to look for, so mail tags were not checked." % shape]
    local = ctx.now.astimezone(ZoneInfo(ctx.policy.tz)).date()
    start = local - timedelta(days=int(cfg("mail_days") or 35))
    mail_tags = [r for r in tags if str(r.get("title") or "").lower().startswith(prefix.lower())]
    good, bad = {}, []
    for r in mail_tags:
        rest = str(r["title"])[len(prefix):]
        if len(rest) == 7 and rest[4] == "-" and rest[:4].isdigit() and rest[5:].isdigit() and 1 <= int(rest[5:]) <= 12:
            good[rest] = r
        else:
            bad.append(str(r["title"]))
    if bad:
        out.append("%d mail tag(s) are not in the shape %s: %s." % (
            len(bad), shape.format(yyyy="%04d" % local.year, mm="%02d" % local.month), _names(bad)))
    mailed = ctx.crm.count({"last_direct_mailed": [start.isoformat(), (local + timedelta(days=1)).isoformat()]})
    if not mailed:
        return out
    months, d = [], start.replace(day=1)
    while d <= local:
        months.append("%04d-%02d" % (d.year, d.month))
        d = (d.replace(year=d.year + 1, month=1) if d.month == 12 else d.replace(month=d.month + 1))
    have = [good[m]["uuid"] for m in months if m in good and good[m].get("uuid")]
    if not have:
        out.append("%d record(s) were mailed since %s and no month tag exists for it (%s)." % (
            mailed, start.isoformat(), shape.format(yyyy=months[-1][:4], mm=months[-1][5:])))
        return out
    tagged = ctx.crm.count({"last_direct_mailed": [start.isoformat(), (local + timedelta(days=1)).isoformat()], "any_tags": have})
    if tagged > mailed:
        out.append("The mail counts do not add up (%d mailed, %d of them tagged), so the tag filter may be ignored; "
                   "month tags were not checked." % (mailed, tagged))
    elif mailed - tagged > 0:
        out.append("%d of %d record(s) mailed since %s carry no month tag." % (mailed - tagged, mailed, start.isoformat()))
    return out


def check_starred(ctx: Ctx, cfg) -> tuple[list[str], str]:
    """(what breaks, a note when the check could not be made)."""
    want = [str(x).strip() for x in (cfg("caller_folders") or [])]
    if not want:
        return [], "No caller folder is named in the playbook, so starred presets were not checked."
    field = str(cfg("starred_field") or "quick_filter")
    # Folders are matched on their letters and digits only, so "05 FTM - CALL" finds "05. FTM - Call".
    folders = {_squash(f.get("title")): f for f in rows_of(
        ctx.crm.call("/api/internal/filter-preset-folder/?type=properties&limit=999"))}
    out, plain, unread = [], [], 0
    for title in want:
        f = folders.get(_squash(title))
        if not f:
            out.append("No preset folder titled %s, so its presets could not be checked." % title)
            continue
        # A folder listing shows ten presets unless it is asked for more.
        for p in rows_of(ctx.crm.call("/api/internal/filter-preset-folder/%s/filter-preset/?limit=999" % f["uuid"])):
            if field not in p:
                unread += 1
            elif not p.get(field):
                plain.append(str(p.get("title") or ""))
    if plain:
        out.append("%d of the callers' presets are not starred: %s." % (len(plain), _names(plain)))
    note = ("The preset listing did not say whether %d preset(s) are starred, so those were not checked." % unread) if unread else ""
    return out, note


def check_settings(ctx: Ctx, cfg) -> list[str]:
    want = cfg("settings") or {"auto_update": True}
    said = {"auto_update": "Auto update is off in Settings, so records and owners are not being refreshed."}
    user = ctx.crm.call("/api/internal/user/") or {}
    out = []
    for key, should in want.items():
        label = key.replace("_", " ")
        if key not in user:
            out.append("The account did not say whether %s is on, so it was not checked." % label)
        elif bool(user[key]) != bool(should):
            out.append(said[key] if should and key in said else "%s is %s in Settings and the playbook expects it %s." % (
                label.capitalize(), "on" if user[key] else "off", "on" if should else "off"))
    return out


# ---------------------------------------------------------------------------------------- the stage
def run_conventions(ctx: Ctx) -> None:
    st = ctx.stage
    cfg = st.get
    skip = {str(x) for x in (cfg("skip") or [])}
    try:
        lists = rows_of(ctx.crm.call("/api/internal/list/?offset=0&limit=1000"))
        tags = rows_of(ctx.crm.call("/api/internal/tag/?offset=0&limit=10000&ordering=title"))
    except Exception as exc:  # noqa: BLE001
        ctx.out.problem("could not read the account's lists and tags (%s); nothing was checked" % str(exc)[:120])
        ctx.out.head = "The conventions check could not read the account."
        return
    ctx.out.found = len(lists) + len(tags)
    groups = [
        ("lists", lambda: check_lists(lists, cfg)),
        ("tags", lambda: check_tags(tags, lists, cfg)),
        ("zombie", lambda: check_zombie(ctx, lists, tags, cfg)),
        ("inbound", lambda: check_inbound_field(ctx, cfg)),
        ("mail", lambda: check_mail_tags(ctx, tags, cfg)),
        ("settings", lambda: check_settings(ctx, cfg)),
    ]
    breaks: dict[str, list] = {}
    for name, fn in groups:
        if name in skip:
            continue
        try:
            breaks[name] = fn()
        except Exception as exc:  # noqa: BLE001  (one check failing must not hide the others)
            ctx.out.problem("the %s check could not be made (%s)" % (name, str(exc)[:120]))
    if "starred" not in skip:
        try:
            breaks["starred"], note = check_starred(ctx, cfg)
            if note:
                ctx.out.problem(note)
        except Exception as exc:  # noqa: BLE001
            ctx.out.problem("the starred presets check could not be made (%s)" % str(exc)[:120])
    total = 0
    for name, found in breaks.items():
        ctx.out.tally["conventions broken: %s" % name] = len(found)
        total += len(found)
        for text in found:
            ctx.out.line(text)
    ctx.out.acted = total
    store.kv_set("autopilot:conventions:last", json.dumps({
        "at": ctx.now.isoformat(timespec="seconds"), "broken": total, "by_check": {k: v for k, v in breaks.items()}}))
    checked = "%d lists and %d tags" % (len(lists), len(tags))
    if total:
        ctx.out.head = "%d thing(s) break the account's conventions (%s read). Nothing was changed." % (total, checked)
    elif ctx.out.problems:
        ctx.out.head = "No convention is broken in what could be read (%s), and %d check(s) could not be made." % (
            checked, len(ctx.out.problems))
    else:
        ctx.out.head = "The account follows its conventions (%s read)." % checked

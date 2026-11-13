"""Text lane selftest: the do-not-call registry scrub, the outreach limits (the check before a send
and the nightly check after the day), the text lane's copy, its opening and follow-up series, the
release of a staged batch and the afternoon text. Zero network: the same fake DataSift as the
autopilot selftest, the selftest's throwaway database, and no texting system anywhere. Every name,
street and number here is invented.
"""

from __future__ import annotations


import copy


import json


from datetime import datetime, timedelta


from pathlib import Path


from . import config


from .selftest_autopilot import EMPLOYEES, ET, PRESETS, FakeCRM, _rec


from .selftest_phase0 import _ts, tag_ev


# The three lines this track hands the orchestrator for autopilot/registry.py. Until they are in
# that file the section adds them for its own run and takes them out again, so the checks below
# run through the same playbook parser and stage runner as every other stage.
REGISTRY = {
    "textlane": ("textlane", "run_textlane", "outreach", False, False),
    "limits": ("limits", "run_limits", "free", False, False),
    "scrub": ("scrub", "run_scrub", "free", True, False),
}

CALL_PRESETS = ["FTM - 02 Ready to Call", "FTM - 03 Call Attempt 1", "FTM - 04 Call Attempt 2"]

PLAYBOOK = {
    "business": {"tz": "America/New_York", "hours": "09:00-18:00", "days": ["Mon", "Tue", "Wed", "Thu", "Fri"]},
    "caps": {"daily_usd": 25, "record_usd": 1.0},
    "people": {"callers": ["Adriana"]},
    "lanes": {"ftm": {"tags": ["FTM"]}},
    "stages": {
        "t_scrub": {"kind": "scrub", "owner": "ops", "mode": "plan", "schedule": "08:20 Mon-Fri", "lane": "ftm",
                    "presets": ["FTM - 02 Ready to Call"]},
        "t_limits": {"kind": "limits", "owner": "ops", "mode": "plan", "schedule": "19:20 Mon-Fri"},
        "t_open": {"kind": "textlane", "owner": "ops", "mode": "live", "schedule": "08:45 Mon-Fri", "lane": "ftm",
                   "series": "opening", "presets": list(CALL_PRESETS)},
        "t_follow": {"kind": "textlane", "owner": "ops", "mode": "live", "schedule": "10:15 Mon-Fri", "lane": "ftm",
                     "series": "followup"},
        "t_full": {"kind": "fullattempt", "owner": "sales", "mode": "live", "schedule": "16:30 Mon-Fri", "lane": "ftm",
                   "afternoon_text": True},
    },
}


def mob(n: int, tier: str = "Dial First", **kw) -> dict:
    """An invented mobile number in the 865 area code (Eastern), one per test owner."""
    return dict({"number": "865555%04d" % n, "type": "MOBILE", "tags": [tier], "status": "UNKNOWN"}, **kw)


def at(day: int, hour: int = 8, minute: int = 45, month: int = 11) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=ET)


class Crm(FakeCRM):
    """The fake DataSift, counting what a stage reads so a bound on reads can be checked."""

    def __init__(self, records, **kw):
        super().__init__(records, **kw)
        self.rec_logs, self.log_rows = {}, []
        self.reads = self.log_reads = 0

    def record(self, u):
        self.reads += 1
        return super().record(u)

    def record_logs(self, u, event_types=None, limit=250):
        self.log_reads += 1
        return super().record_logs(u, event_types, limit)

    def land(self, u: str, when: datetime, tags=("FTM",)) -> None:
        """The record entered its lane at `when`: the lane tag landed on it then."""
        self.rec_logs[u] = [tag_ev("in-" + u, u, when, list(tags))]


def call_ev(i, owner: str, when: datetime, number: str, line: str = "8655550000", kind: str = "made", duration: int = 40,
            caller: str = "Adriana") -> dict:
    """One row of the account's call log, shaped like the real route's: an outbound dial to
    `number` from the caller ID `line`."""
    return {"id": "tl-call-%s" % i, "event_type": "owner.call.%s" % kind, "resource": owner, "resource_type": "owner",
            "source": "kixie", "author": "system", "timestamp": _ts(when),
            "payload": {"call": {"destination_number": number, "origin_number": line, "direction": "outbound", "duration": duration,
                                 "source": "kixie", "owner": owner, "uuid": "c-%s" % i, "extra": {},
                                 "user": {"first_name": caller, "uuid": "u-" + caller.lower()} if caller else {}}}}


def _register(policy, registry):
    """Add this track's stage kinds for the run, unless the orchestrator already has. Returns the
    function that puts everything back."""
    added = [k for k in REGISTRY if k not in registry.EXTRA]
    saved = (policy.KINDS, policy.NO_PRESETS)
    spend_added = [k for k in REGISTRY if k not in policy.SPEND_BY_KIND]
    for k in added:
        registry.EXTRA[k] = REGISTRY[k]
    policy.KINDS = tuple(dict.fromkeys(tuple(policy.KINDS) + tuple(REGISTRY)))
    policy.SPEND_BY_KIND.update({k: v[2] for k, v in REGISTRY.items()})
    policy.NO_PRESETS = tuple(dict.fromkeys(tuple(policy.NO_PRESETS) + tuple(k for k, v in REGISTRY.items() if not v[3])))

    def restore():
        for k in added:
            registry.EXTRA.pop(k, None)
        for k in spend_added:
            policy.SPEND_BY_KIND.pop(k, None)
        policy.KINDS, policy.NO_PRESETS = saved
    return restore


def run(check, tmp: Path) -> None:
    from . import store
    from .autopilot import activity, fullattempt, gate, lanes, ledger, limits, policy, registry, scrub, sweep, textlane
    saved = (config.DATA_DIR, config.DAILY_CAP_USD, config.RUN_CAP_USD)
    config.DATA_DIR, config.DAILY_CAP_USD, config.RUN_CAP_USD = tmp, 100000.0, 5.0
    config.MODE = "live"
    store.init()
    saved_presets = dict(PRESETS)
    PRESETS["FTM - 03 Call Attempt 1"] = lambda r: "FTM" in r["tags"] and r["owner"]["phones"] and r["predictivecall_attempts"] == 1
    PRESETS["FTM - 04 Call Attempt 2"] = lambda r: "FTM" in r["tags"] and r["owner"]["phones"] and r["predictivecall_attempts"] == 2
    restore = _register(policy, registry)
    try:
        _run(check, tmp, store, activity, fullattempt, gate, lanes, ledger, limits, policy, registry, scrub, sweep, textlane)
    finally:
        restore()
        PRESETS.clear()
        PRESETS.update(saved_presets)
        config.DATA_DIR, config.DAILY_CAP_USD, config.RUN_CAP_USD = saved
        config.MODE = "dry"


def _wipe(store, activity, limits, scrub) -> None:
    """Start a group of checks from an empty autopilot memory, so a record id an earlier group (or
    an earlier section) used cannot leak into this one."""
    limits.ensure()
    scrub.ensure()
    with store._LOCK, store._db() as c:
        for table in ("ap_events", "ap_facts", "ap_changes", "ap_batches", "ap_owner_lane", "ap_log", "ap_phones",
                      "ap_texts", "ap_dnc"):
            c.execute("DELETE FROM " + table)
        c.execute("DELETE FROM kv WHERE key LIKE 'autopilot:%'")


def _run(check, tmp, store, activity, fullattempt, gate, lanes, ledger, limits, policy, registry, scrub, sweep, textlane) -> None:
    pol = policy.parse(copy.deepcopy(PLAYBOOK), EMPLOYEES)
    _wipe(store, activity, limits, scrub)
    TZ = "America/New_York"

    def screened(*uuids) -> None:
        for u in uuids:
            ledger.set_fact(u, "death_screen", {"at": "2026-11-09T12:00:00+00:00", "hit": "", "detail": ""})

    # ------------------------------------------------------------------ the registry lines
    print("  registry lines")
    check(all(registry.EXTRA.get(k) == v for k, v in REGISTRY.items()),
          "the three stage kinds are registered exactly as this track hands them over")
    check(not [p for p in registry.problems() if p.split(":")[0] in REGISTRY],
          "each registered kind names a module and a run function that load")
    check(pol.stage("t_open").spend == "outreach" and pol.stage("t_limits").spend == "free" and pol.stage("t_scrub").spend == "free",
          "the text lane is outreach spend; the scrub and the limits check are free")
    try:
        policy.parse({**copy.deepcopy(PLAYBOOK), "stages": {"x": {"kind": "scrub", "owner": "ops", "mode": "plan", "schedule": "08:20"}}}, EMPLOYEES)
        check(False, "a scrub stage with no preset is refused: it has nothing to read")
    except policy.PolicyError:
        check(True, "a scrub stage with no preset is refused: it has nothing to read")

    # ------------------------------------------------------------------ the copy
    print("  copy")
    rows = textlane.variants()
    check(textlane.audit(fresh=True) == [], "every variant passes the audit: %s" % (textlane.audit()[:2] or "ok"))
    check(len(rows) >= 40 and all(textlane.copy_problems(t, named=n, role=r) == [] for _, t, n, r in rows),
          "each of the %d variants passes on its own: one easy question, one segment, signed, street only" % len(rows))
    check(not any(textlane._LIST.search(t) for _, t, _, _ in rows), "no variant names the list")
    check(not any(ch.isdigit() or ch == "$" for _, t, _, _ in rows for ch in t), "no variant carries a number or a dollar amount")
    check(not any(chr(0x2014) in t or chr(0x2013) in t or ";" in t for _, t, _, _ in rows), "no variant carries a dash or a semicolon")
    check(all("{first}" not in t for _, t, named, _ in rows if not named) and all("{first}" in t for _, t, named, _ in rows if named),
          "a no-name variant never greets by first name, and a named one always does")
    check(max(len(t.format(**textlane.WORST)) for _, t, _, role in rows if role != "thread") <= textlane.TEMPLATE_CAP,
          "every template fits one segment at the worst-case name and street, with room for the opt-out line")
    bad = textlane.copy_problems

    def has(text, word, **kw):
        return any(word in p for p in bad(text, named=True, **kw))

    ok_text = "Hi {first}, it's {sender}. Is {addr} something you'd sell?"
    check(bad(ok_text, named=True) == [], "a plain signed question about the street passes")
    check(has("Hi {first}, I saw the auction on {addr}. Want to talk? {sender}", "names the list")
          and has("Hi {first}, I know about the foreclosure on {addr}. Call me? {sender}", "names the list")
          and has("Hi {first}, is {addr} in probate? {sender}", "names the list"), "naming the list is caught, in any of its words")
    check(has("Hi {first}, I can pay 90k for {addr}. Deal? {sender}", "a number or a dollar amount"), "a dollar amount is caught")
    check(has("Hi {first}, is {addr} yours? Would you sell it? {sender}", "questions"), "two questions are caught")
    check(has("Hi {first}, I know things are a struggle. Is {addr} yours? {sender}", "situation"), "naming the owner's situation is caught")
    check(has("Hi {first}, last one from me, I promise. Is {addr} yours? {sender}", "promises an ending"), "a promise the lane breaks is caught")
    check(has("Hi {first}, is {addr} yours " + chr(0x2014) + " or not? {sender}", "dash"), "an em dash is caught")
    check(has("Hi {first}, see example.com about {addr}. Yours? {sender}", "a link"), "a link is caught")
    check(has("Hi {first}, is {addr} in {city} yours? {sender}", "unknown placeholder"), "a merge field the texting platform does not have is caught")
    check(has("Hi {first}, is {addr} yours?", "not signed"), "an unsigned text is caught")
    check(has("Hi {first}, " + "really " * 20 + "is {addr} yours? {sender}", "characters at the worst case"), "a text over one segment is caught")
    check(any("greets by first name" in p for p in bad("Hi {first}, is {addr} yours? {sender}", named=False)),
          "a first name in a no-name pool is caught")
    check(bad("Hey {first}, last note from me about {addr} for a while. Take care. {sender}", named=True, role="breakup") == [],
          "a breakup may end without a question, and says for a while")
    titles = [t["title"] for t in textlane.templates("Adriana")]
    check(len(titles) == len(set(titles)) == 36 and max(len(t) for t in titles) <= textlane.TITLE_CAP,
          "the 36 follow-up and urgent templates have unique titles inside the platform's limit")
    check(all("<First Name>" in t["content"] or t["audience"] == "noname" for t in textlane.templates("Adriana"))
          and all("{" not in t["content"] and "Adriana" in t["content"] for t in textlane.templates("Adriana")),
          "a template is exported with the platform's merge tags and the sender as a literal first name")
    a1 = textlane.render_afternoon("r1", "Pat", "12 Oak St", "Adriana", 3)
    a2 = textlane.render_afternoon("r1", "Pat", "12 Oak St", "Adriana", 4)
    a3 = textlane.render_afternoon("r1", "", "12 Oak St", "Adriana", 4)
    check(a1 != a2 and "Pat" in a1 and "12 Oak St" in a1 and "Adriana" in a1 and "Pat" not in a3 and "{" not in a1 + a3,
          "the afternoon text changes from one day to the next and has a no-name form")
    mod = textlane.sc_templates()
    check(mod is not None, "the existing Smarter Contact template module loads here, so its rules are run over the copy too")
    if mod is not None:
        owners = [{"first_name": "Pat", "last_name": "Example"}, {"first_name": "E A", "last_name": "Example"},
                  {"company": "Example Holdings LLC"}, {"first_name": "PAT", "last_name": "Example"},
                  {"first_name": "Pat Lee", "last_name": "Example"}, {"first_name": "", "last_name": "Example"}]
        check([bool(textlane.no_name_reason(o)) for o in owners] == [bool(mod.no_name_reason(o)) for o in owners] == [False, True, True, True, True, True],
              "who is greeted by name follows the same rule as the existing templates")
        check(textlane.template_titles("p1", 1, "named")[0] == mod.title(1, "", "named", 1)
              and len(textlane.template_titles("p1", 2, "noname")) == len(mod.TOUCH2_NONAME),
              "texts 1 to 3 name the Priority 1 templates the account already holds")
    check(textlane.greeting_name({"first_name": "E A", "last_name": "Example"}) == "" and textlane.greeting_name({"first_name": "pat", "last_name": "Example"}) == "Pat",
          "an owner known only by initials is never greeted by the surname")

    mods = dict(store=store, activity=activity, fullattempt=fullattempt, gate=gate, lanes=lanes, ledger=ledger, limits=limits,
                policy=policy, scrub=scrub, sweep=sweep, textlane=textlane, screened=screened, tmp=tmp)
    _scrub_checks(check, **mods)
    _limits_checks(check, **mods)
    _nightly_checks(check, **mods)
    _opening_checks(check, **mods)
    _release_checks(check, **mods)
    _followup_checks(check, **mods)
    _afternoon_checks(check, **mods)
    _handover_checks(check, **mods)


def _digits_in(summary) -> bool:
    """True when a stage's whole output carries a ten-digit run anywhere: a phone number in full."""
    import re
    return bool(re.search(r"\d{10}", json.dumps(summary.as_dict(), default=str)))


def _world(specs, screened) -> "Crm":
    """A fake account from (uuid, street, the moment it entered the lane, phones, extra record
    fields): every record landed in its lane then, and already seen by the death screen."""
    crm = Crm([_rec(u, street, phones=phones, **kw) for u, street, _, phones, kw in specs])
    for u, _, entered, _, _ in specs:
        if entered is not None:
            crm.land(u, entered)
    screened(*[s[0] for s in specs])
    return crm


def _with(stage: str, **changes) -> dict:
    """The test playbook with one stage's settings changed."""
    out = copy.deepcopy(PLAYBOOK)
    out["stages"][stage].update(changes)
    return out


def _scrub_checks(check, *, store, activity, gate, ledger, limits, policy, scrub, sweep, **_) -> None:
    print("  registry scrub")
    pol = policy.parse(copy.deepcopy(PLAYBOOK), EMPLOYEES)
    _wipe(store, activity, limits, scrub)
    n1, n2, n3 = "8655551001", "8655551002", "8655551003"
    row_flag = {"uuid": "s1", "phone": {"number": "(865) 555-1001", "doNotCall": True}}
    check(scrub.shown(row_flag) == (n1, True) and scrub.shown({"phone": {"number": n2, "doNotCall": False}}) == (n2, False)
          and scrub.shown({"phone": {"number": n3}}) == (n3, None) and scrub.shown({"phone": None}) == ("", None),
          "a search row says flagged, not flagged, or nothing at all about the one number it shows")
    got = scrub.note_rows([row_flag, {"uuid": "s2", "phone": {"number": n2, "doNotCall": False}}, {"uuid": "s3", "phone": {"number": n3}}], "ftm")
    check(got == {"rows": 3, "shown": 3, "answered": 2, "flagged": 1, "new": 1}, "a page of rows is counted: shown, answered, flagged, new")
    check(scrub.is_flagged(n1) and scrub.status_of(n2) == "clear" and scrub.status_of(n3) == "unseen",
          "a flagged number is remembered, and a row that says nothing teaches nothing")
    scrub.note_rows([{"uuid": "s9", "phone": {"number": n1, "doNotCall": False}}])
    check(scrub.is_flagged(n1), "a later row that shows the number without the flag does not clear it")
    check(scrub.note_rows([row_flag])["new"] == 0 and scrub.count() == 1, "the same flag seen again is not new")
    nums = [{"number": n1}, {"number": n2}, {"number": n3}]
    keep, out = scrub.split(nums, "text", pol)
    check([n["number"] for n in keep] == [n2, n3] and [n["number"] for n in out] == [n1],
          "a text never uses a remembered flagged number, with no search row in hand")
    check([n["number"] for n in scrub.split(nums, "call", pol)[0]] == [n2, n3],
          "a flagged number is held from calls too, until the attorney answers")
    allow = copy.deepcopy(PLAYBOOK)
    allow["stages"]["t_scrub"]["calls"] = "allow"
    pol_allow = policy.parse(allow, EMPLOYEES)
    check(len(scrub.split(nums, "call", pol_allow)[0]) == 3 and len(scrub.split(nums, "text", pol_allow)[0]) == 2,
          "calls: allow lets a call use it again; a text still never does")
    strict = copy.deepcopy(PLAYBOOK)
    strict["stages"]["t_scrub"].update(hold_unseen=True, mode="off")
    pol_strict = policy.parse(strict, EMPLOYEES)
    check([n["number"] for n in scrub.split(nums, "text", pol_strict)[0]] == [n2],
          "hold_unseen also holds a number no row has shown with the flag off, and the rule holds with the sweep switched off")
    k2, _o2 = scrub.split([{"number": "8655551004"}], "text", pol, row={"uuid": "s4", "phone": {"number": "8655551004", "doNotCall": True}})
    check(not k2 and scrub.is_flagged("8655551004"), "a row handed to the rule is remembered before the rule is applied")
    check(scrub.forget(n1) and not scrub.is_flagged(n1) and not scrub.forget(n1), "only a person clears a number")
    scrub.note(n1, True)
    rec = _rec("s5", "21 Scrub St", phones=[mob(1001), mob(1005, "Dial Second")])
    v = gate.check(rec, "text")
    check([n["number"] for n in v.numbers] == [n1, "8655551005"]
          and [n["number"] for n in scrub.split(v.numbers, "text", pol)[0]] == ["8655551005"],
          "the full record carries no flag, so the gate alone would text the number; the remembered flag takes it out")

    # the stage: a sweep of the lane's presets, in plan mode
    _wipe(store, activity, limits, scrub)
    recs = [_rec("c1", "31 Flag St", phones=[mob(1101)], row_phone={"number": "8655551101", "doNotCall": True}),
            _rec("c2", "32 Flag St", phones=[mob(1102), mob(1103, "Dial Second")], row_phone={"number": "8655551102", "doNotCall": True}),
            _rec("c3", "33 Flag St", phones=[mob(1104), dict(mob(1105), type="LANDLINE")], row_phone={"number": "8655551104", "doNotCall": True}),
            _rec("c4", "34 Flag St", phones=[mob(1106)], row_phone={"number": "8655551106", "doNotCall": False}),
            _rec("c5", "35 Flag St", phones=[mob(1107)], row_phone={"number": "8655551107"})]
    crm = Crm(recs)
    s = sweep.run("t_scrub", crm=crm, now=at(9, 8, 20), pol=pol)
    check(s.tally["numbers shown on the lane's rows"] == 5 and s.tally["flagged by the do-not-call registry"] == 3 and s.acted == 3
          and "3 of 5 numbers shown in the ftm lane" in s.head and "(60%)" in s.head,
          "the sweep says what share of the lane's shown numbers the registry flags")
    check(any("FTM - 02 Ready to Call: 3 of 5" in ln for ln in s.lines), "the share is given preset by preset")
    check(s.tally["flagged records opened"] == 3 and s.tally["of those, left with no number to text"] == 2
          and s.tally["of those, left with no number to call (mail only)"] == 1 and crm.reads == 3,
          "it opens only the flagged records and says how many are left with nothing to text, and nothing to call")
    check(not crm.writes and not s.live and scrub.count() == 3,
          "in plan mode the sweep writes nothing to the account and still remembers what it saw")
    check(not _digits_in(s), "no line of the sweep shows a number in full")
    s2 = sweep.run("t_scrub", crm=crm, now=at(9, 9, 20), pol=pol)
    check(s2.acted == 0 and crm.reads == 3 and s2.tally["flagged by the do-not-call registry"] == 3,
          "a second sweep finds nothing new and does not open the same records again")
    check("3 of 5" in scrub.recent_line("t_scrub") and scrub.recent_line("nobody") == "", "the last sweep is one line for the digest")
    _wipe(store, activity, limits, scrub)
    capped = copy.deepcopy(PLAYBOOK)
    capped["stages"]["t_scrub"]["max_records"] = 1
    crm2 = Crm(recs)
    s3 = sweep.run("t_scrub", crm=crm2, now=at(9, 8, 20), pol=policy.parse(capped, EMPLOYEES))
    check(crm2.reads == 1 and s3.tally["flagged records not opened yet (read cap, next run)"] == 2,
          "the sweep opens a set number of records a run and leaves the rest for the next one")
    crm3 = Crm([_rec("c9", "39 Flag St", phones=[mob(1109)], row_phone={"number": "8655551109"})])
    s4 = sweep.run("t_scrub", crm=crm3, now=at(9, 8, 20), pol=pol)
    check(any("no row in the lane says" in p for p in s4.problems),
          "when no row carries the flag field at all, the sweep says the search may have stopped sending it")



def _unchecked(pol, stage_id, **changes):
    """A copy of a parsed playbook with one stage's block changed AFTER the load check. The playbook
    refuses a block with a typo when it loads (policy.parse runs each kind's block_problems), so
    this is how a test reaches what a stage itself does with a block that should never get that far."""
    p2 = copy.deepcopy(pol)
    st = p2.stages[stage_id]
    for k, v in changes.items():
        if k != "params" and hasattr(st, k):
            setattr(st, k, v)
        else:
            st.params[k] = v
    return p2

def _limits_checks(check, *, store, activity, ledger, limits, policy, scrub, **_) -> None:
    print("  limits: room before the send")
    pol = policy.parse(copy.deepcopy(PLAYBOOK), EMPLOYEES)
    _wipe(store, activity, limits, scrub)
    tz = "America/New_York"
    lim, bad = limits.read(pol)
    check(lim.day_capacity == 250 and lim.owner_texts_day == 1 and lim.owner_texts_week == 5 and lim.text_hours == (540, 1080)
          and lim.call_hours == (540, 1140) and lim.calls_per_line_day == 25 and lim.lines_per_caller == 10 and not bad,
          "the house limits are the defaults: 25 a number, 10 numbers, one text a day, five a week, 9 to 6 and 9 to 7")
    edit = copy.deepcopy(PLAYBOOK)
    edit["stages"]["t_limits"].update(text_hours="10:00-17:00", owner_texts_week=3, text_lines=2, mode="off")
    pol2 = policy.parse(edit, EMPLOYEES)
    lim2, _ = limits.read(pol2)
    check(lim2.text_hours == (600, 1020) and lim2.owner_texts_week == 3 and lim2.day_capacity == 50,
          "the playbook's limits block changes a limit, and it applies with the nightly check switched off")
    typo = copy.deepcopy(edit)
    typo["stages"]["t_limits"].update(calls_per_line_day="many", call_hours="soon")
    try:
        policy.parse(typo, EMPLOYEES)
        refused = ""
    except policy.PolicyError as exc:
        refused = str(exc)
    check("call_hours" in refused and "calls_per_line_day" in refused,
          "a limit that cannot be read is refused when the playbook loads, so a typo never runs quietly")
    pol2.stages["t_limits"].params.update(calls_per_line_day="many", call_hours="soon")   # a block that got past the load
    lim3, bad3 = limits.read(pol2)
    check(lim3.calls_per_line_day == 25 and lim3.call_hours == (540, 1140) and len(bad3) == 2,
          "and one that gets past the load keeps its default and is said out loud, never read as no limit")

    east, central, pacific, alaska = "8655552001", "6155552002", "3105552003", "9075552004"
    mon = at(9, 9, 30)
    check(limits.inside_hours(lim.text_hours, mon, tz, east)[0] and not limits.inside_hours(lim.text_hours, at(9, 8, 30), tz, east)[0]
          and not limits.inside_hours(lim.text_hours, at(9, 18, 0), tz, east)[0],
          "what the machine sends goes out 9am to 6pm on the business clock")
    check(not limits.inside_hours(lim.text_hours, mon, tz, pacific)[0] and limits.inside_hours(lim.text_hours, at(9, 12, 30), tz, pacific)[0],
          "and inside the owner's own daytime by area code: 9:30 Eastern is 6:30 in California")
    check(limits.inside_hours(lim.call_hours, at(9, 18, 30), tz, east, "call")[0]
          and not limits.inside_hours(lim.call_hours, at(9, 19, 0), tz, east, "call")[0], "people call until 7pm")
    day = mon.date()
    ps = {n: limits.planned_send(n, day, lim, tz) for n in (east, central, pacific, alaska)}
    check(ps[east][:2] == ("east", at(9, 9, 0)) and ps[central][:2] == ("other", at(9, 12, 0)) and ps[pacific][0] == "other",
          "a campaign text is planned for 9am in the east group and noon for everyone else, as the launcher splits them")
    check(ps[alaska][1] is None and "outside" in ps[alaska][2],
          "a number whose own morning has not started by noon Eastern fits neither group and is left out")
    check(limits.planned_send(east, day, lim, tz, not_before=at(9, 14, 0))[1] == at(9, 14, 0)
          and limits.planned_send(east, day, lim, tz, not_before=at(9, 18, 5))[1] is None,
          "a late run plans from now on, and plans nothing once the window has closed")

    check(limits.zones_known(), "the area code table the texting app keeps loads here, with no network")
    saved_zone = list(limits._ZONE_FN)
    limits._ZONE_FN[:] = [None]                           # a box where that table cannot be loaded
    try:
        check(not limits.zones_known() and limits.planned_send(east, day, lim, tz)[:2] == ("other", at(9, 12, 0))
              and not limits.inside_hours(lim.text_hours, mon, tz, east)[0] and limits.inside_hours(lim.text_hours, at(9, 12, 30), tz, east)[0],
              "without the area code table every number waits for the late group: never the early one on a guess")
    finally:
        limits._ZONE_FN[:] = saved_zone

    def text(owner, n, when, batch, release=True, campaign=True, system="smarter_contact", number=east, step=1):
        limits.stage_text(stage="t_open", batch_id=batch, owner_uuid=owner, record_uuid="r-" + owner,
                          numbers=[{"number": number, "thread": system + ":G", "send_at": limits._iso(when)}], system=system,
                          series="opening", step=step, touch="t-%s-%d" % (owner, n), tz=tz, campaign=campaign)
        if release:
            limits.release_text(batch, "r-" + owner, [number])

    def room(owner, when, **kw):
        kw.setdefault("system", "smarter_contact")
        return limits.room(owner, "text", when, policy=pol, **kw)

    check(room("o-l1", mon, number=east).ok, "an owner nobody has texted has room for a text")
    text("o-l1", 1, mon, 11, release=False)
    r = room("o-l1", at(9, 16, 0))
    check(not r.ok and r.code == "day" and r.today == 1, "a text staged for today already holds the owner's one text a day")
    check(room("o-l1", at(9, 16, 0), exclude_batch=11).ok, "the batch being released is not counted against itself")
    text("o-l2", 1, at(6, 9, 30), 12, release=False)
    check(room("o-l2", mon).ok and room("o-l2", mon).week == 0,
          "a batch staged on an earlier day and never released does not count: it can no longer be released")
    for i, d in enumerate((3, 4, 5, 6, 7)):
        text("o-l3", i, at(d, 10, 0), 20 + i, campaign=False)
    r = room("o-l3", mon, campaign=False)
    check(not r.ok and r.code == "week" and r.week == 5, "the sixth text in seven days is refused")
    check(room("o-l3", at(10, 9, 30), campaign=False).ok, "and there is room again once the oldest of the five is a week old")
    for i, d in enumerate((2, 3, 4)):
        text("o-l4", i, at(d, 10, 0), 30 + i)
    r = room("o-l4", at(5, 10, 0))
    check(not r.ok and r.code == "platform" and r.clear_on == "2026-11-23",
          "after three campaign texts in a week the platform rests the contact, so the next one waits for day 21")
    check(room("o-l4", at(20, 10, 0)).code == "platform" and room("o-l4", at(23, 10, 0)).ok, "the rest ends on the day it says")
    check(room("o-l4", at(5, 10, 0), campaign=False).ok, "a one-off text in an open thread is not held by the campaign rest")
    check(limits.room("o-l4", "text", at(5, 10, 0), lim=limits.Limits(platform_cap=0), tz=tz, system="smarter_contact").ok,
          "platform_cap 0 switches the rest off")
    r = room("o-l4", at(23, 10, 0))
    check(r.thread == "smarter_contact:G" and room("o-l4", at(23, 10, 0), system="smrtphone").code == "thread",
          "an owner keeps the thread of the first text: a second texting system is refused")

    rec = {"uuid": "r-l5", "sms_attempts": 3}
    check(limits.observe("o-l5", rec, at(9, 8, 45), tz) == 0 and room("o-l5", mon).week == 0,
          "a text counter met for the first time is history of unknown date and is not counted")
    check(limits.observe("o-l5", dict(rec, sms_attempts=4), at(10, 8, 45), tz) == 1, "a counter that moved is a text from the other system")
    r = room("o-l5", at(10, 9, 30))
    check(r.ok and r.week == 1 and r.today == 0, "it is dated halfway between the two looks: yesterday evening, so today is still free")
    check(limits.observe("o-l5", dict(rec, sms_attempts=5), at(10, 16, 30), tz) == 1 and room("o-l5", at(10, 16, 40)).code == "day",
          "one text per owner a day counts both texting systems")
    limits.own_bump("r-l5", 6, at(11, 9, 0))
    check(limits.observe("o-l5", dict(rec, sms_attempts=6), at(11, 9, 5), tz) == 0, "the autopilot's own counter write is not read back as the other system's text")
    check(limits.observe("o-l6", {"uuid": "r-l6", "sms_attempts": 1}, mon, tz, limits.Limits(count_record_counter=False)) == 0
          and ledger.fact("r-l6", limits.SEEN_FACT) is None, "count_record_counter false leaves the record counter out of it")

    scrub.note(pacific, True)
    check(room("o-l7", at(9, 13, 0), number=pacific).code == "registry", "a number on the do-not-call registry has no room for a text")
    check(limits.room("o-l7", "call", at(9, 13, 0), policy=pol, number=pacific).code == "registry",
          "nor for a call, while calls to those numbers are held")
    check(room("o-l7", at(9, 8, 30)).code == "hours" and room("o-l7", at(14, 10, 0)).code == "hours" and room("o-l7", mon, number=central).code == "hours",
          "outside the hours, on a weekend, or too early where the number is, there is no room")
    check(room("", mon).code == "owner", "a record that names no owner cannot be counted, so it is refused")
    small = limits.Limits(text_lines=1, texts_per_line_day=2)
    used = limits.messages(day="2026-11-09", today="2026-11-09")
    check(limits.capacity_left(mon, small, tz) == max(0, 2 - used)
          and limits.room("o-l8", "text", mon, lim=small, tz=tz, system="smarter_contact").code == ("capacity" if used >= 2 else ""),
          "the day's texts stop at 25 a number times the sending numbers")
    check(limits.room("o-l8", "text", mon, lim=limits.Limits(monthly_texts=1), tz=tz, system="smarter_contact").code == "month",
          "and the month's at its cap")
    try:
        limits.room("o-l8", "mail", mon, policy=pol)
        check(False, "room answers for a text or a call and refuses anything else")
    except ValueError:
        check(True, "room answers for a text or a call and refuses anything else")

    crm = Crm([])
    crm.log_rows = [call_ev(1, "o-c1", at(9, 10, 0), "8655552101")]
    activity.sync(crm, "calls", now=at(9, 11, 0))
    call = lambda owner, when, **kw: limits.room(owner, "call", when, policy=pol, **kw)  # noqa: E731
    check(not call("o-c1", at(9, 11, 0)).ok and call("o-c1", at(9, 11, 0), number="8655552101").code == "dialed",
          "one call per owner a day: an owner dialed today is not put on another list, and a number is not dialed twice")
    check(call("o-c1", at(9, 11, 0), number="8655552102").ok, "a number of theirs nobody tried today may still be dialed: that finishes the attempt")
    check(call("o-c1", at(10, 11, 0)).ok and call("o-c2", at(9, 19, 5)).code == "hours" and call("o-c2", mon, number=pacific[:3] + "5559999").code == "hours",
          "tomorrow the owner may be called again; never after 7pm, and never before 9am where the number is")


def _nightly_checks(check, *, store, activity, limits, policy, scrub, sweep, **_) -> None:
    print("  limits: the nightly check")
    pol = policy.parse(copy.deepcopy(PLAYBOOK), EMPLOYEES)
    _wipe(store, activity, limits, scrub)
    tz = "America/New_York"
    thu = 12
    rows = []
    for i in range(26):                                   # one line carries 26 dials
        rows.append(call_ev("a%d" % i, "o-n%d" % i, at(thu, 10, i), "86555530%02d" % i, line="8655559001"))
    for i in range(11):                                   # one caller rings out from 11 numbers
        rows.append(call_ev("b%d" % i, "o-m%d" % i, at(thu, 11, i), "86555531%02d" % i, line="86555591%02d" % i, caller="Marcus"))
    rows += [call_ev("c1", "o-twice", at(thu, 10, 0), "8655553201", line="8655559201"),
             call_ev("c2", "o-twice", at(thu, 15, 0), "8655553201", line="8655559202"),      # the same number again
             call_ev("d1", "o-late", at(thu, 19, 5), "8655553301", line="8655559203"),        # after 7pm
             call_ev("e1", "o-west", at(thu, 9, 30), "3105553401", line="8655559204"),        # 6:30am there
             call_ev("f1", "o-flag", at(thu, 14, 0), "8655553501", line="8655559205")]        # a registry number
    scrub.note("8655553501", True, now=at(11, 12, 0))     # known since the day before
    scrub.note("8655553301", True, now=at(13, 9, 0))      # learned the day after it was dialed
    crm = Crm([])
    crm.log_rows = rows

    def text(owner, n, when, number="8655553601", batch=50):
        limits.stage_text(stage="t_open", batch_id=batch + n, owner_uuid=owner, record_uuid="r-" + owner,
                          numbers=[{"number": number, "thread": "smarter_contact:G", "send_at": limits._iso(when)}],
                          system="smarter_contact", series="opening", step=n + 1, touch="n-%s-%d" % (owner, n), tz=tz, campaign=False)
        limits.release_text(batch + n, "r-" + owner, [number])

    text("o-t1", 0, at(thu, 9, 30))
    text("o-t1", 1, at(thu, 16, 0))                       # two texts in one day
    for i, d in enumerate((6, 7, 8, 9, 10, 11)):
        text("o-t2", i, at(d, 10, 0), number="8655553602", batch=60)
    text("o-t2", 6, at(thu, 10, 0), number="8655553602", batch=60)       # the seventh in seven days
    text("o-t3", 0, at(thu, 11, 0), number="8655553501", batch=80)       # a registry number
    s = sweep.run("t_limits", crm=crm, now=at(thu, 19, 20), pol=pol)
    said = " | ".join(s.problems)
    check("a caller ID line carried more than 25 dials (the line ending 9001 made 26)" in said, "a line past 25 dials is named by its last four digits")
    check("a caller dialed from more than 10 numbers (Marcus used 11)" in said, "a caller past 10 numbers is named by first name")
    check("an owner's number was dialed more than once in the day (the number ending 3201, 2 times)" in said,
          "an owner's number dialed twice in a day is named")
    check("dialed outside 9am to 7pm on the business clock" in said and "dialed outside the owner's own daytime (the number ending 3401 at 6:30am its time)" in said,
          "a dial outside the hours is named, on the business clock and on the owner's own")
    check("dialed a number on the do-not-call registry (held until the attorney answers) (ending 3501)" in said,
          "a dial to a registry number is named while those calls are held, and only when the flag was already known")
    check("an owner got more than 1 text in a day" in said and "an owner got more than 5 texts in seven days" in said
          and "texted a number on the do-not-call registry (ending 3501)" in said,
          "the day's texts are checked too: two in a day, more than five in a week, a registry number")
    check(not _digits_in(s) and "Example" not in json.dumps(s.as_dict()), "no breach line carries a whole number or an owner's name")
    check(s.acted >= 8 and not crm.writes and "limit(s) were broken" in s.head and s.tally["dials checked"] == len(rows),
          "the check counts what it found, reads only, and says so in one line")
    week = limits.week_counts("2026-11-12")
    check(week.get("a caller dialed from more than 10 numbers") == 1 and any("Limits 11-12" in ln for ln in limits.recent_lines(now=at(13, 8, 0))),
          "each day's result is kept, so a week by caller and by number is one read away, and the digest has its lines")
    quiet = Crm([])
    quiet.log_rows = [call_ev("q1", "o-q1", at(13, 10, 0), "8655553701", line="8655559301")]
    s2 = sweep.run("t_limits", crm=quiet, now=at(13, 19, 20), pol=pol)
    check(s2.acted == 0 and not s2.problems and "Every limit held" in s2.head, "a day inside every limit says so and raises nothing")
    s3 = sweep.run("t_limits", crm=quiet, now=at(16, 7, 0), pol=pol)
    check("Nov 15" in s3.head, "a morning run checks the day before")
    off = copy.deepcopy(PLAYBOOK)
    off["stages"]["t_scrub"]["calls"] = "allow"
    found, _totals = limits.day_breaches(at(thu, 0, 0), at(13, 0, 0), limits.Limits(), tz, policy.parse(off, EMPLOYEES))
    check(not any("registry (held" in b["label"] for b in found), "once calls to registry numbers are allowed, a dial to one is no longer a breach")


def _opening_checks(check, *, store, activity, ledger, lanes, limits, policy, scrub, sweep, textlane, screened, **_) -> None:
    print("  text lane: the opening texts")
    pol = policy.parse(copy.deepcopy(PLAYBOOK), EMPLOYEES)
    pol_plan = policy.parse(_with("t_open", mode="plan"), EMPLOYEES)
    _wipe(store, activity, limits, scrub)
    mon = at(9)                                          # Monday 8:45am, before the sending window opens
    phones = [mob(2101), mob(2102, "Dial Second"), dict(mob(2103), type="LANDLINE"), mob(2109, "Drop")]
    crm = _world([("a1", "11 Text St", at(9, 7, 0), phones, {})], screened)

    # --- plan mode: the load is listed and nothing is written anywhere
    s = sweep.run("t_open", crm=crm, now=mon, pol=pol_plan)
    check(s.acted == 1 and not s.live and "Would stage 2 text(s) to 1 owner(s), about $0.04: 1 on text 1" in s.head,
          "in plan mode the stage says what it would load: who, how many texts, what it costs")
    check(any("11 Text St: text 1 to 2 number(s) ending 2101, 2102" in ln for ln in s.lines) and not _digits_in(s),
          "a line shows the street and the last four digits, never a number in full")
    check(not crm.writes and not ledger.batches() and not limits.rows() and not s.batch and abs(s.would_usd - 0.04) < 1e-9,
          "a plan run stages nothing, counts nothing as sent and writes nothing to the account")

    # --- live mode: a batch is staged for a person, and still nothing is sent
    s = sweep.run("t_open", crm=crm, now=mon, pol=pol)
    b = ledger.batch(s.batch) if s.batch else None
    it = (b or {"items": [{}]})["items"][0]
    check(bool(b) and b["kind"] == "text" and b["status"] == "staged" and len(b["items"]) == 1 and s.live
          and "Staged batch %d" % s.batch in s.head and "release %d --commit" % s.batch in s.head,
          "in live mode the stage stages a text batch and tells the person how to release it")
    check(not crm.writes and not textlane.load_path(s.batch).exists() and len(limits.rows(batch_id=s.batch, statuses=("staged",))) == 2
          and not limits.rows(statuses=("released",)),
          "nothing is ever sent by the stage: no write to the account, no load file, no text counted as out")
    nums = it.get("numbers") or [{}]
    check([n["number"] for n in nums] == ["8655552101", "8655552102"] and it["step"] == 1 and it["series"] == "opening",
          "a text goes to mobile numbers only, the best tier first: never the landline, never the Drop number")
    check(all(n["group"] == n["cohort"] == "FTM 1109 Named East" and n["thread"] == "smarter_contact:FTM 1109 Named East"
              and n["new_contact"] for n in nums), "text 1 puts each number in the day's group, and that group is its thread key")
    check(it["template_set"] == "p1" and it["template_touch"] == 1 and it["audience"] == "named" and it["values"]["SIFT_RECORD"] == "a1"
          and it["values"]["ADDRESS"] == "11 Text St" and nums[0]["send_at"] == limits._iso(at(9, 9, 15)),
          "the item carries the template touch, the contact fields and a send time inside the window")
    s2 = sweep.run("t_open", crm=crm, now=at(9, 9, 30), pol=pol)
    check(s2.tally["already staged, waiting on release"] == 1 and len(ledger.batches()) == 1 and not s2.batch,
          "a record already in a staged batch is not staged a second time")

    # --- release: dry first, then for real
    bid = s.batch
    dry = textlane.release(bid, crm=crm, pol=pol, commit=False, now=at(9, 9, 0))
    check(dry == {"batch": bid, "dry": True, "owners": 1, "texts": 2, "of": 1, "would_hold": {}} and not crm.writes
          and ledger.batch(bid)["status"] == "staged", "a dry release checks every owner again and writes nothing")
    out = textlane.release(bid, crm=crm, pol=pol, commit=True, now=at(9, 9, 0))
    load = json.loads(Path(out["load"]).read_text(encoding="utf-8")) if out.get("load") else {}
    check(out["owners"] == 1 and out["texts"] == 2 and crm.writes == [("sms_attempts", "a1", 1)] and ledger.batch(bid)["status"] == "sent",
          "a release hands the load over and raises the record's text counter once, however many numbers got the text")
    ch = ledger.changes(run_id="release-%d" % bid)
    check(len(ch) == 1 and ch[0]["what"] == "sms_attempts" and ch[0]["before"] == 0 and ch[0]["after"] == 1,
          "the counter change is recorded with the value it replaced")
    check(len(limits.rows(batch_id=bid, statuses=("released",))) == 2 and abs(limits.spent("2026-11-09") - 0.04) < 1e-9
          and len(ledger.events("a1", "text")) == 1 and ledger.events("a1", "text")[0]["usd"] == 0,
          "the text is counted in the text ledger, and its cost stays off the data spend ledger")
    check(load.get("groups", [{}])[0].get("name") == "FTM 1109 Named East" and load["groups"][0]["touch"] == 1
          and load["groups"][0]["tz"] == "east" and load["groups"][0]["contacts"] == 2
          and sorted(c["phone"] for c in load["contacts"]) == ["8655552101", "8655552102"] and "not sent" in out["next"],
          "the load file names the group to create and the contacts to put in it, and the release says it did not send")
    try:
        import smarter_contact_land as land
        keys = {k: "f-" + k.lower() for k in ("ADDRESS", "CITY", "STATE", "ZIP", "MAILING_ADDRESS", "MAILING_CITY",
                                               "MAILING_STATE", "MAILING_ZIP", "SIFT_RECORD")}
        rec = crm.recs["a1"]
        same = all(textlane.contact_input(c, keys) == land.contact_input(rec, {}, c["phone"], keys) for c in load["contacts"])
        rows_ok = [r["group"] for r in textlane.plan_rows(load, keys)] == ["FTM 1109 Named East"] * 2
        check(same and rows_ok, "a load contact is exactly the contact the existing loader builds for the same record")
    except ImportError as exc:
        check(False, "the existing loader could not be imported to compare the contact shape (%s)" % exc)
    try:
        textlane.release(bid, crm=crm, pol=pol, commit=True, now=at(9, 9, 5))
        check(False, "a batch that was released cannot be released again")
    except SystemExit:
        check(True, "a batch that was released cannot be released again")

    # --- the next two days, then the table closes the series
    crm.recs["a1"]["predictivecall_attempts"] = 1           # dialed once on Monday: the record moved to Call Attempt 1
    s = sweep.run("t_open", crm=crm, now=at(10), pol=pol)
    it = ledger.batch(s.batch)["items"][0]
    check(it["step"] == 2 and it["template_touch"] == 2 and all(n["group"] == "FTM 1109 Named East T2" and not n["new_contact"]
                                                              and n["thread"] == "smarter_contact:FTM 1109 Named East" for n in it["numbers"]),
          "text 2 goes out the next day in a new group built off the cohort, on the same thread")
    textlane.release(s.batch, crm=crm, pol=pol, commit=True, now=at(10, 9, 0))
    s = sweep.run("t_open", crm=crm, now=at(11), pol=pol)
    it = ledger.batch(s.batch)["items"][0]
    textlane.release(s.batch, crm=crm, pol=pol, commit=True, now=at(11, 9, 0))
    check(it["step"] == 3 and it["numbers"][0]["group"] == "FTM 1109 Named East T3" and crm.recs["a1"]["sms_attempts"] == 3,
          "text 3 goes out on day 2, and the record's counter reads three")
    s = sweep.run("t_open", crm=crm, now=at(12), pol=pol)
    check(not s.batch and s.tally["opening texts closed for the record (past day 2 of the lane)"] == 1
          and "No owner in the ftm lane is due an opening text" in s.head, "on day 3 the table has closed the opening texts")
    before = (crm.reads, crm.log_reads)
    sweep.run("t_open", crm=crm, now=at(13), pol=pol)
    check((crm.reads, crm.log_reads) == before, "a record whose series is over is remembered and never read again")
    short = copy.deepcopy(PLAYBOOK)
    short["lanes"]["ftm"]["timeline"] = {"opening_texts": [0, 1]}
    crm_t = _world([("a2", "12 Text St", at(9, 7, 0), [mob(2111)], {})], screened)
    s = sweep.run("t_open", crm=crm_t, now=at(11), pol=policy.parse(short, EMPLOYEES))
    check(not s.batch and s.tally["opening texts closed for the record (past day 1 of the lane)"] == 1,
          "the days come from the lanes table: shorten the table and the stage closes a day sooner")
    s = sweep.run("t_open", crm=crm_t, now=at(14, 10, 0), pol=pol)
    check("Saturday is not a sending day" in s.head and not s.batch, "nothing is loaded on a day that is not a sending day")

    # --- a sequence, never a later text first
    _wipe(store, activity, limits, scrub)
    crm = Crm([_rec("b1", "13 Text St", phones=[mob(2121)]), _rec("b2", "14 Text St", phones=[mob(2122)])])
    crm.land("b1", at(9, 7, 0))
    crm.land("b2", at(9, 7, 0))
    ledger.set_fact("b2", "death_screen_plan_hit", {"hit": "index", "detail": "x"})
    s = sweep.run("t_open", crm=crm, now=mon, pol=pol)
    check(not s.batch and s.tally["waiting on the death screen"] == 1 and s.tally["possible death: waiting on research"] == 1 and crm.reads == 0,
          "a record the death screen has not seen is not texted, and is not even opened")
    screened("b1")
    s = sweep.run("t_open", crm=crm, now=at(10), pol=pol)
    it = ledger.batch(s.batch)["items"][0]
    check(it["record_uuid"] == "b1" and it["step"] == 1 and it["numbers"][0]["group"] == "FTM 1110 Named East",
          "a record first eligible on day 1 gets text 1 that day, never text 2 first")
    textlane.release(s.batch, crm=crm, pol=pol, commit=True, now=at(10, 9, 0))
    s = sweep.run("t_open", crm=crm, now=at(11), pol=pol)
    textlane.release(s.batch, crm=crm, pol=pol, commit=True, now=at(11, 9, 0))
    s = sweep.run("t_open", crm=crm, now=at(12), pol=pol)
    steps = sorted(t["step"] for t in limits.touches("o-b1", "2026-11-12"))
    check(steps == [1, 2] and not s.batch, "and its series still closes on the table's day, wherever the record is in it")

    # --- a text a day behind its cohort
    _wipe(store, activity, limits, scrub)
    crm = _world([("c1", "15 Text St", at(9, 7, 0), [mob(2131)], {})], screened)
    s = sweep.run("t_open", crm=crm, now=mon, pol=pol)
    textlane.release(s.batch, crm=crm, pol=pol, commit=True, now=at(9, 9, 0))
    s = sweep.run("t_open", crm=crm, now=at(10), pol=policy.parse(_with("t_open", daily_owners=0), EMPLOYEES))
    check(not s.batch and s.tally["left for tomorrow (daily cap 0 owners)"] == 1, "a daily cap leaves the rest for tomorrow")
    s = sweep.run("t_open", crm=crm, now=at(11), pol=pol)
    it = ledger.batch(s.batch)["items"][0]
    check(it["step"] == 2 and it["numbers"][0]["group"] == "FTM 1109 Named East T2 Late 1111",
          "a text that goes a day behind its cohort gets its own Late group, so a sending campaign never gains a contact")

    # --- who is never loaded
    _wipe(store, activity, limits, scrub)
    scrub.note("8655552204", True)                         # flagged on some other record's row, on an earlier day
    t0 = at(9, 7, 0)
    specs = [
        ("g_ok", "20 Gate St", t0, [mob(2205)], {}),
        ("g_lead", "21 Gate St", t0, [mob(2211)], {"status": "Hot Lead"}),
        ("g_dead", "22 Gate St", t0, [mob(2212)], {"deceased": True}),
        ("g_obit", "23 Gate St", t0, [mob(2213)], {"lists": ["Obituary"]}),
        ("g_lit", "24 Gate St", t0, [dict(mob(2214), tags=["Dial First", "Litigator"])], {}),
        ("g_dnm", "25 Gate St", t0, [mob(2215)], {"tags": ["FTM", "Do Not Market"]}),
        ("g_sold", "26 Gate St", t0, [mob(2216)], {"tags": ["FTM", "recently sold"]}),
        ("g_list", "27 Gate St", t0, [mob(2217)], {"mls": "Listed"}),
        ("g_mail", "28 Gate St", t0, [mob(2218)], {"tags": ["FTM", "Mail Only"]}),
        ("g_dnc", "29 Gate St", t0, [mob(2219)], {"lists": ["DNC"]}),
        ("g_reply", "30 Gate St", t0, [mob(2220, status="CORRECT")], {}),
        ("g_land", "31 Gate St", t0, [dict(mob(2221), type="LANDLINE")], {}),
        ("g_flag", "32 Gate St", t0, [mob(2203)], {"row_phone": {"number": "8655552203", "doNotCall": True}}),
        ("g_mem", "33 Gate St", t0, [mob(2204)], {}),
        ("g_two", "34 Gate St", t0, [mob(2222)], {}),
    ]
    crm = _world(specs, screened)
    lanes.set_owner_lane("o-g_two", "lead", {"elsewhere": "lead"})
    s = sweep.run("t_open", crm=crm, now=mon, pol=pol)
    items = ledger.batch(s.batch)["items"] if s.batch else []
    check([i["record_uuid"] for i in items] == ["g_ok"], "of fifteen records only the one clean owner is loaded")
    t = s.tally
    check(t["held back: already a lead (hot lead)"] == 1 and crm.reads == len(specs) - 1,
          "a lead is never loaded, and is not even opened")
    check(t["held back: the owner has died or may have (owner flag); research first"] == 1
          and t["held back: the owner has died or may have (Obituary list); research first"] == 1,
          "a dead owner, and an owner who may have died, are never loaded")
    check(t["held back: a litigator's number is on the record"] == 1 and t["held back: tagged Do Not Market"] == 1
          and t["held back: the owner opted out (DNC list)"] == 1, "a litigator, Do Not Market and an opt-out are never loaded")
    check(t["held back: the house sold"] == 1 and t["held back: listed on the MLS"] == 1
          and t["held back: mail only: the owner is not to be called or texted"] == 1,
          "a sold house, a listed house and a mail-only record are never loaded")
    check(t["held back: the owner is in the lead lane on another record"] == 1, "an owner who is a lead on another record is never loaded here")
    check(t["the owner already answered: a person has the thread"] == 1, "an owner who already answered is left to a person")
    check(t["held back: no usable number for a text"] == 1, "a record with only a landline has nothing to text")
    check(t["no number left to text (do-not-call registry)"] == 2 and scrub.is_flagged("8655552203"),
          "a flagged number is never loaded: the record whose row shows the flag is counted as the registry's doing, and the flag is remembered")
    check(t["numbers left out: on the do-not-call registry"] == 1,
          "a number flagged on another record's row is never loaded here either, where no row shows the flag")
    flat = json.dumps(items)
    check(not any(n in flat for n in ("8655552203", "8655552204", "8655552211", "8655552220")),
          "no flagged number, no lead's number and no answered owner's number is anywhere in the batch")
    s = sweep.run("t_open", crm=_world([specs[10]], screened), now=mon, pol=policy.parse(_with("t_open", stop_on_correct=False), EMPLOYEES))
    check(bool(s.batch), "with stop_on_correct off, a confirmed number is texted like any other usable one")

    # --- limits, groups and caps inside the stage
    _wipe(store, activity, limits, scrub)
    specs = [("h_week", "40 Room St", t0, [mob(2301)], {}), ("h_today", "41 Room St", t0, [mob(2302)], {}),
             ("h_init", "42 Room St", t0, [mob(2303)], {"first": "E A"}),
             ("h_cst", "43 Room St", t0, [dict(mob(2304), number="6155552304")], {}),
             ("h_ak", "44 Room St", t0, [dict(mob(2305), number="9075552305")], {}),
             ("h_co", "45 Room St", t0, [mob(2306)], {"first": "", "last": "", "company": "Example Holdings LLC"})]
    crm = _world(specs, screened)
    for i, d in enumerate((3, 4, 5, 6, 7)):
        limits.stage_text(stage="t_open", batch_id=900 + i, owner_uuid="o-h_week", record_uuid="h_week",
                          numbers=[{"number": "8655552301", "thread": "smarter_contact:G", "send_at": limits._iso(at(d, 10, 0))}],
                          system="smarter_contact", series="afternoon", step=i, touch="w%d" % i, tz="America/New_York", campaign=False)
        limits.release_text(900 + i, "h_week", ["8655552301"])
    limits.observe("o-h_today", {"uuid": "h_today", "sms_attempts": 0}, at(9, 7, 0), "America/New_York")
    limits.observe("o-h_today", {"uuid": "h_today", "sms_attempts": 1}, at(9, 8, 0), "America/New_York")
    s = sweep.run("t_open", crm=crm, now=mon, pol=pol)
    by = {i["record_uuid"]: i for i in (ledger.batch(s.batch)["items"] if s.batch else [])}
    check(s.tally["no room: the owner's texts for the week are used"] == 1 and "h_week" not in by,
          "the sixth text in a week is refused by the stage: the owner is not loaded")
    check(s.tally["no room: the owner already has a text today"] == 1 and "h_today" not in by,
          "an owner the other texting system texted today is not loaded")
    check(by["h_init"]["audience"] == "noname" and by["h_init"]["numbers"][0]["group"] == "FTM 1109 No Name East"
          and by["h_co"]["audience"] == "noname" and by["h_co"]["first_name"] == "" and by["h_co"]["last_name"] == "Example Holdings LLC",
          "an owner known by initials, and a company, go to the no-name group")
    check(by["h_cst"]["numbers"][0]["group"] == "FTM 1109 Named Other TZ" and by["h_cst"]["numbers"][0]["send_at"] == limits._iso(at(9, 12, 0)),
          "a Central number goes in the late group, which starts at noon on the business clock")
    check("h_ak" not in by and s.tally["number left out: outside the owner's daytime"] == 1,
          "a number whose own morning has not started by noon is left out")
    _wipe(store, activity, limits, scrub)
    three = [("i%d" % i, "5%d Cap St" % i, t0, [mob(2400 + i)], {}) for i in range(3)]
    crm = _world(three, screened)
    s = sweep.run("t_open", crm=crm, now=mon, pol=policy.parse(_with("t_limits", text_lines=1, texts_per_line_day=2), EMPLOYEES))
    check(len(ledger.batch(s.batch)["items"]) == 2 and s.tally["left for tomorrow (today's texts are used)"] == 1 and crm.reads == 2,
          "the day's texts stop at what the sending numbers carry, and the stage stops reading when it can load no more")
    _wipe(store, activity, limits, scrub)
    crm = _world(three, screened)
    s = sweep.run("t_open", crm=crm, now=mon, pol=policy.parse(_with("t_open", daily_usd=0.03), EMPLOYEES))
    check(len(ledger.batch(s.batch)["items"]) == 1 and s.tally["left for tomorrow (stage cap $0.03 a day)"] == 2,
          "the stage's own daily cap holds the rest for tomorrow")

    # --- one owner, two records: one text
    _wipe(store, activity, limits, scrub)
    twins = [("w1", "95 Twin St", t0, [mob(2901)], {}), ("w2", "96 Twin St", t0, [mob(2901)], {})]
    crm = _world(twins, screened)
    crm.recs["w2"]["owner"]["uuid"] = "o-w1"
    s = sweep.run("t_open", crm=crm, now=mon, pol=pol)
    check(len(ledger.batch(s.batch)["items"]) == 1 and s.tally["the owner's other record is already in today's load"] == 1 and crm.reads == 1,
          "an owner with two records in the lane gets one text, and the second record is not opened")

    class Bare(Crm):
        def _row(self, r):
            return dict(super()._row(r), owner={})          # a search row that does not name the owner

    _wipe(store, activity, limits, scrub)
    crm = Bare([_rec(u, street, phones=phones) for u, street, _, phones, _ in twins])
    crm.land("w1", t0)
    crm.land("w2", t0)
    crm.recs["w2"]["owner"]["uuid"] = "o-w1"
    screened("w1", "w2")
    s = sweep.run("t_open", crm=crm, now=mon, pol=pol)
    check(len(ledger.batch(s.batch)["items"]) == 1 and s.tally["the owner's other record is already in today's load"] == 1,
          "and still one text when the search rows do not name the owner")

    _wipe(store, activity, limits, scrub)
    crm = _world([("x1", "97 Share St", t0, [mob(2911)], {}), ("x2", "98 Share St", t0, [mob(2911), mob(2912, "Dial Second")], {}),
                  ("x3", "99 Share St", t0, [mob(2911)], {})], screened)
    s = sweep.run("t_open", crm=crm, now=mon, pol=pol)
    by = {i["record_uuid"]: [n["number"] for n in i["numbers"]] for i in ledger.batch(s.batch)["items"]}
    check(by == {"x1": ["8655552911"], "x2": ["8655552912"]} and s.tally["numbers left out: already in today's texts for another owner"] == 2,
          "a number two owners share gets one text a day, under the first owner: one phone is one contact")

    # --- the playbook blocks are checked before a stage ever runs
    wrong = _unchecked(pol, "t_open", series="follow-up", numbers_per_owner=0, text_usd="cheap").stage("t_open")
    try:
        policy.parse(_with("t_open", series="follow-up", numbers_per_owner=0, text_usd="cheap"), EMPLOYEES)
        refused = ""
    except policy.PolicyError as exc:
        refused = str(exc)
    check(all(w in refused for w in ("series", "numbers_per_owner", "text_usd")),
          "a text stage's block with a typo is refused when the playbook loads")
    check(len(textlane.block_problems(wrong)) == 3 and textlane.block_problems(pol.stage("t_open")) == []
          and textlane.block_problems(pol.stage("t_follow")) == [], "a text stage's block is checked: its series, its numbers, its price")
    bare = _unchecked(pol, "t_open", presets=[])
    s = sweep.run("t_open", crm=crm, now=mon, pol=bare)
    check(len(textlane.block_problems(bare.stage("t_open"))) == 1 and any("need the presets" in p for p in s.problems) and not s.batch,
          "the opening texts need the presets that hold new records, and say so")
    wrong = _unchecked(pol, "t_limits", text_hours="late", lines_per_caller=-1).stage("t_limits")
    check(len(limits.block_problems(wrong)) == 2 and limits.block_problems(pol.stage("t_limits")) == [], "a limits block is checked the same way")
    maybe = _unchecked(pol, "t_scrub", calls="maybe")
    check(len(scrub.block_problems(maybe.stage("t_scrub"))) == 1 and scrub.settings(maybe).calls == "hold" and scrub.block_problems(pol.stage("t_scrub")) == [],
          "a calls setting that cannot be read is reported, and means hold")

    # --- a backlog cannot use up the run, and an old record is closed without being opened
    _wipe(store, activity, limits, scrub)
    old = [("j%02d" % i, "6%d Old St" % i, at(3, 7, 0, month=8), [mob(2500 + i)], {}) for i in range(12)]
    crm = _world(old, screened)
    cap5 = policy.parse(_with("t_open", max_new=5), EMPLOYEES)
    s = sweep.run("t_open", crm=crm, now=mon, pol=cap5)
    check(crm.log_reads == 5 and crm.reads == 0 and s.tally["not looked at yet (read cap, next run)"] == 7,
          "a run looks at a set number of new records and opens none whose opening days are long past")
    sweep.run("t_open", crm=crm, now=at(9, 9, 0), pol=cap5)
    sweep.run("t_open", crm=crm, now=at(9, 9, 30), pol=cap5)
    sweep.run("t_open", crm=crm, now=at(9, 10, 0), pol=cap5)
    check(crm.log_reads == 12 and crm.reads == 0, "after a few runs the backlog is known and is never read again")
    _wipe(store, activity, limits, scrub)
    crm = _world([("k_old", "70 Log St", None, [mob(2601)], {}), ("k_new", "71 Log St", at(10, 7, 0), [mob(2602)], {})], screened)
    one = policy.parse(_with("t_open", max_new=1), EMPLOYEES)
    crm.recs["k_new"]["tags"] = []                          # not in the lane yet on Monday
    s = sweep.run("t_open", crm=crm, now=mon, pol=one)
    check(s.tally["lane entry date not known yet (the log is asked again tomorrow)"] == 1 and crm.log_reads == 1,
          "a record whose log does not show when it entered the lane is not guessed at")
    sweep.run("t_open", crm=crm, now=at(9, 12, 0), pol=one)
    check(crm.log_reads == 1, "and its log is not asked again the same day")
    crm.recs["k_new"]["tags"] = ["FTM"]
    s = sweep.run("t_open", crm=crm, now=at(10), pol=one)
    check(bool(s.batch) and ledger.batch(s.batch)["items"][0]["record_uuid"] == "k_new" and s.tally["not looked at yet (read cap, next run)"] == 1,
          "today's new record is looked at before a record the log could not place yesterday")
    sweep.run("t_open", crm=crm, now=at(11), pol=pol)
    s = sweep.run("t_open", crm=crm, now=at(12), pol=pol)
    check(s.tally["opening texts closed for the record (in the lane longer than the log reaches)"] == 1,
          "after three days of no answer from the log the record is taken as old and closed")

    # --- a read that fails, and the hour's request budget running out
    from .autopilot import budget

    class Flaky(Crm):
        def record_logs(self, u, event_types=None, limit=250):
            if u == "y1":
                self.log_reads += 1
                raise RuntimeError("the log route answered 502")
            return super().record_logs(u, event_types, limit)

    _wipe(store, activity, limits, scrub)
    crm = Flaky([_rec("y1", "72 Flaky St", phones=[mob(2611)]), _rec("y2", "73 Flaky St", phones=[mob(2612)])])
    crm.land("y1", t0)
    crm.land("y2", t0)
    screened("y1", "y2")
    s = sweep.run("t_open", crm=crm, now=mon, pol=pol)
    check(s.tally["lane entry date not read (the log did not answer)"] == 1 and ledger.fact("y1", textlane.UNKNOWN_FACT % "ftm") is None
          and [i["record_uuid"] for i in ledger.batch(s.batch)["items"]] == ["y2"],
          "a log read that fails proves nothing about the record: it is not counted against it, and the rest of the load goes on")

    class Tired(Crm):
        def record(self, u):
            if self.reads >= 1:
                raise budget.BudgetExhausted("the brain app has used its 2800 DataSift requests for this hour")
            return super().record(u)

    _wipe(store, activity, limits, scrub)
    crm = Tired([_rec(u, street, phones=phones) for u, street, _, phones, _ in three])
    for u, _, _, _, _ in three:
        crm.land(u, t0)
    screened(*[x[0] for x in three])
    s = sweep.run("t_open", crm=crm, now=mon, pol=pol)
    check(bool(s.batch) and len(ledger.batch(s.batch)["items"]) == 1 and any("stopped reading early" in p for p in s.problems),
          "when the hour's request budget runs out mid-run, what was already built is still staged and the stop is said")

    # --- a batch is released on its day or not at all
    _wipe(store, activity, limits, scrub)
    crm = _world([("m1", "80 Stale St", at(9, 7, 0), [mob(2701)], {})], screened)
    s = sweep.run("t_open", crm=crm, now=mon, pol=pol)
    stale = s.batch
    try:
        textlane.release(stale, crm=crm, pol=pol, commit=True, now=at(10, 9, 0))
        check(False, "a batch staged for Monday is refused on Tuesday")
    except SystemExit as exc:
        check("staged for 2026-11-09" in str(exc) and not crm.writes, "a batch staged for Monday is refused on Tuesday")
    s = sweep.run("t_open", crm=crm, now=at(10), pol=pol)
    check(any("batch %d" % stale in p and "never released" in p for p in s.problems) and ledger.batch(stale)["status"] == "expired"
          and not limits.rows(batch_id=stale),
          "the next run says the batch was never released, closes it, and stops counting its texts")
    it = ledger.batch(s.batch)["items"][0]
    check(it["record_uuid"] == "m1" and it["step"] == 1 and it["numbers"][0]["group"] == "FTM 1110 Named East",
          "and the owner is picked up again from text 1, because text 1 never went out")
    if hasattr(lanes, "tranche_members"):
        # A lane worked in tranches (plan 4.1, another track's rule inside stage_rows): the text
        # stage must load the current tranche and nothing behind it.
        _wipe(store, activity, limits, scrub)
        crm = _world([("n1", "90 Set St", at(9, 7, 0), [mob(2801)], {}), ("n2", "91 Set St", at(9, 7, 0), [mob(2802)], {})], screened)
        s = sweep.run("t_open", crm=crm, now=mon, pol=policy.parse(_with("t_open", tranche=1), EMPLOYEES))
        check(bool(s.batch) and len(ledger.batch(s.batch)["items"]) == 1 and len(lanes.tranche_members("ftm")) == 1,
              "in a lane worked in tranches the stage loads the current tranche and nothing behind it")


def _release_checks(check, *, store, activity, ledger, limits, policy, scrub, sweep, textlane, screened, **_) -> None:
    print("  text lane: the release")
    import re
    pol = policy.parse(copy.deepcopy(PLAYBOOK), EMPLOYEES)
    _wipe(store, activity, limits, scrub)
    mon, t0 = at(9), at(9, 7, 0)
    specs = [("l_ok", "100 Rel St", t0, [mob(3001)], {}),
             ("l_out", "101 Rel St", t0, [mob(3002)], {}),
             ("l_reply", "102 Rel St", t0, [mob(3003)], {}),
             ("l_wrong", "103 Rel St", t0, [mob(3004), mob(3005, "Dial Second")], {}),
             ("l_flag", "104 Rel St", t0, [mob(3006)], {}),
             ("l_lead", "105 Rel St", t0, [mob(3007)], {}),
             ("l_dead", "106 Rel St", t0, [mob(3008)], {}),
             ("l_other", "107 Rel St", t0, [mob(3009)], {})]
    crm = _world(specs, screened)
    s = sweep.run("t_open", crm=crm, now=mon, pol=pol)
    bid = s.batch
    check(len(ledger.batch(bid)["items"]) == 8 and len(limits.rows(batch_id=bid, statuses=("staged",))) == 9,
          "eight clean owners are staged on Monday morning, nine texts in all")
    # What changes between the staging and the release:
    crm.recs["l_out"]["tags"].append("Do Not Market")                                  # opted out
    crm.recs["l_reply"]["owner"]["phones"][0]["status"] = "CORRECT"                    # answered
    crm.recs["l_wrong"]["owner"]["phones"][0]["status"] = "WRONG"                      # one number is somebody else's
    crm.recs["l_flag"]["row_phone"] = {"number": "8655553006", "doNotCall": True}      # the registry flag appeared
    crm.recs["l_lead"]["status"] = "new_lead"                                          # became a lead
    crm.recs["l_dead"]["owner"]["deceased"] = True                                     # died
    crm.recs["l_other"]["sms_attempts"] = 1                                            # the other texting system texted them
    dry = textlane.release(bid, crm=crm, pol=pol, commit=False, now=at(9, 11, 0))
    hold = dry["would_hold"]
    check(dry["owners"] == 2 and dry["texts"] == 2 and dry["of"] == 8 and not crm.writes,
          "the release runs every check again: two of the eight owners still pass")
    check(hold.get("tagged Do Not Market") == 1 and hold.get("already a lead (new_lead)") == 1
          and hold.get("the owner has died or may have (owner flag); research first") == 1,
          "an owner who opted out, became a lead or died after the batch was staged gets nothing")
    check(hold.get("the owner answered since the batch was staged: a person has the thread") == 1,
          "texting stops the moment the owner answers: a number marked CORRECT ends the series")
    check(hold.get("no usable number for a text") == 1 and hold.get("the owner already has a text today") == 1,
          "a number the registry flagged since, and an owner the other texting system reached first, are held too")
    out = textlane.release(bid, crm=crm, pol=pol, commit=True, now=at(9, 11, 0))
    load = json.loads(Path(out["load"]).read_text(encoding="utf-8"))
    check(crm.writes == [("sms_attempts", "l_ok", 1), ("sms_attempts", "l_wrong", 1)] and out["owners"] == 2,
          "only the owners who passed have their text counter raised")
    check([c["phone"] for c in load["contacts"]] == ["8655553001", "8655553005"],
          "the load holds only what passed: the number marked wrong since is gone, the owner's other number stays")
    check(len(load["held"]) == 6 and all(h.get("why") and h.get("street") for h in load["held"])
          and not re.search(r"\d{10}", json.dumps(load["held"])), "who was held back is listed with the reason and no phone number")
    check(len(limits.rows(batch_id=bid, statuses=("released",))) == 2 and len(limits.rows(batch_id=bid, statuses=("dropped",))) == 7
          and not limits.rows(batch_id=bid, statuses=("staged",)) and ledger.batch(bid)["status"] == "sent",
          "the ledger counts the two texts that went and stops counting the seven that did not")
    check(limits.room("o-l_out", "text", at(10, 9, 30), policy=pol, system="smarter_contact").today == 0
          and limits.touches("o-l_out", "2026-11-10") == [], "a held owner is not counted as texted")

    _wipe(store, activity, limits, scrub)
    crm = _world([("l_all", "110 Rel St", t0, [mob(3011)], {})], screened)
    s = sweep.run("t_open", crm=crm, now=mon, pol=pol)
    crm.recs["l_all"]["tags"].append("Do Not Market")
    out = textlane.release(s.batch, crm=crm, pol=pol, commit=True, now=at(9, 9, 0))
    check(out["owners"] == 0 and out["load"] == "" and ledger.batch(s.batch)["status"] == "held" and not crm.writes
          and "nothing was handed over" in out["next"], "when nobody passes, nothing is handed over and nothing is written")

    class Stuck(Crm):
        def sms_attempts(self, u, total):
            self.writes.append(("sms_attempts", u, total))          # accepted by the route, never stored

    _wipe(store, activity, limits, scrub)
    crm = Stuck([_rec("l_s1", "111 Rel St", phones=[mob(3021)]), _rec("l_s2", "112 Rel St", phones=[mob(3022)])])
    crm.land("l_s1", t0)
    crm.land("l_s2", t0)
    screened("l_s1", "l_s2")
    s = sweep.run("t_open", crm=crm, now=mon, pol=pol)
    out = textlane.release(s.batch, crm=crm, pol=pol, commit=True, now=at(9, 9, 0))
    check(any("did not stick" in p for p in out["problems"]) and len(crm.writes) == 1
          and len(limits.rows(batch_id=s.batch, statuses=("released",))) == 2,
          "a text counter that does not stick is said once, the rest of the batch skips it, and the ledger still counts both texts")

    class Refused(Crm):
        def sms_attempts(self, u, total):
            raise RuntimeError("403 on the counter route")

    _wipe(store, activity, limits, scrub)
    crm = Refused([_rec("l_r1", "118 Rel St", phones=[mob(3061)]), _rec("l_r2", "119 Rel St", phones=[mob(3062)])])
    crm.land("l_r1", t0)
    crm.land("l_r2", t0)
    screened("l_r1", "l_r2")
    s = sweep.run("t_open", crm=crm, now=mon, pol=pol)
    out = textlane.release(s.batch, crm=crm, pol=pol, commit=True, now=at(9, 9, 0))
    check(out["owners"] == 2 and any("could not be written" in p for p in out["problems"]) and Path(out["load"]).exists()
          and len(limits.rows(batch_id=s.batch, statuses=("released",))) == 2 and ledger.batch(s.batch)["status"] == "sent",
          "a counter route that refuses does not lose the release: the load is handed over and both texts are counted")

    _wipe(store, activity, limits, scrub)
    crm = _world([("l_late", "113 Rel St", t0, [mob(3031)], {})], screened)
    s = sweep.run("t_open", crm=crm, now=mon, pol=pol)
    out = textlane.release(s.batch, crm=crm, pol=pol, commit=False, now=at(9, 18, 30))
    check(out["owners"] == 0 and out["would_hold"] == {"the sending window has closed for today": 1},
          "a batch released after 6pm hands nothing over: the window has closed")
    _wipe(store, activity, limits, scrub)
    tight = policy.parse(_with("t_limits", text_lines=1, texts_per_line_day=2), EMPLOYEES)
    crm = _world([("l_c1", "114 Rel St", t0, [mob(3041)], {}), ("l_c2", "115 Rel St", t0, [mob(3042)], {})], screened)
    s = sweep.run("t_open", crm=crm, now=mon, pol=tight)
    out = textlane.release(s.batch, crm=crm, pol=tight, commit=True, now=at(9, 9, 0))
    check(out["owners"] == 2 and out["texts"] == 2 and limits.capacity_left(at(9, 9, 5), limits.settings(tight), "America/New_York") == 0,
          "a batch that fills the day is released in full: it is not counted against itself, and then the day is used")
    _wipe(store, activity, limits, scrub)
    crm = _world([("l_p1", "116 Rel St", t0, [mob(3051)], {}), ("l_p2", "117 Rel St", t0, [mob(3052)], {})], screened)
    s = sweep.run("t_open", crm=crm, now=mon, pol=pol)
    check(textlane.pull(s.batch, {"l_p2"}) == 1 and len(ledger.batch(s.batch)["items"]) == 2,
          "the revocation stage can ask what it would pull from a staged text batch without changing it")
    check(textlane.pull(s.batch, {"l_p2"}, commit=True) == 1 and [i["record_uuid"] for i in ledger.batch(s.batch)["items"]] == ["l_p1"]
          and abs(ledger.batch(s.batch)["usd"] - 0.02) < 1e-9 and not limits.touches("o-l_p2", "2026-11-09"),
          "and pull an owner out for real: the batch shrinks and the owner's place for the day is given back")
    other = ledger.batch_create("t_open", "mail", [], 0)
    check(textlane.pull(other, {"l_p1"}, commit=True) == 0, "pull leaves a batch of another kind alone")
    try:
        textlane.release(other, crm=crm, pol=pol, commit=True)
        check(False, "the text release refuses a batch of another kind")
    except SystemExit:
        check(True, "the text release refuses a batch of another kind")
    it = ledger.batch(s.batch)["items"][0]
    check(it.get("record_uuid") and "price" in it and it["price"] == 0.02,
          "a staged item carries the record id and its price, the two things the revocation stage reads to pull it")


def _followup_checks(check, *, store, activity, ledger, lanes, limits, policy, scrub, sweep, textlane, screened, **_) -> None:
    print("  text lane: the follow-up series")
    tz = "America/New_York"
    pol = policy.parse(copy.deepcopy(PLAYBOOK), EMPLOYEES)
    pol_tabled = policy.parse(_with("t_limits", platform_cap=0), EMPLOYEES)       # the platform's rest switched off
    _wipe(store, activity, limits, scrub)

    def opened(u, number, days, month=10, cohort="FTM 1026 Named East"):
        """The opening texts already went out on these days (released, on the cohort's thread)."""
        for i, d in enumerate(days, 1):
            limits.stage_text(stage="t_open", batch_id=700 + i, owner_uuid="o-" + u, record_uuid=u,
                              numbers=[{"number": number, "thread": "smarter_contact:" + cohort, "send_at": limits._iso(at(d, 9, 15, month=month))}],
                              system="smarter_contact", series="opening", step=i, touch="open-%s-%d" % (u, i), tz=tz)
            limits.release_text(700 + i, u, [number])

    def go(crm, when, policy_):
        s = sweep.run("t_follow", crm=crm, now=when, pol=policy_)
        it = ledger.batch(s.batch)["items"][0] if s.batch else None
        if s.batch:
            textlane.release(s.batch, crm=crm, pol=policy_, commit=True, now=when + timedelta(minutes=15))
        return s, it

    plan = textlane.followup_plan(pol.lanes.timeline("ftm"), pol.lanes, "ftm")
    check(plan["count"] == 4 and plan["offsets"] == [0, 3, 7, 10] and plan["gap"] == 3 and plan["close"] == 23 and plan["earliest"] == 5,
          "the series is read off the lanes table: four texts over days 8 to 18, three business days apart, closed after day 23")

    # --- as the table writes it (the platform's rest switched off): days 8, 11, 15, 18
    crm = _world([("f1", "120 Follow St", at(26, 7, 0, month=10), [mob(3101)], {"predictivecall_attempts": 5})], screened)
    opened("f1", "8655553101", (26, 27, 28))
    s, it = go(crm, at(5, 10, 15), pol_tabled)                       # Thursday Nov 5: business day 8
    n0 = (it or {"numbers": [{}]})["numbers"][0]
    check(bool(it) and it["series"] == "followup" and it["step"] == 1 and it["template_set"] == "ftm_followup"
          and it["templates"][0] == "FTM F1 Named 01", "on day 8, after the call round, the first follow-up text is loaded")
    check(n0.get("group") == "FTM F1 1105 Named East" and n0.get("cohort") == "FTM 1026 Named East"
          and n0.get("thread") == "smarter_contact:FTM 1026 Named East" and n0.get("new_contact") is False,
          "it goes in a new group of its own and carries the thread key of the owner's first text")
    s, it = go(crm, at(6, 10, 15), pol_tabled)
    check(it is None and s.tally["not yet: the next follow-up is not due"] == 1, "the next one is not due the day after")
    days_sent = [8]
    for d, lane_day in ((10, 11), (16, 15), (19, 18)):
        s, it = go(crm, at(d, 10, 15), pol_tabled)
        if it:
            days_sent.append(it["lane_day"])
    check(days_sent == [8, 11, 15, 18] and it and it["step"] == 4 and it["templates"][0] == "FTM F4 Named 01",
          "the four follow-ups go on days 8, 11, 15 and 18, and the last one is the breakup text")
    s, it = go(crm, at(20, 10, 15), pol_tabled)
    check(it is None and s.tally["finished the follow-up texts"] == 1, "after the breakup the series is finished")
    before = crm.reads
    go(crm, at(23, 10, 15), pol_tabled)
    check(crm.reads == before and crm.recs["f1"]["sms_attempts"] == 4, "a finished record is not read again, and its counter reads four")

    # --- with the platform's rest (the default): held to day 21 after text 1, and the breakup is still last
    crm = _world([("f2", "121 Follow St", at(26, 7, 0, month=10), [mob(3102)], {"predictivecall_attempts": 5})], screened)
    opened("f2", "8655553102", (26, 27, 28))
    s, it = go(crm, at(5, 10, 15), pol)
    check(it is None and s.tally["not yet: the texting platform is resting the contact"] == 1,
          "with the platform's rest on, day 8 is too soon after three opening texts: the follow-up waits")
    got = []
    for d in (16, 19, 24):
        s, it = go(crm, at(d, 10, 15), pol)
        got.append((it or {}).get("step"))
    check(got == [1, 2, 4], "it starts on calendar day 21, and when only one more text fits before the series closes, that text is the breakup")
    s, it = go(crm, at(27, 10, 15), pol)
    check(it is None and s.tally["follow-up texts closed for the record (past day 23 of the lane)"] == 1,
          "past the table's last day the series is closed")

    # --- an owner the opening texts never reached
    crm = _world([("f3", "122 Follow St", at(2, 7, 0), [mob(3103)], {"predictivecall_attempts": 2})], screened)
    s, it = go(crm, at(6, 10, 15), pol)
    check(it is None and crm.reads == 0 and s.tally["not yet: the follow-up texts start on day 5"] == 1,
          "before the call round can be over the record is not even opened")
    s, it = go(crm, at(10, 10, 15), pol)
    check(it is None and s.tally["closed today: follow-up texts start after the call round"] == 1,
          "follow-up texts wait for the call round to end")
    crm.recs["f3"]["predictivecall_attempts"] = 5
    s, it = go(crm, at(10, 11, 0), pol)
    n0 = (it or {"numbers": [{}]})["numbers"][0]
    check(bool(it) and it["step"] == 1 and it["template_set"] == "p1" and it["template_touch"] == 1 and n0.get("new_contact") is True
          and n0.get("thread") == "smarter_contact:FTM F1 1110 Named East",
          "an owner with no thread yet starts the series with the plain identity check, and that group becomes the thread")
    none = copy.deepcopy(PLAYBOOK)
    none["lanes"]["ftm"]["timeline"] = {"followup_texts": {"count": 0}}
    s = sweep.run("t_follow", crm=crm, now=at(11, 10, 15), pol=policy.parse(none, EMPLOYEES))
    check("has no follow-up texts" in s.head and not s.batch, "a lane whose timeline writes no follow-ups gets none")
    if hasattr(lanes, "tranche_open"):
        _wipe(store, activity, limits, scrub)
        crm = _world([("f_in", "123 Follow St", at(26, 7, 0, month=10), [mob(3111)], {"predictivecall_attempts": 5}),
                      ("f_out", "124 Follow St", at(26, 7, 0, month=10), [mob(3112)], {"predictivecall_attempts": 5})], screened)
        lanes.tranche_open("ftm", ["f_in"], at(26, 7, 0, month=10))
        s = sweep.run("t_follow", crm=crm, now=at(5, 10, 15), pol=policy.parse(_with("t_follow", tranche=True), EMPLOYEES))
        got = [i["record_uuid"] for i in ledger.batch(s.batch)["items"]] if s.batch else []
        check(got == ["f_in"] and s.tally["behind the lane's tranches, waiting their turn"] == 1,
              "in a lane worked in tranches the follow-up series reads only records that were drawn into one")

    # --- the time-sensitive touches for a sale two weeks out (decision D21)
    _wipe(store, activity, limits, scrub)
    hot = {"tags": ["FTM", "Auction Soon", "Auction Critical"]}
    crm = _world([("u1", "130 Soon St", at(9, 7, 0), [mob(3201)], hot), ("u2", "131 Soon St", at(9, 7, 0), [mob(3202)], {})], screened)
    s = sweep.run("t_open", crm=crm, now=at(9), pol=pol)
    first = {i["record_uuid"]: i for i in ledger.batch(s.batch)["items"]}
    textlane.release(s.batch, crm=crm, pol=pol, commit=True, now=at(9, 9, 0))
    s = sweep.run("t_open", crm=crm, now=at(10), pol=pol)
    second = {i["record_uuid"]: i for i in ledger.batch(s.batch)["items"]}
    check(first["u1"]["template_set"] == "p1" and second["u1"]["template_set"] == "ftm_urgent"
          and second["u1"]["templates"][0] == "FTM Urgent T2 Named 01" and second["u1"]["numbers"][0]["group"] == "FTM 1109 Named East T2 Urgent",
          "a record with a sale two weeks out gets the same text 1 as anyone, then the time-sensitive text 2, in its own group")
    check(second["u2"]["template_set"] == "p1" and second["u2"]["numbers"][0]["group"] == "FTM 1109 Named East T2",
          "every other record keeps the usual text 2")
    _wipe(store, activity, limits, scrub)
    crm = _world([("u3", "132 Soon St", at(9, 7, 0), [mob(3203)], hot)], screened)
    plain = policy.parse(_with("t_open", urgent_copy=False), EMPLOYEES)
    s = sweep.run("t_open", crm=crm, now=at(9), pol=plain)
    textlane.release(s.batch, crm=crm, pol=plain, commit=True, now=at(9, 9, 0))
    s = sweep.run("t_open", crm=crm, now=at(10), pol=plain)
    check(ledger.batch(s.batch)["items"][0]["template_set"] == "p1", "with urgent_copy off the time-sensitive templates are never used")
    base = policy.parse(copy.deepcopy(PLAYBOOK), EMPLOYEES)
    bad = _unchecked(base, "t_open", series="blast")
    s = sweep.run("t_open", crm=crm, now=at(10), pol=bad)
    check(any("series must be opening or followup" in p for p in s.problems) and not s.batch, "a series the stage does not know loads nothing")
    nolane = copy.deepcopy(base)
    nolane.stages["t_open"].params.pop("lane", None)
    s = sweep.run("t_open", crm=crm, now=at(10), pol=nolane)
    check(any("written timeline" in p for p in s.problems) and not s.batch, "a text stage with no lane loads nothing: it has no timeline to read")
    saved_pool = textlane.FOLLOWUP[1][0][0]
    textlane.FOLLOWUP[1][0][0] = "Hi {first}, I saw the foreclosure notice on {addr}. Want help? {sender}"
    try:
        check(any("names the list" in p for p in textlane.audit(fresh=True)), "a variant that names the list fails the audit")
        s = sweep.run("t_open", crm=_world([("u4", "133 Soon St", at(10, 7, 0), [mob(3204)], {})], screened), now=at(10), pol=pol)
        check(any("fails its own audit" in p for p in s.problems) and not s.batch, "and while the audit fails, the stage loads nothing at all")
    finally:
        textlane.FOLLOWUP[1][0][0] = saved_pool
        textlane.audit(fresh=True)


def _afternoon_checks(check, *, store, activity, ledger, limits, policy, scrub, sweep, textlane, screened, **_) -> None:
    print("  the afternoon text")
    tz = "America/New_York"
    pol = policy.parse(copy.deepcopy(PLAYBOOK), EMPLOYEES)
    thu, t_mon = 12, at(9, 7, 0)

    def opened(u, number, days=(9, 10, 11), cohort="FTM 1109 Named East"):
        for i, d in enumerate(days, 1):
            limits.stage_text(stage="t_open", batch_id=800 + i, owner_uuid="o-" + u, record_uuid=u,
                              numbers=[{"number": number, "thread": "smarter_contact:" + cohort, "send_at": limits._iso(at(d, 9, 15))}],
                              system="smarter_contact", series="opening", step=i, touch="open-%s-%d" % (u, i), tz=tz)
            limits.release_text(800 + i, u, [number])

    def world():
        _wipe(store, activity, limits, scrub)
        specs = [("p1", "140 Dial St", t_mon, [mob(3301), mob(3302, "Dial Second")], {}),     # dialed on both numbers, neither reached
                 ("p2", "141 Dial St", t_mon, [mob(3303)], {}),                                # reached by phone
                 ("p3", "142 Dial St", at(10, 7, 0), [mob(3304)], {}),                         # only on day 2 of the lane
                 ("p4", "143 Dial St", t_mon, [mob(3305)], {}),                                # no text thread yet
                 ("p5", "144 Dial St", t_mon, [mob(3306)], {}),                                # already texted today
                 ("p6", "145 Dial St", t_mon, [mob(3307)], {}),                                # the dialed number is on the registry
                 ("p7", "146 Dial St", t_mon, [mob(3308), mob(3309, "Dial Second")], {})]      # one number dialed, the other on the registry
        crm = _world(specs, screened)
        opened("p1", "8655553301")
        opened("p2", "8655553303")
        opened("p3", "8655553304", days=(10, 11))
        opened("p5", "8655553306")
        opened("p6", "8655553307")
        limits.stage_text(stage="t_full", batch_id=850, owner_uuid="o-p5", record_uuid="p5",
                          numbers=[{"number": "8655553306", "thread": "smarter_contact:FTM 1109 Named East", "send_at": limits._iso(at(thu, 9, 30))}],
                          system="smarter_contact", series="afternoon", step=3, touch="today-p5", tz=tz, campaign=False)
        limits.release_text(850, "p5", ["8655553306"])
        scrub.note("8655553307", True)
        scrub.note("8655553309", True)
        crm.log_rows = [call_ev("p1a", "o-p1", at(thu, 11, 0), "8655553301"), call_ev("p1b", "o-p1", at(thu, 11, 5), "8655553302"),
                        call_ev("p2a", "o-p2", at(thu, 11, 10), "8655553303"),
                        call_ev("p2b", "o-p2", at(thu, 11, 10), "8655553303", kind="answered", duration=120),
                        call_ev("p3a", "o-p3", at(thu, 11, 20), "8655553304"), call_ev("p4a", "o-p4", at(thu, 11, 30), "8655553305"),
                        call_ev("p5a", "o-p5", at(thu, 11, 40), "8655553306"), call_ev("p6a", "o-p6", at(thu, 11, 50), "8655553307"),
                        call_ev("p7a", "o-p7", at(thu, 11, 55), "8655553308")]
        return crm

    crm = world()
    s = sweep.run("t_full", crm=crm, now=at(thu, 16, 30), pol=pol)
    t = s.tally
    check(t["got a full attempt"] == 7 and t["numbers left off the list: on the do-not-call registry"] == 2
          and not any("146 Dial St" in ln and "not tried" in ln for ln in s.lines),
          "the 4:30 list no longer asks the caller to try a number the registry flags")
    check(t["dialed on a number the do-not-call registry flags"] == 1 and any("do-not-call registry flags" in p for p in s.problems),
          "and a dial to a flagged number is said the same afternoon")
    b = ledger.batch(s.batch) if s.batch else {"items": [{}], "kind": "", "status": ""}
    it = b["items"][0]
    check(b["kind"] == "text" and b["status"] == "staged" and len(b["items"]) == 1 and it.get("record_uuid") == "p1"
          and "Staged batch %d: 1 afternoon text(s) to 1 owner(s)" % s.batch in s.head and not crm.writes,
          "one owner gets an afternoon text: staged for a person, nothing sent, nothing written to the account")
    check([n["number"] for n in it["numbers"]] == ["8655553301"] and it["series"] == "afternoon" and it["campaign"] is False
          and it["numbers"][0]["thread"] == "smarter_contact:FTM 1109 Named East",
          "the text goes only to the dialed number that is already in the owner's thread, as a one-off on that thread")
    check("Pat" in it["text"] and "140 Dial St" in it["text"] and "Adriana" in it["text"] and len(it["text"]) <= 160
          and not textlane._LIST.search(it["text"]) and it["send_by"] == limits._iso(at(thu, 17, 30)),
          "the message names the street, is signed by the caller, fits one segment, and must go out by 5:30pm")
    check(t["reached by phone today: no text"] == 1, "an owner who was reached by phone gets no text")
    check(t["not on an afternoon-text day (days 3, 4, 5 of the lane)"] == 1,
          "the afternoon text runs on the call days after the opening texts, read from the lanes table")
    check(t["no text thread yet (the opening texts have not gone out): no afternoon text"] == 2,
          "the two owners with no text thread get none: the message goes in an existing thread or not at all")
    check(t["no room: the owner already has a text today"] == 1, "an owner who already has a text today gets none")
    check(t["numbers left out of the text: on the do-not-call registry"] == 2 and t["no dialed number that a text may use in the owner's thread"] == 1,
          "a dialed number on the registry is never texted, even though it sits in the owner's thread")
    check(not _digits_in(s), "the afternoon lines show the last four digits only")
    late = textlane.release(s.batch, crm=crm, pol=pol, commit=False, now=at(thu, 17, 45))
    check(late["owners"] == 0 and late["would_hold"] == {"past the time the afternoon texts go out by: they are not sent late": 1},
          "a batch released after 5:30pm is refused: today in the message would no longer be true")
    out = textlane.release(s.batch, crm=crm, pol=pol, commit=True, now=at(thu, 17, 0))
    load = json.loads(Path(out["load"]).read_text(encoding="utf-8"))
    check(out["owners"] == 1 and crm.writes == [("sms_attempts", "p1", 1)] and load["groups"] == [] and load["campaign"] is False
          and load["contacts"][0]["text"] == it["text"] and load["contacts"][0]["thread"] == "smarter_contact:FTM 1109 Named East",
          "the release hands over the rendered message for the open thread and raises the counter by one")
    check(limits.room("o-p1", "text", at(thu, 17, 5), policy=pol, system="smarter_contact", campaign=False).code == "day",
          "and that text is the owner's one text for the day")

    crm = world()
    s = sweep.run("t_full", crm=crm, now=at(thu, 16, 30), pol=policy.parse(_with("t_full", mode="plan"), EMPLOYEES))
    check("Would stage 1 afternoon text(s) to 1 owner(s)" in s.head and not s.batch and not ledger.batches() and not crm.writes,
          "in plan mode the afternoon text is listed and nothing is staged")
    crm = world()
    s = sweep.run("t_full", crm=crm, now=at(thu, 16, 30), pol=policy.parse(_with("t_full", afternoon_text=False), EMPLOYEES))
    check("afternoon" not in s.head and not s.batch and not any("text" in k for k in s.tally if "registry" not in k),
          "with afternoon_text off the stage is the 4:30 list and nothing else")
    crm = world()
    s = sweep.run("t_full", crm=crm, now=at(thu, 17, 40), pol=pol)
    check(s.tally["afternoon texts not staged: it is past 5:30pm"] == 1 and not s.batch, "a run after 5:30pm stages no afternoon text")
    two = copy.deepcopy(PLAYBOOK)
    two["people"]["callers"] = ["Adriana", "Marcus"]
    crm = world()
    s = sweep.run("t_full", crm=crm, now=at(thu, 16, 30), pol=policy.parse(two, EMPLOYEES))
    check(any("signed by the caller" in p for p in s.problems) and not s.batch,
          "with two callers and no sender named, no afternoon text is staged: the name is never guessed")
    two["stages"]["t_full"]["sender"] = "Marcus"
    crm = world()
    s = sweep.run("t_full", crm=crm, now=at(thu, 16, 30), pol=policy.parse(two, EMPLOYEES))
    check(bool(s.batch) and "Marcus" in ledger.batch(s.batch)["items"][0]["text"], "the sender named in the playbook signs the text")
    stale = s.batch
    s = sweep.run("t_full", crm=crm, now=at(13, 16, 30), pol=policy.parse(two, EMPLOYEES))
    check(any("batch %d" % stale in p and "never released" in p for p in s.problems) and ledger.batch(stale)["status"] == "expired",
          "an afternoon batch nobody released is closed the next day and said out loud")

    # --- the 4:30 list and the registry
    _wipe(store, activity, limits, scrub)
    crm = _world([("q1", "150 List St", t_mon, [mob(3401), mob(3402, "Dial Second")],
                   {"row_phone": {"number": "8655553402", "doNotCall": True}})], screened)
    crm.log_rows = [call_ev("q1a", "o-q1", at(thu, 11, 0), "8655553401")]
    listing = _with("t_full", afternoon_text=False)
    s = sweep.run("t_full", crm=crm, now=at(thu, 16, 30), pol=policy.parse(listing, EMPLOYEES))
    check(s.tally["got a full attempt"] == 1 and not s.lines, "while calls to registry numbers are held, a flagged number is not a number left to try")
    listing["stages"]["t_scrub"]["calls"] = "allow"
    s = sweep.run("t_full", crm=crm, now=at(thu, 16, 35), pol=policy.parse(listing, EMPLOYEES))
    check(s.tally["one or two numbers short"] == 1 and any("150 List St" in ln and "3402" in ln for ln in s.lines),
          "once the playbook allows those calls, the same number is back on the list")


PLAYBOOK_BLOCKS = """
  ftm_registry_scrub:
    kind: scrub
    owner: ops
    lane: ftm
    mode: "plan"
    schedule: "08:20 Mon-Fri"       # ahead of the text stage and the callers' day
    summary: The do-not-call registry flags on the first-to-market lane remembered and counted, so no flagged number is texted or put on a call list.
    # Decision D32 (11-13): a number DataSift's registry scrub flags is never texted, and is held
    # from calls until the attorney answers. The flag shows only on a search row, for one number a
    # record, so every flag seen is remembered and applies wherever that number turns up.
    presets:
      - FTM - 02 Ready to Call
      - FTM - 03 Call Attempt 1
      - FTM - 04 Call Attempt 2
      - FTM - 05 Call Attempt 3
      - FTM - 06 Call Attempt 4
    calls: hold                 # hold until the attorney answers; allow lifts it for calls only
    hold_unseen: false          # true also holds a number no row has shown with the flag off
    max_records: 60             # flagged records opened a run, to say how many have nothing left to text or call

  ftm_texts:
    kind: textlane
    owner: ops
    lane: ftm
    mode: "plan"
    schedule: "08:45 Mon-Fri"       # after the death screen (07:00 on) and the scrub (08:20), before the window opens at 9
    summary: New first-to-market owners loaded for text 1 the day they land and texts 2 and 3 on the next two days, staged for a person to release.
    # Plan 1.4. The stage never sends: it stages a batch, a person releases it, and the release
    # writes the load file for the Smarter Contact loader. The days come from lanes.ftm.timeline.
    series: opening
    presets:
      - FTM - 02 Ready to Call
      - FTM - 03 Call Attempt 1
      - FTM - 04 Call Attempt 2
    system: smarter_contact     # D4: Smarter Contact for every campaign
    label: FTM                  # the group name prefix in Smarter Contact
    numbers_per_owner: 3
    require_screen: true        # never before the death screen has seen the record (plan 0.21)
    stop_on_correct: true       # a number marked CORRECT means the owner answered: a person has the thread
    urgent_copy: true           # D21: for a sale inside 14 days, texts 2 and 3 use the time-sensitive templates
    urgent_tag: Auction Critical
    text_usd: 0.02              # what one text costs, for the plan and the stage cap
    daily_usd: 10
    daily_owners: 150
    max_records: 150            # full records opened a run
    max_new: 150                # records looked at for the first time a run

  ftm_followup_texts:
    kind: textlane
    owner: ops
    lane: ftm
    mode: "plan"
    schedule: "10:15 Mon-Fri"
    summary: After the call round, the follow-up texts on the lane's timeline, ending with the breakup text, staged for a person to release.
    # Plan 4.5 (decision D20). Count, first day and last day come from lanes.ftm.timeline.followup_texts.
    series: followup
    system: smarter_contact
    label: FTM
    numbers_per_owner: 3
    require_screen: true
    stop_on_correct: true
    text_usd: 0.02
    daily_usd: 10

  outreach_limits:
    kind: limits
    owner: ops
    mode: "plan"
    schedule: ["19:20 Mon-Fri", "07:25 daily"]   # the evening run checks today so far, the morning run all of yesterday
    summary: The day's dials and texts checked against the outreach limits; every one broken is named by caller and by a number's last four digits.
    # Plan 4.6. These numbers are also what every text stage asks before it stages anything
    # (limits.room), whatever this stage's mode.
    call_hours: "09:00-19:00"   # people call 9am to 7pm, on the business clock and on the owner's own
    text_hours: "09:00-18:00"   # what the machine sends goes out 9am to 6pm, on both clocks
    calls_per_line_day: 25
    texts_per_line_day: 25
    text_lines: 10              # how many sending numbers carry the day's texts: 25 x 10 = 250 a day
    lines_per_caller: 10
    monthly_texts: 20000
    owner_texts_day: 1
    owner_texts_week: 5
    owner_dials_number_day: 1   # one call per owner a day: a number is not dialed twice
    platform_cap: 3             # Smarter Contact: three campaign texts in seven days, then fourteen days of rest
    platform_window_days: 7
    platform_rest_days: 14
    count_record_counter: true  # the record's own text counter counts the other texting system's texts
"""

FULL_ATTEMPT_BLOCK = """
  full_attempt:
    kind: fullattempt
    owner: sales
    mode: "plan"
    schedule: "16:30 Mon-Fri"
    summary: The owners dialed today who still have a top-tier number nobody tried, listed while there is time to finish them.
    # Plan 1.3. A full attempt dials every Dial First and Second number. Shows the last four digits
    # of a number. A number the do-not-call registry flags is left off the list while those calls
    # are held (D32).
    short_by: 2
    lane: ftm                   # the lane whose timeline says which call days get the afternoon text
    # The afternoon text: one short text to a number dialed today and not reached, in the thread the
    # owner already has, staged for a person to release before 5:30pm. Keep it false until one
    # one-off text has been proven to land in an existing Smarter Contact thread (proofs.py, thread_text).
    afternoon_text: false
    sender: Adriana             # who signs it: the caller who dialed
    text_by: "17:30"
    reached_seconds: 60         # an answered call this long means the owner was reached: no text
"""


def _handover_checks(check, *, store, activity, ledger, limits, policy, scrub, sweep, screened, **_) -> None:
    print("  the playbook blocks handed over")
    import yaml
    blocks = yaml.safe_load("stages:" + PLAYBOOK_BLOCKS)["stages"]
    blocks.update(yaml.safe_load("stages:" + FULL_ATTEMPT_BLOCK)["stages"])
    real = yaml.safe_load((config.TEAM_DIR / "autopilot.yaml").read_text(encoding="utf-8")) or {}
    base = {k: v for k, v in real.items() if k != "stages"}
    try:
        pol = policy.parse(dict(base, stages=blocks), EMPLOYEES)
        ok = True
    except policy.PolicyError as exc:
        pol, ok = None, False
        print("    %s" % str(exc)[:400])
    check(ok and set(blocks) == {"ftm_registry_scrub", "ftm_texts", "ftm_followup_texts", "outreach_limits", "full_attempt"},
          "the five stage blocks handed to the orchestrator load against the playbook's own gate and lanes")
    if not ok:
        return
    check(all(st.mode == "plan" for st in pol.stages.values()), "every block ships in plan mode")
    lim, bad = limits.read(pol)
    check(not bad and lim == limits.Limits() and scrub.settings(pol).calls == "hold",
          "the limits block states the house defaults, and calls to registry numbers are held")
    clash = [sid for sid in blocks if sid in (real.get("stages") or {}) and (real["stages"][sid] or {}).get("kind") != blocks[sid]["kind"]]
    check(not clash, "no handed-over block takes the id of a different stage already in the playbook")
    check(pol.stage("ftm_texts").spend == "outreach" and pol.stage("ftm_followup_texts").get("series") == "followup"
          and pol.stage("full_attempt").get("afternoon_text") is False and len(pol.stage("outreach_limits").schedule) == 2,
          "the text stages are outreach, the afternoon text ships switched off, and the limits check runs evening and morning")
    # Each block, run as handed over, against the fake account: plan mode, nothing written.
    _wipe(store, activity, limits, scrub)
    for title in ("FTM - 05 Call Attempt 3", "FTM - 06 Call Attempt 4"):
        PRESETS.setdefault(title, lambda r: False)
    crm = _world([("z1", "200 Plan St", at(9, 7, 0), [mob(4101)], {"row_phone": {"number": "8655554101", "doNotCall": False}})], screened)
    heads = {sid: sweep.run(sid, crm=crm, now=at(9), pol=pol) for sid in blocks}
    check(all(not s.live and not s.batch and not s.problems for s in heads.values()) and not crm.writes and not ledger.batches(),
          "each handed-over stage runs in plan mode against the fake account, raises no problem and writes nothing")
    check("Would stage 1 text(s) to 1 owner(s)" in heads["ftm_texts"].head
          and "0 of 1 numbers shown in the ftm lane" in heads["ftm_registry_scrub"].head,
          "and each plan reads as a person would expect: one owner to text, one number shown and none flagged")

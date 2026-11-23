"""The first-to-market texts and the foreclosure research run on their own (2026-11-18).

Three pieces: the narrow switch (AGENTS_LIVE_STAGES: on a free box only the named stages act and
spend, and only the research tasks they queue run live), the text stage releasing its own batch
and handing it to the texting app (only what Smarter Contact loaded counts as texted), and the
handover itself (scpush, which never prints the secret route). Zero network: the fake DataSift
of the text lane selftest, a fake loader and a fake HTTP session. Every name, street and number
here is invented.
"""
from __future__ import annotations

import copy
import json
import os

from . import config


def run(check, tmp) -> None:
    from . import store
    from .autopilot import activity, ledger, limits, policy, registry, scpush, scrub, sweep, textlane
    from .selftest_autopilot import EMPLOYEES, PRESETS
    from .selftest_textlane import PLAYBOOK, _register, _wipe, _world, at, mob
    saved = (config.DATA_DIR, config.DAILY_CAP_USD, config.RUN_CAP_USD, config.MODE, config.LIVE_STAGES)
    saved_loader = scpush.hand_over
    saved_presets = dict(PRESETS)
    PRESETS["FTM - 03 Call Attempt 1"] = lambda r: "FTM" in r["tags"] and r["owner"]["phones"] and r["predictivecall_attempts"] == 1
    PRESETS["FTM - 04 Call Attempt 2"] = lambda r: "FTM" in r["tags"] and r["owner"]["phones"] and r["predictivecall_attempts"] == 2
    config.DATA_DIR, config.DAILY_CAP_USD, config.RUN_CAP_USD = tmp, 100000.0, 5.0
    store.init()
    restore = _register(policy, registry)
    try:
        _switch_checks(check)
        config.MODE = "live"
        _auto_checks(check, store=store, activity=activity, ledger=ledger, limits=limits, policy=policy, scrub=scrub,
                     sweep=sweep, textlane=textlane, scpush=scpush, EMPLOYEES=EMPLOYEES, PLAYBOOK=PLAYBOOK, _wipe=_wipe,
                     _world=_world, at=at, mob=mob)
        _first_text_checks(check, store=store, activity=activity, ledger=ledger, limits=limits, policy=policy, scrub=scrub,
                           sweep=sweep, scpush=scpush, EMPLOYEES=EMPLOYEES, PLAYBOOK=PLAYBOOK, _wipe=_wipe, _world=_world,
                           at=at, mob=mob)
        _texted_before_checks(check, store=store, activity=activity, ledger=ledger, limits=limits, policy=policy, scrub=scrub,
                              sweep=sweep, scpush=scpush, textlane=textlane, EMPLOYEES=EMPLOYEES, PLAYBOOK=PLAYBOOK,
                              _wipe=_wipe, _world=_world, at=at, mob=mob)
        _anchor_checks(check, store=store, activity=activity, ledger=ledger, limits=limits, policy=policy, scrub=scrub,
                       sweep=sweep, scpush=scpush, EMPLOYEES=EMPLOYEES, PLAYBOOK=PLAYBOOK, _wipe=_wipe, _world=_world,
                       at=at, mob=mob)
        _settle_checks(check, store=store, activity=activity, ledger=ledger, limits=limits, policy=policy, scrub=scrub,
                       sweep=sweep, scpush=scpush, textlane=textlane, EMPLOYEES=EMPLOYEES, PLAYBOOK=PLAYBOOK, _wipe=_wipe,
                       _world=_world, at=at, mob=mob)
        _screen_write_checks(check, store=store, activity=activity, ledger=ledger, limits=limits, scrub=scrub, policy=policy,
                             sweep=sweep, EMPLOYEES=EMPLOYEES, PLAYBOOK=PLAYBOOK, _wipe=_wipe, at=at, mob=mob)
        _note_route_checks(check)
        scpush.hand_over = saved_loader
        _push_checks(check, scpush)
        _playbook_checks(check, policy)
    finally:
        scpush.hand_over = saved_loader
        restore()
        PRESETS.clear()
        PRESETS.update(saved_presets)
        config.DATA_DIR, config.DAILY_CAP_USD, config.RUN_CAP_USD, config.MODE, config.LIVE_STAGES = saved


def _switch_checks(check) -> None:
    print("  the narrow switch: AGENTS_LIVE_STAGES")
    config.MODE, config.LIVE_STAGES = "free", frozenset({"ftm_texts", "research_queue"})
    check(config.stage_live("free") and config.stage_live("outreach", "ftm_texts") and config.stage_live("paid", "research_queue")
          and not config.stage_live("outreach") and not config.stage_live("outreach", "top1000_texts"),
          "on a free box a stage named in AGENTS_LIVE_STAGES acts whatever it spends, and no other stage does")
    check(config.task_live({"source": "autopilot:research_queue"}) and not config.task_live({"source": "autopilot:ftm_deep_prospect"})
          and not config.task_live({"source": "datasift:deep-prospect"}) and not config.task_live({"source": "slack"})
          and not config.task_live(None),
          "the research a named stage queued runs live; a webhook, a Slack ask or another stage's handoff stays dry")
    config.MODE = "dry"
    check(not config.stage_live("outreach", "ftm_texts") and not config.task_live({"source": "autopilot:research_queue"}),
          "a dry box acts on nothing, whatever the list says")
    config.MODE, config.LIVE_STAGES = "free", frozenset()
    check(not config.stage_live("outreach", "ftm_texts") and config.stage_live("free"),
          "with the list empty, free mode is exactly what it was")
    config.MODE = "live"
    check(config.stage_live("outreach") and config.task_live({"source": "slack"}), "a live box acts on everything, as before")


def _auto_checks(check, *, store, activity, ledger, limits, policy, scrub, sweep, textlane, scpush, EMPLOYEES, PLAYBOOK,
                 _wipe, _world, at, mob) -> None:
    print("  the text stage releases its own batch")
    book = copy.deepcopy(PLAYBOOK)
    book["stages"]["t_open"]["auto_release"] = True
    pol = policy.parse(book, EMPLOYEES)
    seen = {}

    def screened(*uuids):
        for u in uuids:
            ledger.set_fact(u, "death_screen", {"at": "2026-11-09T12:00:00+00:00", "hit": "", "detail": ""})

    def refuse(load):
        seen.setdefault("refuse", []).append(load)
        return {"status": "failed", "result": {"ok": False, "loaded": [],
                                               "refused": "no Smarter Contact number can send (10 numbers, 10 not approved): "
                                                          "nothing was loaded"}}

    def load_all(load):
        seen.setdefault("all", []).append(load)
        return {"status": "done", "result": {"ok": True, "loaded": [c["phone"] for c in load["contacts"]],
                                             "groups": [{"group": g["name"], "contacts": g["contacts"], "campaign": "k1",
                                                         "state": "SCHEDULED"} for g in load["groups"]],
                                             "contacts_new": len(load["contacts"]), "skipped": {}, "problems": []}}

    # --- Smarter Contact refuses: nothing counted, nothing written, the owner is tried again with text 1
    _wipe(store, activity, limits, scrub)
    scpush.hand_over = refuse
    crm = _world([("s1", "21 Auto St", at(9, 7, 0), [mob(3101), mob(3102, "Dial Second")], {})], screened)
    s = sweep.run("t_open", crm=crm, now=at(9), pol=pol)
    b = ledger.batch(s.batch) if s.batch else {}
    check(len(seen.get("refuse") or []) == 1 and b.get("status") == "held" and not crm.writes
          and not limits.rows(statuses=("released",)) and not limits.rows(statuses=("staged",)),
          "a load Smarter Contact refuses counts nobody as texted, raises no counter and leaves nothing staged")
    check(any("no Smarter Contact number can send" in p for p in s.problems) and "refused" in s.head and "10 not approved" in s.head,
          "the refusal is said in the stage's own words, with Smarter Contact's reason")
    s2 = sweep.run("t_open", crm=crm, now=at(9, 9, 30), pol=pol)
    b2 = ledger.batch(s2.batch) if s2.batch else {"items": [{}]}
    check(bool(s2.batch) and b2["items"][0].get("step") == 1, "the next run tries the same owner with text 1 again, never text 2")

    # --- Smarter Contact loads everything: counted and written exactly as a person's release
    _wipe(store, activity, limits, scrub)
    scpush.hand_over = load_all
    crm = _world([("s2", "22 Auto St", at(9, 7, 0), [mob(3201), mob(3202, "Dial Second")], {})], screened)
    s = sweep.run("t_open", crm=crm, now=at(9), pol=pol)
    b = ledger.batch(s.batch) if s.batch else {}
    check(b.get("status") == "sent" and crm.writes == [("sms_attempts", "s2", 1)]
          and len(limits.rows(batch_id=s.batch, statuses=("released",))) == 2,
          "a load Smarter Contact took is counted and raises the record's text counter once, as a person's release does")
    check("In Smarter Contact: 1 campaign(s) for 2 text(s), 2 new contact(s)" in s.head and not s.problems,
          "the stage says how many campaigns and texts Smarter Contact now holds (%s | %s)" % (s.head, s.problems[:2]))
    load = seen["all"][-1]
    check(load["groups"][0]["name"] == "FTM 1109 Named East" and load["groups"][0]["templates"][:1] == ["P1 T1 Named 01"]
          and not any(len(c["phone"]) != 10 for c in load["contacts"]) and json.dumps(b.get("result") or {}).count("k1") >= 1,
          "the load names the day's group, its templates and ten-digit numbers, and the batch keeps Smarter Contact's answer")

    # --- part loaded: only the loaded number counts
    _wipe(store, activity, limits, scrub)

    def load_first(load):
        first = load["contacts"][0]["phone"]
        return {"status": "done", "result": {"ok": True, "loaded": [first], "groups": [], "skipped": {}, "problems": []}}
    scpush.hand_over = load_first
    crm = _world([("s3", "23 Auto St", at(9, 7, 0), [mob(3301), mob(3302, "Dial Second")], {})], screened)
    s = sweep.run("t_open", crm=crm, now=at(9), pol=pol)
    rel = limits.rows(batch_id=s.batch, statuses=("released",)) if s.batch else []
    check(len(rel) == 1 and crm.writes == [("sms_attempts", "s3", 1)],
          "when Smarter Contact loads one of two numbers, only that one counts as texted")

    # --- still building when the run stopped waiting: counted, so text 1 never goes twice
    _wipe(store, activity, limits, scrub)
    scpush.hand_over = lambda load: {"status": "loading", "why": "still loading after 300 seconds"}
    crm = _world([("s4", "24 Auto St", at(9, 7, 0), [mob(3401)], {})], screened)
    s = sweep.run("t_open", crm=crm, now=at(9), pol=pol)
    check(len(limits.rows(batch_id=s.batch, statuses=("released",))) == 1 and "still building" in s.head,
          "a load still building when the run stopped waiting is counted, so the same owner never gets text 1 twice")

    # --- since: a record that entered the lane before the date is left alone
    _wipe(store, activity, limits, scrub)
    scpush.hand_over = load_all
    late = copy.deepcopy(book)
    late["stages"]["t_open"]["since"] = "2026-11-10"
    pol_late = policy.parse(late, EMPLOYEES)
    crm = _world([("s5", "25 Auto St", at(9, 7, 0), [mob(3501)], {})], screened)
    s = sweep.run("t_open", crm=crm, now=at(9), pol=pol_late)
    check(not s.batch and s.tally.get("entered the lane before 2026-11-10, when these texts began") == 1,
          "a record that entered the lane before `since` gets nothing from the stage")
    for bad, why in (({"since": "soon"}, "since must be a date"), ({"auto_release": "yes"}, "auto_release must be true or false")):
        wrong = copy.deepcopy(book)
        wrong["stages"]["t_open"].update(bad)
        try:
            policy.parse(wrong, EMPLOYEES)
            check(False, "a stage block with %s is refused when the playbook loads" % why)
        except policy.PolicyError as exc:
            check(why in str(exc), "a stage block with %s is refused when the playbook loads" % why)

    # --- without auto_release nothing changes: the batch waits for a person
    _wipe(store, activity, limits, scrub)
    scpush.hand_over = load_all
    manual = policy.parse(copy.deepcopy(PLAYBOOK), EMPLOYEES)
    crm = _world([("s6", "26 Auto St", at(9, 7, 0), [mob(3601)], {})], screened)
    n_before = len(seen.get("all") or [])
    s = sweep.run("t_open", crm=crm, now=at(9), pol=manual)
    check(ledger.batch(s.batch)["status"] == "staged" and len(seen.get("all") or []) == n_before and not crm.writes,
          "a text stage without auto_release still only stages, as before")


def _first_text_checks(check, *, store, activity, ledger, limits, policy, scrub, sweep, scpush, EMPLOYEES, PLAYBOOK,
                       _wipe, _world, at, mob) -> None:
    print("  series_start first_text: the backlog of foreclosures")
    from datetime import timedelta
    book = copy.deepcopy(PLAYBOOK)
    book["stages"]["t_open"]["auto_release"] = True
    book["stages"]["t_back"] = dict(book["stages"]["t_open"], series_start="first_text", label="FTM Backlog",
                                    schedule="08:50 Mon-Fri")
    book["stages"]["t_follow"].update(auto_release=True)
    book["stages"]["t_follow_ft"] = dict(book["stages"]["t_follow"], series_start="first_text", schedule="10:20 Mon-Fri")
    pol = policy.parse(book, EMPLOYEES)

    def screened(*uuids):
        for u in uuids:
            ledger.set_fact(u, "death_screen", {"at": "2026-11-09T12:00:00+00:00", "hit": "", "detail": ""})

    def load_all(load):
        return {"status": "done", "result": {"ok": True, "loaded": [c["phone"] for c in load["contacts"]],
                                             "groups": [{"group": g["name"], "contacts": g["contacts"], "campaign": "k",
                                                         "state": "SCHEDULED"} for g in load["groups"]],
                                             "contacts_new": 0, "skipped": {}, "problems": []}}
    scpush.hand_over = load_all

    def steps(owner_uuid, day):
        return sorted(int(t["step"]) for t in limits.touches(owner_uuid, day, since_day="2026-10-01", series="opening"))

    # --- a foreclosure that landed a month ago: the lane-day stage cannot reach it, first_text can
    _wipe(store, activity, limits, scrub)
    crm = _world([("f1", "41 Old Sale St", at(9, 7, 0) - timedelta(days=30), [mob(4101)], {})], screened)
    owner = crm.recs["f1"]["owner"]["uuid"]
    s = sweep.run("t_open", crm=crm, now=at(9), pol=pol)
    check(not s.batch, "the lane-day stage leaves a month-old record alone (past its first three days)")
    s = sweep.run("t_back", crm=crm, now=at(9, 8, 50), pol=pol)
    b = ledger.batch(s.batch) if s.batch else {"items": [{}]}
    check(bool(s.batch) and b["items"][0].get("step") == 1 and b["status"] == "sent"
          and all(n["group"].startswith("FTM Backlog 1109") for n in b["items"][0].get("numbers") or []),
          "a first_text stage gives it text 1 today, in its own groups")
    for day, want in ((10, 2), (11, 3)):
        sweep.run("t_back", crm=crm, now=at(day, 8, 50), pol=pol)
        check(steps(owner, "2026-11-%02d" % day) == list(range(1, want + 1)),
              "then text %d the next business day, counted from its first text" % want)
    s = sweep.run("t_back", crm=crm, now=at(12, 8, 50), pol=pol)
    check(not s.batch and steps(owner, "2026-11-12") == [1, 2, 3], "and nothing after text 3: the opening series is done")

    # --- an owner the lane-day stage texted yesterday is continued, never given text 1 again
    _wipe(store, activity, limits, scrub)
    crm = _world([("f2", "42 New Notice St", at(9, 7, 0), [mob(4201)], {})], screened)
    owner = crm.recs["f2"]["owner"]["uuid"]
    sweep.run("t_open", crm=crm, now=at(9), pol=pol)
    s_same = sweep.run("t_back", crm=crm, now=at(9, 8, 50), pol=pol)
    s = sweep.run("t_back", crm=crm, now=at(10, 8, 50), pol=pol)
    it = (ledger.batch(s.batch) or {"items": [{}]})["items"][0] if s.batch else {}
    check(not s_same.batch and it.get("step") == 2 and steps(owner, "2026-11-10") == [1, 2],
          "an owner the daily stage texted gets nothing more that day, and text 2 (not a second text 1) from the backlog stage next")

    # --- follow-ups count from the first opening text; an owner no opening stage texted gets none.
    # f4 sits in no call preset (its counter is past the presets), so neither opening stage reaches it.
    _wipe(store, activity, limits, scrub)
    crm = _world([("f3", "43 Old Sale St", at(9, 7, 0) - timedelta(days=30), [mob(4301)], {}),
                  ("f4", "44 Never Texted St", at(9, 7, 0) - timedelta(days=30), [mob(4401)], {"predictivecall_attempts": 7})],
                 screened)
    o3, o4 = crm.recs["f3"]["owner"]["uuid"], crm.recs["f4"]["owner"]["uuid"]
    for day in (9, 10, 11):
        sweep.run("t_back", crm=crm, now=at(day, 8, 50), pol=pol)
    fu = lambda o: limits.touches(o, "2026-12-31", since_day="2026-10-01", series="followup")  # noqa: E731
    sweep.run("t_follow_ft", crm=crm, now=at(13, 10, 20), pol=pol)
    check(not fu(o3), "no follow-up before day 8 after the first text")
    s = sweep.run("t_follow_ft", crm=crm, now=at(19, 10, 20), pol=pol)
    check(not fu(o3) and s.tally.get("not yet: the texting platform is resting the contact") == 1,
          "on day 8 the texting platform's rest after three texts in a week still holds the follow-up")
    s = sweep.run("t_follow_ft", crm=crm, now=at(30, 10, 20), pol=pol)   # the rest ends 7 + 14 days after text 1
    check(len(fu(o3)) == 1 and int(fu(o3)[0]["step"]) == 1,
          "after the rest, follow-up 1 goes, counted from the first opening text (%s %s)" % (s.head, dict(s.tally)))
    check(not fu(o4) and not steps(o4, "2026-11-30"),
          "an owner no opening stage texted gets no follow-up from a first_text follow-up stage")
    for bad in ({"series_start": "whenever"},):
        wrong = copy.deepcopy(book)
        wrong["stages"]["t_back"].update(bad)
        try:
            policy.parse(wrong, EMPLOYEES)
            check(False, "series_start must be lane or first_text")
        except policy.PolicyError as exc:
            check("series_start must be lane or first_text" in str(exc), "series_start must be lane or first_text")

    print("  list_sets: a probate gets its own opening texts, in its own groups")
    _wipe(store, activity, limits, scrub)
    probate_book = copy.deepcopy(book)
    probate_book["stages"]["t_back"]["list_sets"] = {"Probate": "ftm_probate"}
    ppol = policy.parse(probate_book, EMPLOYEES)
    crm = _world([("p1x", "51 Estate St", at(9, 7, 0) - timedelta(days=30), [mob(5101)], {"lists": ["Probate", "Owner Occupied"]}),
                  ("p2x", "52 Sale St", at(9, 7, 0) - timedelta(days=30), [mob(5201)], {"lists": ["Foreclosure"]})], screened)
    s = sweep.run("t_back", crm=crm, now=at(9, 8, 50), pol=ppol)
    items = {it["record_uuid"]: it for it in ((ledger.batch(s.batch) or {}).get("items") or [])} if s.batch else {}
    pr, fc = items.get("p1x") or {}, items.get("p2x") or {}
    check(pr.get("template_set") == "ftm_probate" and (pr.get("templates") or [""])[0] == "FTM Probate T1 Named 01"
          and all(n["group"].startswith("FTM Backlog Probate 1109") for n in pr.get("numbers") or []),
          "a probate takes the probate opening texts, in a probate group")
    check(fc.get("template_set") == "p1" and all(not n["group"].startswith("FTM Backlog Probate") for n in fc.get("numbers") or []),
          "a foreclosure in the same run keeps the Priority 1 texts and its own group: no campaign mixes two sets")
    try:
        bad_book = copy.deepcopy(book)
        bad_book["stages"]["t_back"]["list_sets"] = {"Probate": "no_such_set"}
        policy.parse(bad_book, EMPLOYEES)
        check(False, "a list_sets value that names no template set is refused")
    except policy.PolicyError as exc:
        check("list_sets must map a list title" in str(exc), "a list_sets value that names no template set is refused")


def _load_all(load):
    """A fake texting app that loads every number it is given, each group in a started campaign."""
    return {"status": "done", "result": {"ok": True, "loaded": [c["phone"] for c in load["contacts"]],
                                         "groups": [{"group": g["name"], "contacts": g["contacts"], "campaign": "k-%s" % g["name"][-4:],
                                                     "state": "SCHEDULED"} for g in load["groups"]],
                                         "contacts_new": 0, "skipped": {}, "problems": []}}


def _refuse(load):
    """A fake texting app that refuses the load before it writes anything, as sc_load does with no number able to send."""
    return {"status": "failed", "result": {"ok": False, "loaded": [],
                                           "refused": "no Smarter Contact number can send (2 numbers, 2 not approved): nothing was loaded"}}


def _anchor_checks(check, *, store, activity, ledger, limits, policy, scrub, sweep, scpush, EMPLOYEES, PLAYBOOK, _wipe, _world,
                   at, mob) -> None:
    print("  series_start first_text: the series starts the day its text 1 is handed over")
    from datetime import timedelta
    book = copy.deepcopy(PLAYBOOK)
    book["stages"]["t_open"]["auto_release"] = True
    book["stages"]["t_back"] = dict(book["stages"]["t_open"], series_start="first_text", label="FTM Backlog", schedule="08:50 Mon-Fri")
    pol = policy.parse(book, EMPLOYEES)
    key = "text_anchor:t_back:ftm"

    def screened(*uuids):
        for u in uuids:
            ledger.set_fact(u, "death_screen", {"at": "2026-11-09T12:00:00+00:00", "hit": "", "detail": ""})

    def steps(owner_uuid, day):
        return sorted(int(t["step"]) for t in limits.touches(owner_uuid, day, since_day="2026-10-01", series="opening"))

    # --- Monday Smarter Contact refuses the whole load, then takes the next three
    _wipe(store, activity, limits, scrub)
    crm = _world([("a1", "91 Anchor St", at(9, 7, 0) - timedelta(days=30), [mob(9101)], {})], screened)
    owner = crm.recs["a1"]["owner"]["uuid"]
    scpush.hand_over = _refuse
    s = sweep.run("t_back", crm=crm, now=at(9, 8, 50), pol=pol)
    check(bool(s.batch) and ledger.batch(s.batch)["status"] == "held" and ledger.fact("a1", key) is None and not steps(owner, "2026-11-09"),
          "a text 1 Smarter Contact refused counts nobody and does not start the series' clock")
    scpush.hand_over = _load_all
    s = sweep.run("t_back", crm=crm, now=at(10, 8, 50), pol=pol)
    check(ledger.fact("a1", key) == "2026-11-10" and steps(owner, "2026-11-10") == [1],
          "the series' day 0 is the day text 1 was handed over: %s" % ledger.fact("a1", key))
    for day in (11, 12):
        sweep.run("t_back", crm=crm, now=at(day, 8, 50), pol=pol)
    check(steps(owner, "2026-11-12") == [1, 2, 3],
          "so texts 2 and 3 still follow on the next two business days; dated from the refused Monday, the series closed "
          "before text 3: %s" % steps(owner, "2026-11-12"))

    # --- a batch staged and never released starts no clock either
    _wipe(store, activity, limits, scrub)
    manual = copy.deepcopy(book)
    manual["stages"]["t_back"]["auto_release"] = False
    crm = _world([("a2", "92 Anchor St", at(9, 7, 0) - timedelta(days=30), [mob(9201)], {})], screened)
    s = sweep.run("t_back", crm=crm, now=at(9, 8, 50), pol=policy.parse(manual, EMPLOYEES))
    check(bool(s.batch) and ledger.batch(s.batch)["status"] == "staged" and ledger.fact("a2", key) is None,
          "a staged batch nobody has released starts no series clock")


def _settle_checks(check, *, store, activity, ledger, limits, policy, scrub, sweep, scpush, textlane, EMPLOYEES, PLAYBOOK,
                   _wipe, _world, at, mob) -> None:
    print("  a load still building when the stage stopped waiting, read again later")
    from datetime import timedelta

    from .autopilot import stages
    book = copy.deepcopy(PLAYBOOK)
    book["stages"]["t_open"]["auto_release"] = True
    book["stages"]["t_back"] = dict(book["stages"]["t_open"], series_start="first_text", label="FTM Backlog", schedule="08:50 Mon-Fri")
    pol = policy.parse(book, EMPLOYEES)
    num = lambda n: "865555%04d" % n  # noqa: E731
    answers, asked = {}, []

    def building(load):
        return {"status": "loading", "why": "still loading after 300 seconds; the texting app keeps going"}

    def status_of(batch, **kw):
        asked.append(int(batch))
        return answers.get(int(batch), {"status": "loading"})

    def screened(*uuids):
        for u in uuids:
            ledger.set_fact(u, "death_screen", {"at": "2026-11-09T12:00:00+00:00", "hit": "", "detail": ""})

    saved_status = scpush.job_status
    scpush.job_status = status_of
    try:
        # --- Monday: released while Smarter Contact was still building, so every number counts
        _wipe(store, activity, limits, scrub)
        scpush.hand_over = building
        crm = _world([("w1", "81 Wait St", at(9, 7, 0), [mob(8101), mob(8102, "Dial Second")], {}),
                      ("w2", "82 Wait St", at(9, 7, 0), [mob(8201)], {}),
                      ("w3", "83 Wait St", at(9, 7, 0), [mob(8301)], {"first": ""})], screened)
        s = sweep.run("t_open", crm=crm, now=at(9), pol=pol)
        bid = s.batch
        check(bool(bid) and len(limits.rows(batch_id=bid, statuses=("released",))) == 4
              and sorted(w for w in crm.writes if w[0] == "sms_attempts") == [("sms_attempts", u, 1) for u in ("w1", "w2", "w3")],
              "released while Smarter Contact was still building: every number counts, so none can get text 1 twice")
        group = {c["phone"]: c["group"] for c in json.loads(textlane.load_path(bid).read_text(encoding="utf-8"))["contacts"]}
        s = sweep.run("t_open", crm=crm, now=at(9, 9, 30), pol=pol)
        check(asked == [bid] and len(limits.rows(batch_id=bid, statuses=("released",))) == 4
              and s.tally.get(textlane.SETTLE_WAIT) == 1,
              "while the answer is not final the count stands, and the answer is asked for again on the next run")

        # --- Tuesday the answer is final. W1's first number loaded and its second was on Smarter Contact's own
        # list; W2's lookup failed; W3's group failed part way, so its campaign may have started.
        answers[bid] = {"status": "failed", "result": {
            "ok": False, "loaded": [num(8101)], "skipped": {"on Smarter Contact's own do-not-text list": 1},
            "groups": [{"group": group[num(8101)], "contacts": 1, "campaign": "k-named", "state": "SCHEDULED"},
                       {"group": group[num(8301)], "contacts": 1, "campaign": "", "state": "failed"}],
            "problems": ["phone lookup failed for a number ending 8201 (timeout)"]}}
        scpush.hand_over = _load_all
        n0 = len(crm.writes)
        s = sweep.run("t_open", crm=crm, now=at(10), pol=pol)
        check({r["number"] for r in limits.rows(batch_id=bid, statuses=("released",))} == {num(8101), num(8301)}
              and {r["number"] for r in limits.rows(batch_id=bid, statuses=("not_loaded",))} == {num(8102), num(8201)},
              "the final answer takes out of the count only the numbers it shows never loaded; a number in a group that "
              "failed part way stays counted")
        mine = crm.writes[n0:]
        check(mine[:1] == [("sms_attempts", "w2", 0)] and ("sms_attempts", "w2", 1) in mine[1:]
              and ("sms_attempts", "w1", 2) in mine and ("sms_attempts", "w3", 2) in mine,
              "the owner left with no text from that batch gets its counter put back before anything new goes; the others "
              "keep theirs (%s)" % mine)
        b2 = ledger.batch(s.batch) if s.batch else {"items": []}
        got = {it["record_uuid"]: (it["step"], [n["number"] for n in it["numbers"]]) for it in b2["items"]}
        check(got == {"w1": (2, [num(8101)]), "w2": (1, [num(8201)]), "w3": (2, [num(8301)])},
              "the same run gives W2 text 1 again, W1 text 2 on the number that loaded, and W3 text 2: %s" % got)
        firsts = {n["number"] for it in b2["items"] if it["step"] == 1 for n in it["numbers"]}
        check(not (firsts & {num(8101), num(8301)}),
              "a number that loaded, or may have, never gets text 1 a second time")
        settled = (ledger.batch(bid)["result"] or {}).get("settled") or {}
        check(settled.get("not_loaded") == 2 and settled.get("owners_back") == 1 and settled.get("in_doubt") == 1
              and any(ln.startswith("Batch %d's load has finished: 2 number(s) never loaded and no longer count as texted "
                                    "(1 owner(s) can get the text again); 1 in a group the answer cannot vouch for" % bid)
                      for ln in s.lines),
              "the batch keeps what was settled, and the run says it: %s" % s.lines[:2])
        check(any(c["what"] == "sms_attempts" and c["record_uuid"] == "w2" and c["before"] == 1 and c["after"] == 0
                  for c in ledger.changes()),
              "putting the counter back is in the change log, so undo can reverse it")
        before = len(asked)
        sweep.run("t_open", crm=crm, now=at(10, 9, 30), pol=pol)
        check(len(asked) == before, "a settled load is never asked about again")

        # --- what each final answer can vouch for
        load = {"contacts": [{"group": "G1", "phone": num(8501)}, {"group": "G1", "phone": num(8502)},
                             {"group": "G2", "phone": num(8503)}, {"group": "", "phone": num(8504)}]}
        nl = textlane.never_loaded
        check(nl(load, {"status": "loading"})[0] is None
              and nl(load, {"status": "failed", "result": {"ok": False, "refused": "the loader crashed: boom"}})[0] is None
              and nl(load, {"status": "done", "result": {"ok": True}})[0] is None,
              "an answer not finished, a loader that stopped part way, or one that does not say what loaded vouches for "
              "nothing: every text stays counted")
        check(nl(load, {"status": "failed", "result": {"ok": False, "loaded": [],
                                                       "refused": "the account has no template named X: nothing was loaded"}})[:2]
              == ({num(8501), num(8502), num(8503), num(8504)}, 0),
              "a refusal before anything was written loaded nothing")
        twice = {"status": "done", "result": {"ok": True, "loaded": [num(8503)],
                                              "skipped": {"the group already has a campaign (a load sent twice)": 2},
                                              "groups": [{"group": "G1", "contacts": 0, "state": "nothing to send"},
                                                         {"group": "G2", "contacts": 1, "campaign": "k2", "state": "SCHEDULED"}]}}
        check(nl(load, twice)[:2] == (set(), 3),
              "a group skipped for having a campaign already (a load sent twice) vouches for none of its numbers, nor does a "
              "contact in no group")
        paused = {"status": "done", "result": {"ok": True, "loaded": [], "skipped": {},
                                               "groups": [{"group": "G1", "contacts": 2, "campaign": "k1", "state": "PAUSED"},
                                                          {"group": "G2", "contacts": 0, "state": "nothing to send"}]}}
        check(nl(load, paused)[:2] == ({num(8503)}, 3),
              "a campaign that exists and never reported starting keeps its numbers counted; a group with nothing to send "
              "never texted anyone")

        # --- an answer that never comes is given up on, and its texts stay counted
        _wipe(store, activity, limits, scrub)
        scpush.hand_over = building
        crm = _world([("w4", "84 Wait St", at(9, 7, 0), [mob(8401)], {})], screened)
        bid4 = sweep.run("t_open", crm=crm, now=at(9), pol=pol).batch
        st = pol.stage("t_open")
        late = at(9) + timedelta(days=textlane.SETTLE_DAYS + 2)
        ctx = stages.Ctx(policy=pol, stage=st, crm=crm, run_id="t-settle", now=late, live=True,
                         out=stages.Summary(st.id, st.kind, st.owner, st.mode, True))
        textlane.settle_loads(ctx)
        check(((ledger.batch(bid4)["result"] or {}).get("settled") or {}).get("kept") == "all"
              and len(limits.rows(batch_id=bid4, statuses=("released",))) == 1
              and any(ln.startswith("Batch %d: no final answer after 16 day(s)" % bid4) for ln in ctx.out.lines),
              "an answer still not final after %d days is given up on, and its texts stay counted" % textlane.SETTLE_DAYS)

        # --- a first_text series whose text 1 never loaded starts again the day its text really goes
        _wipe(store, activity, limits, scrub)
        key = "text_anchor:t_back:ftm"
        scpush.hand_over = building
        crm = _world([("w5", "85 Wait St", at(9, 7, 0) - timedelta(days=30), [mob(8601)], {})], screened)
        bid5 = sweep.run("t_back", crm=crm, now=at(9, 8, 50), pol=pol).batch
        check(ledger.fact("w5", key) == "2026-11-09", "counted at release, the series is anchored on Monday")
        answers[bid5] = _refuse({})
        scpush.hand_over = _load_all
        s = sweep.run("t_back", crm=crm, now=at(10, 8, 50), pol=pol)
        it = (ledger.batch(s.batch) or {"items": [{}]})["items"][0] if s.batch else {}
        check(it.get("step") == 1 and ledger.fact("w5", key) == "2026-11-10" and crm.recs["w5"]["sms_attempts"] == 1,
              "a text 1 the answer shows never loaded is taken out with its anchor and its counter, and text 1 goes again, "
              "anchoring the series that day")
    finally:
        scpush.job_status = saved_status


SC_REASON = "texted before in Smarter Contact (text 1 goes only to a number with no thread)"


def _sc_answer(texted=(), name_list=True, seen=None):
    """A fake texting app: every number in `texted` is left out of text 1 the way sc_load does it (a
    thread that holds a text), the rest are loaded. name_list False answers as the texting app did
    before 2026-11-19, with the reason's count only."""
    texted = set(texted)

    def loader(load):
        if seen is not None:
            seen.append(load)
        old = sorted({c["phone"] for c in load["contacts"] if c["phone"] in texted and c["series"] == "opening"
                      and int(c["step"]) == 1})
        loaded = [c["phone"] for c in load["contacts"] if c["phone"] not in old]
        groups = [{"group": g["name"], "contacts": sum(1 for c in load["contacts"] if c["group"] == g["name"] and c["phone"] in loaded),
                   "campaign": "k-%s" % g["name"][-4:], "state": "SCHEDULED"} for g in load["groups"]]
        res = {"ok": True, "loaded": loaded, "groups": [g for g in groups if g["contacts"]], "contacts_new": 0,
               "skipped": {SC_REASON: len(old)} if old else {}, "problems": []}
        if name_list:
            res["texted_before"] = old
        return {"status": "done", "result": res}
    return loader


def _texted_before_checks(check, *, store, activity, ledger, limits, policy, scrub, sweep, scpush, textlane, EMPLOYEES,
                          PLAYBOOK, _wipe, _world, at, mob) -> None:
    print("  text 1 and the numbers Smarter Contact texted before")
    from datetime import timedelta
    book = copy.deepcopy(PLAYBOOK)
    book["stages"]["t_open"]["auto_release"] = True
    book["stages"]["t_back"] = dict(book["stages"]["t_open"], series_start="first_text", label="Top 1000",
                                    schedule="08:55 Mon-Fri")
    pol = policy.parse(book, EMPLOYEES)
    num = lambda n: "865555%04d" % n  # noqa: E731

    def screened(*uuids):
        for u in uuids:
            ledger.set_fact(u, "death_screen", {"at": "2026-11-09T12:00:00+00:00", "hit": "", "detail": ""})

    # --- every number texted before: remembered at release, then the owner is closed and stops taking a place
    _wipe(store, activity, limits, scrub)
    scpush.hand_over = _sc_answer(texted={num(7101), num(7102)})
    crm = _world([("x1", "71 Thread St", at(9, 7, 0), [mob(7101), mob(7102, "Dial Second")], {})], screened)
    s = sweep.run("t_open", crm=crm, now=at(9), pol=pol)
    b = ledger.batch(s.batch) if s.batch else {}
    res = b.get("result") or {}
    check(b.get("status") == "held" and not crm.writes and limits.sc_texted([num(7101), num(7102)]) == {num(7101), num(7102)},
          "numbers Smarter Contact left out of text 1 because it texted them before are remembered, and nobody is counted")
    check(res.get("sc_texted") == 2 and (res.get("held") or {}).get(textlane.SC_NOT_LOADED) == 2
          and "not loaded into Smarter Contact" not in (res.get("held") or {}) and "remembered" in s.head,
          "the batch says why those numbers did not load, and the run says they are remembered (%s)" % s.head)
    before = crm.reads
    s2 = sweep.run("t_open", crm=crm, now=at(9, 9, 30), pol=pol)
    check(not s2.batch and s2.tally.get(textlane.SC_CLOSED) == 1 and s2.tally.get(textlane.SC_LEFT_OUT) == 2
          and "Closed the opening texts for 1 owner(s)" in s2.head and crm.reads == before + 1,
          "the next run leaves both numbers out, closes the owner's opening texts and says so (%s)" % s2.head)
    s3 = sweep.run("t_open", crm=crm, now=at(10), pol=pol)
    check(not s3.batch and crm.reads == before + 1 and not s3.tally.get(textlane.SC_CLOSED),
          "a closed owner is not read again and takes no place in the day's texts")

    # --- one number texted before, the other not: text 1 goes to the other one only
    _wipe(store, activity, limits, scrub)
    limits.note_sc_texted({num(7201): ("o-x2", "x2")}, stage="t_open")
    scpush.hand_over = _sc_answer()
    crm = _world([("x2", "72 Thread St", at(9, 7, 0), [mob(7201), mob(7202, "Dial Second")], {})], screened)
    s = sweep.run("t_open", crm=crm, now=at(9), pol=pol)
    it = (ledger.batch(s.batch) or {"items": [{}]})["items"][0] if s.batch else {}
    check([n["number"] for n in it.get("numbers") or []] == [num(7202)] and s.tally.get(textlane.SC_LEFT_OUT) == 1
          and not s.tally.get(textlane.SC_CLOSED),
          "an owner with another number gets text 1 on that number only, and is not closed")

    # --- texts 2 and 3 are as before: the thread's own number, whatever the memory says
    _wipe(store, activity, limits, scrub)
    scpush.hand_over = _sc_answer(texted={num(7302)})
    crm = _world([("x3", "73 Thread St", at(9, 7, 0), [mob(7301), mob(7302, "Dial Second")], {})], screened)
    owner = crm.recs["x3"]["owner"]["uuid"]
    sweep.run("t_open", crm=crm, now=at(9), pol=pol)
    s = sweep.run("t_open", crm=crm, now=at(10), pol=pol)
    it = (ledger.batch(s.batch) or {"items": [{}]})["items"][0] if s.batch else {}
    steps = sorted(int(t["step"]) for t in limits.touches(owner, "2026-11-10", since_day="2026-11-01", series="opening"))
    check(limits.sc_texted([num(7302)]) == {num(7302)} and it.get("step") == 2
          and [n["number"] for n in it.get("numbers") or []] == [num(7301)] and steps == [1, 2],
          "text 1 went to the new number and was counted; text 2 follows that thread as it always did")

    # --- the reason is read as the texting app writes it, and only that reason counts
    load = {"contacts": [{"group": "G", "phone": num(7401), "series": "opening", "step": 1},
                         {"group": "G", "phone": num(7402), "series": "opening", "step": 1},
                         {"group": "G T2", "phone": num(7403), "series": "opening", "step": 2}]}

    def doc(status="done", **res):
        return {"status": status, "result": dict({"ok": status == "done", "loaded": [num(7401), num(7403)]}, **res)}
    check(textlane.texted_before(load, doc(texted_before=[num(7402)], skipped={SC_REASON: 1})) == {num(7402)},
          "the numbers the texting app names are the ones remembered")
    check(textlane.texted_before(load, doc("failed", texted_before=[num(7402)])) == {num(7402)}
          and textlane.texted_before(load, doc("failed", refused="no number can send", texted_before=[num(7402)])) == set()
          and textlane.texted_before(load, {"status": "loading"}) == set() and textlane.texted_before(load, None) == set(),
          "a named number counts even when a group failed later; a refused, unfinished or missing answer gives nothing")
    check(textlane.texted_before(load, doc(skipped={SC_REASON: 1})) == {num(7402)},
          "a texting app from before the list: the one text 1 number it did not load is the one its count names")
    check(textlane.texted_before(load, doc(loaded=[num(7403)], skipped={SC_REASON: 1})) == set()
          and textlane.texted_before(load, doc(skipped={"on Smarter Contact's own do-not-text list": 1})) == set(),
          "when another number was lost too, or the reason is another one, nothing is remembered")

    # --- the loads answered before this memory existed are read once
    _wipe(store, activity, limits, scrub)
    bid = ledger.batch_create("t_open", "text", [{"record_uuid": "x5", "day": "2026-11-08"}], 0)
    old_load = {"batch": bid, "contacts": [{"group": "FTM 1108 Named East", "phone": num(7501), "series": "opening", "step": 1,
                                            "owner": "o-x5", "record": "x5"},
                                           {"group": "FTM 1108 Named East", "phone": num(7502), "series": "opening", "step": 1,
                                            "owner": "o-x5", "record": "x5"}]}
    p = textlane.load_path(bid)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(old_load), encoding="utf-8")
    ledger.batch_set(bid, "sent", {"loader": {"status": "done", "result": {"ok": True, "loaded": [num(7501)],
                                                                            "skipped": {SC_REASON: 1}}}})
    done = ledger.batch_create("t_open", "text", [{"record_uuid": "x6"}], 0)
    ledger.batch_set(done, "sent", {"sc_texted": 0, "loader": {"status": "done", "result": {"skipped": {SC_REASON: 1}}}})
    first, again = textlane.learn_past_loads(at(9)), textlane.learn_past_loads(at(9))
    check(first == 1 and again == 0 and limits.sc_texted([num(7501), num(7502)]) == {num(7502)}
          and int(store.kv_get(textlane.SC_LEARNED_KEY) or 0) == done,
          "a load answered before the memory existed is read once for its texted-before numbers, and a batch "
          "already remembered at its release is not read again")

    # --- a first_text stage: text 1 staged and lost, closed the next day, then looked at again after
    # sc_recheck_days: the anchor of the text 1 that never went is not counted, and text 1 goes to a number found since
    _wipe(store, activity, limits, scrub)
    scpush.hand_over = _sc_answer(texted={num(7601)})
    crm = _world([("y1", "76 Backlog St", at(9, 7, 0) - timedelta(days=40), [mob(7601)], {})], screened)
    s = sweep.run("t_back", crm=crm, now=at(9, 8, 55), pol=pol)
    s2 = sweep.run("t_back", crm=crm, now=at(10, 8, 55), pol=pol)
    reads = crm.reads
    s3 = sweep.run("t_back", crm=crm, now=at(11, 8, 55), pol=pol)
    check(bool(s.batch) and ledger.batch(s.batch)["status"] == "held" and limits.sc_texted([num(7601)]) == {num(7601)}
          and s2.tally.get(textlane.SC_CLOSED) == 1 and not s2.batch and not s3.batch and crm.reads == reads,
          "a first_text stage closes an owner whose only number Smarter Contact texted before, and stops reading it")
    crm.recs["y1"]["owner"]["phones"].append(mob(7602, "Dial Second"))
    s4 = sweep.run("t_back", crm=crm, now=at(10, 8, 55, month=12), pol=pol)
    b4 = ledger.batch(s4.batch) if s4.batch else {"items": [{}]}
    it = b4["items"][0]
    check(it.get("step") == 1 and [n["number"] for n in it.get("numbers") or []] == [num(7602)] and b4.get("status") == "sent"
          and any(k.startswith("looked at again") for k in s4.tally) and ledger.fact("y1", "text_anchor:t_back:ftm") == "2026-12-10",
          "after sc_recheck_days the owner is looked at again as new, and text 1 goes to the number found since (%s)" % dict(s4.tally))
    never = copy.deepcopy(book)
    never["stages"]["t_back"]["sc_recheck_days"] = 0
    check(textlane.sc_reopens({"why": textlane.SC_CLOSED_WHY, "at": "2026-01-01T00:00:00+00:00"}, at(9), 0) is False
          and textlane.sc_reopens({"why": "past day 2", "at": "2026-01-01T00:00:00+00:00"}, at(9), 30) is False
          and bool(policy.parse(never, EMPLOYEES)),
          "sc_recheck_days 0 keeps the close for good, and no other close is ever reopened")
    try:
        bad = copy.deepcopy(book)
        bad["stages"]["t_back"]["sc_recheck_days"] = "monthly"
        policy.parse(bad, EMPLOYEES)
        check(False, "a stage block whose sc_recheck_days is not a whole number is refused")
    except policy.PolicyError as exc:
        check("sc_recheck_days" in str(exc), "a stage block whose sc_recheck_days is not a whole number is refused")

    # --- a person's release leaves out a number learned after the batch was staged
    _wipe(store, activity, limits, scrub)
    manual = policy.parse(copy.deepcopy(PLAYBOOK), EMPLOYEES)
    crm = _world([("z1", "78 Staged St", at(9, 7, 0), [mob(7801)], {})], screened)
    s = sweep.run("t_open", crm=crm, now=at(9), pol=manual)
    limits.note_sc_texted({num(7801): ("", "")})
    r = textlane.release(s.batch, crm=crm, pol=manual, commit=False, now=at(9, 9, 0)) if s.batch else {}
    check(bool(s.batch) and r.get("would_hold") == {textlane.SC_HOLD: 1},
          "a staged text 1 whose every number Smarter Contact texted before is held at release, never handed over")


def _screen_write_checks(check, *, store, activity, ledger, limits, scrub, policy, sweep, EMPLOYEES, PLAYBOOK, _wipe, at, mob) -> None:
    print("  the death screen survives a write DataSift refuses")
    from .selftest_autopilot import PRESETS, FakeCRM, _rec
    book = copy.deepcopy(PLAYBOOK)
    book["stages"]["t_scr"] = {"kind": "screen", "owner": "research", "mode": "live", "schedule": "07:00 Mon-Fri",
                               "lane": "ftm", "presets": ["FTM - 02 Ready to Call"]}
    pol = policy.parse(book, EMPLOYEES)

    class NoteRefused(FakeCRM):
        def message(self, u, text, pin=False):
            raise RuntimeError("HTTP 404 on POST add-notes")

    class TagRefused(FakeCRM):
        def add_tags(self, u, add, rec=None):
            raise RuntimeError("HTTP 404 on POST add-tags")

    for cls, name in ((NoteRefused, "note"), (TagRefused, "tag")):
        _wipe(store, activity, limits, scrub)
        recs = [_rec("sx1", "61 Notice St", phones=[mob(6101)], notes="Notice: Estate of Pat Example"),
                _rec("sx2", "62 Quiet St", phones=[mob(6201)])]
        crm = cls(recs)
        try:
            s = sweep.run("t_scr", crm=crm, now=at(9, 7, 0), pol=pol)
            crashed = False
        except Exception as exc:  # noqa: BLE001
            s, crashed = None, exc
        check(not crashed, "a refused %s write does not stop the screen (%s)" % (name, crashed or "ok"))
        if crashed:
            continue
        hit_fact = ledger.fact("sx1", "death_screen")
        quiet_fact = ledger.fact("sx2", "death_screen")
        if name == "note":
            check(hit_fact is not None and quiet_fact is not None and any("research note" in p for p in s.problems),
                  "when only the note is refused, the tag holds the record, it counts as screened, and the problem is said")
        else:
            check(hit_fact is None and quiet_fact is not None and any("tag for research" in p for p in s.problems),
                  "when the tag is refused, the record stays unscreened (no text before it is), and the rest are screened")


def _note_route_checks(check) -> None:
    print("  the real CRM writes a note on the documented property message route")
    from .autopilot import crm as crm_mod
    sent = []

    class Wire(crm_mod.CRM):
        def __init__(self):  # no client, no network
            pass

        def call(self, path, method="GET", body=None, headers=None, _tries=6):
            sent.append((method, path, body))
            return {"uuid": "m-1"} if path.endswith("/message/") else None

    w = Wire()
    got = w.message("rec-1", "Possible death found (sample)")
    check(sent and sent[0] == ("POST", "/api/internal/property/rec-1/message/", {"message": "Possible death found (sample)"}),
          "a note is POST /property/{uuid}/message/ with a message body, never the removed add-notes route (%s)" % sent[:1])
    check(got == "m-1" and len(sent) == 1, "an unpinned note is one call and returns the message uuid")
    check(not any("add-notes" in p for _, p, _ in sent), "nothing goes to add-notes")
    sent.clear()
    w.message("rec-1", "x" * 5000, pin=True)
    check(len(sent[0][2]["message"]) == 4000, "a long note is cut to 4,000 characters")
    check(len(sent) == 2 and sent[1][:2] == ("POST", "/api/internal/property/rec-1/message/m-1/pin/"),
          "a pinned note is pinned through the documented pin route")


def _playbook_checks(check, policy) -> None:
    print("  the real playbook: every text a live stage may send can reach the death screen first")
    pol = policy.load()
    if pol is None:
        check(False, "the playbook loads")
        return
    screened: dict[str, set] = {}
    for st in pol.stages.values():
        if st.kind == "screen" and st.mode == "live":
            screened.setdefault(str(st.get("lane") or ""), set()).update(st.presets)
    gaps = []
    for st in pol.stages.values():
        if st.kind == "textlane" and st.mode == "live" and st.get("require_screen", True) and st.presets:
            missing = [p for p in st.presets if p not in screened.get(str(st.get("lane") or ""), set())]
            if missing:
                gaps.append("%s: %s" % (st.id, ", ".join(missing)))
    check(not gaps, "a live text stage that waits on the death screen reads only presets that screen reads too "
                    "(else those records wait forever): %s" % ("; ".join(gaps) or "ok"))
    live = {sid for sid, st in pol.stages.items() if st.mode == "live" and st.kind == "textlane"}
    check(all(pol.stage(s).get("auto_release") is True for s in live),
          "every live text stage releases its own batch (a live stage left to a person would stage and wait): %s" % sorted(live))
    # A text goes only to a mobile scored Dial First or Second (gate.usable_numbers): with the lane's
    # scoring in plan, no new record ever has a textable number (found 2026-11-18, the first live day).
    scored = {str(st.get("lane") or "") for st in pol.stages.values() if st.kind == "score" and st.mode == "live"}
    unscored = sorted(s for s in live if str(pol.stage(s).get("lane") or "") not in scored)
    check(not unscored, "every live text stage's lane has a live scoring stage, or no new number is ever textable: %s"
          % (unscored or "ok"))


def _push_checks(check, scpush) -> None:
    print("  the handover to the texting app")
    route, tok = "https://texting.example/hooks/scload", "s3cret-token-example"

    class Resp:
        def __init__(self, code, doc=None, text=""):
            self.status_code, self._doc, self.text = code, doc or {}, text

        def json(self):
            return self._doc

    class Session:
        def __init__(self, gets):
            self.posts, self.gets, self.heads, self._gets = [], [], [], list(gets)

        def post(self, url, json=None, headers=None, timeout=0):
            self.posts.append(url)
            self.heads.append(headers or {})
            return Resp(200, {"accepted": True})

        def get(self, url, headers=None, timeout=0):
            self.gets.append(url)
            self.heads.append(headers or {})
            return Resp(200, self._gets.pop(0) if self._gets else {"status": "loading"})

    saved = {k: os.environ.get(k) for k in ("AGENTS_SC_LOAD_URL", "AGENTS_SC_LOAD_TOKEN")}
    try:
        os.environ.pop("AGENTS_SC_LOAD_URL", None)
        os.environ.pop("AGENTS_SC_LOAD_TOKEN", None)
        doc = scpush.hand_over({"batch": 5})
        check(doc["status"] == "not handed over" and "AGENTS_SC_LOAD_URL" in doc["why"],
              "with no load route set nothing is handed over, and the stage says why")
        os.environ["AGENTS_SC_LOAD_URL"], os.environ["AGENTS_SC_LOAD_TOKEN"] = route, tok
        sess = Session([{"status": "loading"}, {"status": "done", "result": {"ok": True, "loaded": ["8655550001"]}}])
        doc = scpush.hand_over({"batch": 5}, session=sess, sleep=lambda s: None, wait_s=60, poll_s=0)
        check(doc["status"] == "done" and sess.posts == [route] and sess.gets[-1] == route + "/5",
              "the load is posted once and its result read back until Smarter Contact is done")
        check(all(h.get("authorization") == "Bearer " + tok for h in sess.heads) and not any(tok in u for u in sess.posts + sess.gets),
              "the token rides in a header on every call and never in a URL, so no access log prints it")

        class Broken(Session):
            def post(self, url, json=None, headers=None, timeout=0):
                raise RuntimeError("could not connect to %s with %s" % (url, (headers or {}).get("authorization")))
        doc = scpush.hand_over({"batch": 6}, session=Broken([]))
        line, problems = scpush.words(doc)
        check(doc["status"] == "not handed over" and tok not in json.dumps(doc) + line + " ".join(problems),
              "a failed handover is reported without the token in it")
        line, _ = scpush.words({"status": "done", "result": {"ok": True, "groups": [{"campaign": "k1", "contacts": 3},
                                                                                    {"campaign": "", "contacts": 2}],
                                                             "contacts_new": 2, "skipped": {"on Smarter Contact's own do-not-text list": 1}}})
        check(line == "In Smarter Contact: 1 campaign(s) for 3 text(s), 2 new contact(s); left out 1 on Smarter Contact's own "
                      "do-not-text list.", "the result reads as one plain line")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
